"""管理面作用域化 API Token 的回归测试（见 docs/api-tokens.md）。

锁定的性质（每一条都能在实现里追溯到动机）：

  * 明文**只在创建时返回一次**，列表永远不回传明文或哈希；
  * scope 决定角色，且**非法 scope 降级为最小权限**（宁可给少了）；
  * readonly 不能调写接口；admin 能调一般写接口；
  * **token 管理接口只接受会话**——泄露的 token 不能拿来自助提权；
  * **高危接口只接受会话**——即便 token 是 admin scope（更新、清日志、用户管理…）；
  * 过期 / 停用 / 删除后**立即失效**（逐次校验，无缓存）；
  * 失败**不泄露原因**（不存在 / 停用 / 过期 都是同一个 401）；
  * 令牌不绑定用户：改动用户表不影响已签发的令牌。

注意：鉴权**会话优先**，而 `TestClient` 自带 cookie jar——若登录后不清空，
后续带 Bearer 的请求会被会话顶掉，用例会全部「假通过」（其实测的是会话）。
所有会话调用都走 `_as_session()`，退出即清空 jar。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security, tokensvc  # noqa: E402


class ApiTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'tokens.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        # 令牌校验失败会走按 IP 的失败计数（防爆破）。用例里会刻意发坏令牌，
        # 必须每次清干净，否则后面的登录会被自己锁掉（429）。
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    def tearDown(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    # ── 工具 ─────────────────────────────────────────────
    @contextmanager
    def _as_session(self):
        """以**会话**身份执行，退出时清空 cookie jar。"""
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'admin-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def _mint(self, name: str, scope: str = 'readonly', expires_at=None) -> str:
        """用会话创建一个令牌，返回明文。"""
        with self._as_session() as c:
            r = c.post('/api/tokens',
                       json={'name': name, 'scope': scope, 'expires_at': expires_at})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['token']

    @staticmethod
    def _bearer(token: str) -> dict:
        return {'Authorization': f'Bearer {token}'}

    def _id_of(self, token: str) -> int:
        row = tokensvc.resolve(token)
        assert row is not None
        return int(row['id'])

    # ── 创建 / 列表 ───────────────────────────────────────
    def test_create_returns_plaintext_once_and_list_hides_it(self) -> None:
        with self._as_session() as c:
            created = c.post('/api/tokens', json={'name': 'ci', 'scope': 'readonly'}).json()
            listed = c.get('/api/tokens').json()
        self.assertTrue(created['token'].startswith('wbt_'), '明文应带 wbt_ 前缀')
        self.assertEqual(created['prefix'], created['token'][:12])
        row = next(t for t in listed if t['id'] == created['id'])
        self.assertNotIn('token', row, '列表绝不能回传明文')
        self.assertNotIn('token_hash', row, '列表绝不能回传哈希')

    def test_blank_name_rejected(self) -> None:
        with self._as_session() as c:
            r = c.post('/api/tokens', json={'name': '   '})
        self.assertEqual(r.status_code, 400)

    def test_invalid_scope_downgrades_to_readonly(self) -> None:
        """非法 scope 必须降级为最小权限，而不是报错或意外给管理员。"""
        self.assertEqual(tokensvc.normalize_scope('ADMIN '), 'admin')
        self.assertEqual(tokensvc.normalize_scope('root'), 'readonly')
        self.assertEqual(tokensvc.normalize_scope(''), 'readonly')
        self.assertEqual(tokensvc.normalize_scope(None), 'readonly')

    # ── 鉴权 ─────────────────────────────────────────────
    def test_token_authenticates_and_role_follows_scope(self) -> None:
        ro = self._mint('ro', 'readonly')
        r = self.c.get('/api/me', headers=self._bearer(ro))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['role'], 'viewer')
        self.assertTrue(r.json()['username'].startswith('token:'))

        adm = self._mint('adm', 'admin')
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(adm)).json()['role'], 'admin')

    def test_readonly_cannot_write(self) -> None:
        ro = self._mint('ro2', 'readonly')
        r = self.c.post('/api/accounts/checkin-all', headers=self._bearer(ro))
        self.assertEqual(r.status_code, 403, '只读令牌不能调写接口')

    def test_admin_token_can_write_general_endpoint(self) -> None:
        """admin 令牌能调一般写接口（密钥轮换是自动化的典型用途）。"""
        adm = self._mint('adm2', 'admin')
        h = self._bearer(adm)
        r = self.c.post('/api/keys', json={'name': 'by-token'}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        key_id = r.json()['id']
        self.assertEqual(self.c.delete(f'/api/keys/{key_id}', headers=h).status_code, 200)

    def test_bad_tokens_do_not_leak_reason(self) -> None:
        """不存在的 / 停用的 / 过期的，都必须是同一个笼统 401。"""
        disabled = self._mint('dis')
        with self._as_session() as c:
            c.patch(f'/api/tokens/{self._id_of(disabled)}', json={'enabled': False})
        expired = self._mint('exp', expires_at=int(time.time()) - 10)
        for tok in ('wbt_not_a_real_token', disabled, expired, 'wbk_gateway_key'):
            r = self.c.get('/api/me', headers=self._bearer(tok))
            self.assertEqual(r.status_code, 401, f'{tok[:12]} 应被拒')
            self.assertEqual(r.json().get('detail'), '未登录', '不得泄露失败原因')

    def test_deleted_token_immediately_invalid(self) -> None:
        tok = self._mint('todelete')
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 200)
        with self._as_session() as c:
            r = c.delete(f'/api/tokens/{self._id_of(tok)}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 401)

    def test_expired_token_immediately_invalid(self) -> None:
        tok = self._mint('exp2', expires_at=int(time.time()) - 1)
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 401)

    def test_tampered_token_rejected(self) -> None:
        tok = self._mint('tamper')
        bad = tok[:-1] + ('A' if tok[-1] != 'A' else 'B')
        self.assertIsNone(tokensvc.resolve(bad), '改一个字符就应解析不出来')

    # ── 边界：token 不能管 token、不能碰高危接口 ──────────────
    def test_token_cannot_manage_tokens(self) -> None:
        """泄露的 token 不能用来创建 / 列表 / 删除令牌（防自助提权与持久化）。"""
        h = self._bearer(self._mint('adm3', 'admin'))
        self.assertEqual(self.c.get('/api/tokens', headers=h).status_code, 403)
        self.assertEqual(self.c.post('/api/tokens', json={'name': 'self'}, headers=h).status_code, 403)
        self.assertEqual(self.c.delete('/api/tokens/1', headers=h).status_code, 403)

    def test_admin_token_rejected_on_session_only_endpoints(self) -> None:
        """高危 / 不可逆接口只对会话开放——即便 token 是 admin scope。"""
        h = self._bearer(self._mint('adm4', 'admin'))
        cases = [
            ('post', '/api/logs/clear', None),
            ('post', '/api/system/update', {'target': 'manager'}),
            ('post', '/api/system/upstream-ref', {'ref': ''}),
            ('post', '/api/security/logs/clear', None),
            ('post', '/api/users', {'username': 'x', 'password': '12345678', 'role': 'viewer'}),
        ]
        for method, path, body in cases:
            r = getattr(self.c, method)(path, headers=h, **({'json': body} if body else {}))
            self.assertEqual(r.status_code, 403, f'{path} 不应接受 API Token')
            self.assertIn('API Token', r.json().get('detail', ''))

    def test_session_still_allowed_on_session_only_endpoint(self) -> None:
        """会话调高危接口不受影响（新约束不能误伤正常人）。"""
        with self._as_session() as c:
            r = c.post('/api/logs/clear')
        self.assertEqual(r.status_code, 200, r.text)

    # ── 记账 ─────────────────────────────────────────────
    def test_last_used_is_throttled(self) -> None:
        """last_used 写库要节流：窗口内第二次不该再写。"""
        tid = self._id_of(self._mint('throttle'))
        tokensvc.touch(tid, '1.1.1.1')
        first = db.query_one('SELECT last_used_ip FROM api_tokens WHERE id = ?', (tid,))
        self.assertEqual(first['last_used_ip'], '1.1.1.1')
        tokensvc.touch(tid, '2.2.2.2')  # 窗口内 → 跳过
        again = db.query_one('SELECT last_used_ip FROM api_tokens WHERE id = ?', (tid,))
        self.assertEqual(again['last_used_ip'], '1.1.1.1', '节流窗口内不应改写')

    def test_create_and_delete_are_audited_without_plaintext(self) -> None:
        tok = self._mint('audited')
        with self._as_session() as c:
            c.delete(f'/api/tokens/{self._id_of(tok)}')
            logs = c.get('/api/audit-logs', params={'limit': 200}).json()
        actions = [row['action'] for row in logs['items']]
        self.assertIn('create_token', actions)
        self.assertIn('delete_token', actions)
        self.assertNotIn(tok, json.dumps(logs, ensure_ascii=False), '审计日志绝不能出现明文令牌')

    def test_token_does_not_depend_on_users_table(self) -> None:
        """令牌不绑定用户：改动用户表不影响已签发的令牌。"""
        tok = self._mint('decoupled', 'admin')
        cfg = security.load_users()
        cfg['users'] = []
        security.save_users(cfg)
        try:
            self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 200)
        finally:
            cfg['users'] = [{'username': 'admin', 'role': 'admin',
                             'pwd_hash': security.make_hash('admin-pw')}]
            security.save_users(cfg)


if __name__ == '__main__':
    unittest.main()
