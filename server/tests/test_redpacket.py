"""红包：分配算法与创建流程。

为什么算法部分要测得这么细：金额分配的错误是**静默**的 —— 界面照样显示
「10 份、合计 1000」，只是各份之和悄悄不等于 1000（少发或多发），或者某一份
拿到了 0。后者尤其危险：配额 0 在 keysvc 里的语义是**不限**，一个「0 积分」
的红包份额等于一把无限额度的钥匙——比少发钱严重得多。

所以下面钉住三条不变式：
  1. 各份之和 **精确等于** 总额（不是「约等于」）；
  2. 每份 **不低于** 该类别的最小单位；
  3. 份数 ≥ 2 时分配结果**不该是常量**（那说明随机性没生效，退化成均分）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.redpacket import (  # noqa: E402
    MAX_SHARES, MODE_EVEN, MODE_LUCKY, RedPacketError,
    KIND_CREDIT, KIND_TOKEN, split_amount, validate,
)


class SplitTest(unittest.TestCase):
    """分配算法的三条不变式。"""

    def test_sum_is_exact(self) -> None:
        """**核心**：各份之和必须精确等于总额。

        差一分钱看起来无所谓，但这是「红包」——用户会拿计算器加一遍。
        而且浮点累加很容易差出 0.01，所以最后一份必须拿余数兜底。
        """
        cases = [
            (100.0, 3, KIND_CREDIT), (1000.0, 7, KIND_CREDIT),
            (0.05, 5, KIND_CREDIT), (99.99, 11, KIND_CREDIT),
            (1000, 7, KIND_TOKEN), (100, 100, KIND_TOKEN),
        ]
        for total, shares, kind in cases:
            for mode in (MODE_LUCKY, MODE_EVEN):
                with self.subTest(total=total, shares=shares, kind=kind, mode=mode):
                    got = split_amount(total, shares, kind, mode)
                    self.assertEqual(len(got), shares, '份数不对')
                    self.assertEqual(round(sum(got), 2), round(float(total), 2),
                                     f'各份之和 {sum(got)} != 总额 {total}')

    def test_each_share_at_least_min_unit(self) -> None:
        """每份不能低于最小单位。

        **这一条是防「无限额度钥匙」的**：配额 0 在 keysvc 里意味着「不限」，
        所以一份 0 额度的红包 key 不是「没钱」而是「随便花」。
        """
        for total, shares, kind, unit in [
            (0.05, 5, KIND_CREDIT, 0.01),
            (1000, 10, KIND_TOKEN, 1),
            (100.0, 7, KIND_CREDIT, 0.01),
        ]:
            for mode in (MODE_LUCKY, MODE_EVEN):
                got = split_amount(total, shares, kind, mode)
                self.assertTrue(all(a >= unit for a in got),
                                f'{kind}/{mode} 出现了低于 {unit} 的份额: {got}')

    def test_lucky_is_not_constant(self) -> None:
        """拼手气要真的随机 —— 否则它和均分没区别。

        只跑一次可能碰巧全等（概率极低但存在），所以取三次看是否有差异。
        """
        results = {tuple(split_amount(1000.0, 10, KIND_CREDIT, MODE_LUCKY))
                   for _ in range(3)}
        self.assertGreater(len(results), 1, '三次分出来的完全相同——随机性没生效')

    def test_even_is_constant(self) -> None:
        """均分就该每份一样（除了最后一份兜底的零头）。"""
        got = split_amount(100.0, 4, KIND_CREDIT, MODE_EVEN)
        self.assertEqual(got[:3], [25.0, 25.0, 25.0])
        self.assertEqual(round(sum(got), 2), 100.0)

    def test_single_share_gets_everything(self) -> None:
        """一份的红包 = 全部额度（退化情形，但必须对）。"""
        self.assertEqual(split_amount(88.8, 1, KIND_CREDIT, MODE_LUCKY), [88.8])

    def test_token_shares_are_integers(self) -> None:
        """token 是整数 —— 不能分出 0.5 个 token。"""
        got = split_amount(1000, 7, KIND_TOKEN, MODE_LUCKY)
        self.assertTrue(all(float(a).is_integer() for a in got), f'出现小数: {got}')

    def test_extremes_do_not_break(self) -> None:
        """两种极端分布都要能算出来（算法不能因为随机值越界抛异常）。"""
        # 总额刚好够每份最小值
        got = split_amount(0.03, 3, KIND_CREDIT, MODE_LUCKY)
        self.assertEqual(round(sum(got), 2), 0.03)
        # 份数上限
        got = split_amount(10000.0, MAX_SHARES, KIND_CREDIT, MODE_LUCKY)
        self.assertEqual(len(got), MAX_SHARES)
        self.assertEqual(round(sum(got), 2), 10000.0)


class ValidateTest(unittest.TestCase):
    """参数校验 —— 不合法一律抛 RedPacketError（路由层翻成 400）。"""

    def test_accepts_normal(self) -> None:
        validate(100.0, 10, KIND_CREDIT, MODE_LUCKY, 7)
        validate(1000, 5, KIND_TOKEN, MODE_EVEN, None, ['glm-5.2'])

    def test_rejects_bad_kind_or_mode(self) -> None:
        for kind, mode in [('money', MODE_LUCKY), (KIND_CREDIT, 'random')]:
            with self.assertRaises(RedPacketError):
                validate(100.0, 5, kind, mode, 7)

    def test_rejects_too_small_total(self) -> None:
        """总额不够每份最小值时必须拒绝。

        不拦的话 split_amount 会产出 0 份额 —— 而配额 0 = **不限**
        （见 keysvc），等于发了一把无限额度的钥匙。
        """
        with self.assertRaises(RedPacketError):
            validate(0.02, 5, KIND_CREDIT, MODE_LUCKY, 7)     # 每份只有 0.004
        with self.assertRaises(RedPacketError):
            validate(5, 10, KIND_TOKEN, MODE_LUCKY, 7)        # 每份只有 0.5 个

    def test_rejects_bad_shares(self) -> None:
        for shares in (0, -1, MAX_SHARES + 1, 1.5, True):
            with self.assertRaises(RedPacketError, msg=f'shares={shares!r}'):
                validate(1000.0, shares, KIND_CREDIT, MODE_LUCKY, 7)

    def test_rejects_bad_ttl(self) -> None:
        for ttl in (0, -1, 99999):
            with self.assertRaises(RedPacketError, msg=f'ttl={ttl!r}'):
                validate(100.0, 5, KIND_CREDIT, MODE_LUCKY, ttl)

    def test_token_requires_models(self) -> None:
        """**token 红包必须限定模型范围。**

        为什么：token 是「量」，与模型强相关 —— 同一段上下文在不同模型下的
        token 数、输出长度、上下文窗口都不同。不限定的话「10 万 token 红包」
        的含义是浮动的，收的人也不知道能拿它干什么。
        """
        with self.assertRaises(RedPacketError):
            validate(1000, 5, KIND_TOKEN, MODE_LUCKY, 7)             # 没给
        with self.assertRaises(RedPacketError):
            validate(1000, 5, KIND_TOKEN, MODE_LUCKY, 7, [])         # 空列表
        with self.assertRaises(RedPacketError):
            validate(1000, 5, KIND_TOKEN, MODE_LUCKY, 7, ['  '])     # 只有空白
        validate(1000, 5, KIND_TOKEN, MODE_LUCKY, 7, ['glm-5.2'])    # 正常

    def test_credit_forbids_models(self) -> None:
        """**积分红包必须不限定模型。**

        积分是「钱」，按上游真实扣费算，任何模型都能用；再叠一层模型限制
        只会让人算不清「这红包到底值多少」（想控成本就少发点积分）。
        所以传了反而拦下来把语义钉死，而不是默默忽略 —— 后者会让管理员
        以为自己设的限制生效了。
        """
        validate(100.0, 5, KIND_CREDIT, MODE_LUCKY, 7)               # 不传 = 对
        validate(100.0, 5, KIND_CREDIT, MODE_LUCKY, 7, [])           # 空列表 = 对
        with self.assertRaises(RedPacketError):
            validate(100.0, 5, KIND_CREDIT, MODE_LUCKY, 7, ['glm-5.2'])


class CreatePacketTest(unittest.TestCase):
    """端到端：真的建出密钥、额度对、过期时间对、事务能回滚。"""

    @classmethod
    def setUpClass(cls) -> None:
        from server import config, db
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / 'rp.db'
        db._conn = None
        db.connect()

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        from server import db
        db.execute('DELETE FROM red_packet_shares')
        db.execute('DELETE FROM red_packets')
        db.execute('DELETE FROM api_keys')

    def _create(self, **kw):
        from server import redpacket
        args = {'name': '测试红包', 'kind': KIND_CREDIT, 'total': 100.0,
                'shares': 5, 'mode': MODE_LUCKY, 'ttl_days': 7, 'actor': 'admin'}
        args.update(kw)
        # token 红包**必须**限定模型、积分红包**必须**不限定（见 validate）。
        # 这里按类别自动补上，免得每个用例都写一遍而漏掉。
        if args['kind'] == KIND_TOKEN and 'models' not in args:
            args['models'] = ['glm-5.2']
        return redpacket.create_packet(**args)

    def test_creates_one_key_per_share(self) -> None:
        from server import db
        out = self._create(shares=5)
        self.assertEqual(len(out['keys']), 5)
        self.assertEqual(len({k['key'] for k in out['keys']}), 5, '明文 token 不该重复')
        n = db.query_one('SELECT COUNT(*) AS c FROM api_keys')['c']
        self.assertEqual(n, 5)

    def test_amounts_land_on_the_keys(self) -> None:
        """分到的额度必须**真的写进密钥配额** —— 否则红包只是好看。"""
        out = self._create(kind=KIND_CREDIT, total=100.0, shares=4)
        amounts = [k['quota_credit'] for k in out['keys']]
        self.assertEqual(round(sum(amounts), 2), 100.0)
        self.assertTrue(all(a > 0 for a in amounts), f'有份额是 0（= 不限额度！）: {amounts}')

    def test_token_kind_uses_token_quota(self) -> None:
        """token 红包写 quota，积分红包写 quota_credit —— 别串了。"""
        out = self._create(kind=KIND_TOKEN, total=1000, shares=5)
        for k in out['keys']:
            self.assertGreater(k['quota'], 0)
            self.assertEqual(k['quota_credit'], 0, 'token 红包不该动积分额度')

    def test_token_models_land_on_every_key(self) -> None:
        """token 红包的模型范围要**真的落到每个密钥上**（不是只记在红包表里）。

        只记在红包表的话，界面上写着「限定 glm-5.2」，而密钥实际什么模型都能调
        —— 那是最糟的一种：看起来有限制。
        """
        limit = ['glm-5.2', 'deepseek-v4.1-flash']
        out = self._create(kind=KIND_TOKEN, total=1000, shares=3, models=limit)
        self.assertEqual(out['models'], limit)
        for k in out['keys']:
            self.assertEqual(k['models'], limit, '密钥没带上模型白名单')

    def test_credit_packet_leaves_models_empty(self) -> None:
        """积分红包不限制模型：密钥的白名单是空的（= 任何模型可用）。"""
        out = self._create(kind=KIND_CREDIT, total=100.0, shares=3)
        self.assertEqual(out['models'], [])
        for k in out['keys']:
            self.assertEqual(k['models'], [])

    def test_expiry_is_applied(self) -> None:
        """默认 7 天失效（主人明确要求的默认值）。"""
        from server import redpacket
        before = int(time.time())
        out = self._create(ttl_days=None)          # 用默认值
        expect = before + redpacket.DEFAULT_TTL_DAYS * 86400
        self.assertAlmostEqual(out['expires_at'], expect, delta=5)
        for k in out['keys']:
            self.assertEqual(k['expires_at'], out['expires_at'], '密钥要跟红包一起过期')

    def test_rolls_back_on_failure(self) -> None:
        """中途失败必须整体回滚，不能留下「没人知道出处」的孤儿密钥。

        造一个失败：把 create_key 换成会抛异常的版本。
        """
        from unittest import mock

        from server import db, keysvc
        calls = {'n': 0}
        real = keysvc.create_key

        def flaky(*a, **kw):
            calls['n'] += 1
            if calls['n'] == 3:
                raise RuntimeError('模拟第 3 个密钥创建失败')
            return real(*a, **kw)

        with mock.patch.object(keysvc, 'create_key', side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self._create(shares=5)

        self.assertEqual(db.query_one('SELECT COUNT(*) AS c FROM api_keys')['c'], 0,
                         '回滚后不该有任何密钥残留')
        self.assertEqual(db.query_one('SELECT COUNT(*) AS c FROM red_packets')['c'], 0)

    def test_revoke_disables_whole_batch(self) -> None:
        from server import redpacket
        out = self._create(shares=4)
        self.assertEqual(redpacket.revoke_packet(out['id']), 4)
        detail = redpacket.packet_detail(out['id'])
        self.assertTrue(all(not it['enabled'] for it in detail['items']))
        # 可逆：停用不是删除
        self.assertEqual(len(detail['items']), 4)

    def test_detail_never_exposes_plaintext(self) -> None:
        """详情接口不能回明文 key —— 库里只有哈希，界面上也不该有。"""
        from server import redpacket
        out = self._create(shares=3)
        detail = redpacket.packet_detail(out['id'])
        self.assertNotIn('key', detail)
        for it in detail['items']:
            self.assertNotIn('key', it)
            self.assertTrue(it['prefix'].startswith('wbk_'), '前缀要能对上密钥列表')

    def test_list_marks_revoked(self) -> None:
        from server import redpacket
        out = self._create(shares=2)
        self.assertFalse(redpacket.list_packets()[0]['revoked'])
        redpacket.revoke_packet(out['id'])
        self.assertTrue(redpacket.list_packets()[0]['revoked'])


class RouteTest(unittest.TestCase):
    """HTTP 层：权限、参数校验、错误码。

    这里不重复测业务逻辑（上面已覆盖），只测「路由有没有把门」——
    红包是**发放额度**的动作，权限漏一个就等于把发钱的口子敞开了。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from server import config, db, security
        from server.main import app

        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = config.DB_PATH
        config.DB_PATH = Path(cls._tmp.name) / 'rproute.db'
        db._conn = None
        db.connect()
        cls._app = app
        cls.c = TestClient(app)
        cls._security = security

        cls.ADMIN = {'username': 'admin', 'role': 'admin'}
        cls.VIEWER = {'username': 'guest', 'role': 'viewer'}

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        cls._app.dependency_overrides.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        from server import db
        db.execute('DELETE FROM red_packet_shares')
        db.execute('DELETE FROM red_packets')
        db.execute('DELETE FROM api_keys')
        self._app.dependency_overrides.clear()

    def tearDown(self) -> None:
        self._app.dependency_overrides.clear()

    def _as(self, who) -> None:
        """以某个身份请求。**只覆盖 `current_user`**。

        不要连 `require_admin` 一起覆盖 —— 那会把「本用例要测的权限检查」本身
        mock 掉：第一版两个都覆盖了，于是 viewer 创建红包返回 200，测试失败
        但其实是被测代码没问题、测试自己绕过了检查。
        `require_admin` 的实现是 `Depends(current_user)` + 判 role
        （security.py:515），所以只覆盖前者就能走到真实的 403。
        """
        self._app.dependency_overrides[self._security.current_user] = lambda: who

    def test_requires_admin_to_create(self) -> None:
        """创建红包必须是管理员 —— 它发出去的是真额度。"""
        self._app.dependency_overrides.clear()
        r = self.c.post('/api/red-packets', json={
            'title': 'x', 'quota_kind': KIND_CREDIT, 'total_amount': 100,
            'shares': 5, 'mode': MODE_LUCKY, 'ttl_days': 7})
        self.assertIn(r.status_code, (401, 403), '未认证不该能创建红包')

        self._as(self.VIEWER)
        r = self.c.post('/api/red-packets', json={
            'title': 'x', 'quota_kind': KIND_CREDIT, 'total_amount': 100,
            'shares': 5, 'mode': MODE_LUCKY, 'ttl_days': 7})
        self.assertEqual(r.status_code, 403, '查看者不该能创建红包')

    def test_admin_creates_and_gets_plaintext_once(self) -> None:
        self._as(self.ADMIN)
        r = self.c.post('/api/red-packets', json={
            'title': '给朋友', 'quota_kind': KIND_CREDIT, 'total_amount': 100,
            'shares': 3, 'mode': MODE_EVEN, 'ttl_days': 7})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body['keys']), 3)
        self.assertTrue(all(k['key'].startswith('wbk_') for k in body['keys']))

        # 详情不该再给明文。
        # 注意别用 `assertNotIn('key', ...)` —— 响应里有 `key_id` 字段，
        # 子串匹配会命中它（第一次写就这么错了）。要按**字段名**精确判，
        # 再用「prefix 是截断的」作为第二道：明文有 47 字符，prefix 只有 12。
        d = self.c.get(f"/api/red-packets/{body['id']}").json()
        self.assertNotIn('key', d, '详情顶层不该有 key 字段')
        for it in d['items']:
            self.assertNotIn('key', it, '每一份都不该有 key 字段')
            self.assertLessEqual(len(it['prefix']), 16,
                                 f"prefix 太长了，像是完整 token：{it['prefix']}")

    def test_bad_params_return_400(self) -> None:
        self._as(self.ADMIN)
        cases = [
            {'total_amount': 0.02, 'shares': 5},         # 总额不够分
            {'total_amount': 100, 'shares': 0},          # 份数非法
            {'total_amount': 100, 'shares': 999},        # 超上限
            {'total_amount': 100, 'shares': 5, 'quota_kind': 'money'},
            {'total_amount': 100, 'shares': 5, 'mode': 'random'},
            {'total_amount': 100, 'shares': 5, 'ttl_days': 0},
        ]
        for patch in cases:
            with self.subTest(**patch):
                body = {'title': 'x', 'quota_kind': KIND_CREDIT, 'total_amount': 100,
                        'shares': 5, 'mode': MODE_LUCKY, 'ttl_days': 7}
                body.update(patch)
                r = self.c.post('/api/red-packets', json=body)
                # 400 = 业务校验（redpacket.validate 抛的）
                # 422 = 模型层校验（pydantic 的 ge/le 先拦下了，如 shares=0）
                # 两者都是「被拒绝了」，只是拦截层不同 —— 断言不该绑定到某一层
                self.assertIn(r.status_code, (400, 422),
                              f'{patch} 应该被拒: {r.text}')

    def test_unknown_packet_is_404(self) -> None:
        self._as(self.ADMIN)
        self.assertEqual(self.c.get('/api/red-packets/999999').status_code, 404)
        self.assertEqual(
            self.c.post('/api/red-packets/999999/revoke').status_code, 404)

    def test_revoke_route(self) -> None:
        self._as(self.ADMIN)
        made = self.c.post('/api/red-packets', json={
            'title': 'x', 'quota_kind': KIND_TOKEN, 'total_amount': 500,
            'shares': 5, 'mode': MODE_LUCKY, 'ttl_days': 7,
            'models': ['glm-5.2']}).json()
        r = self.c.post(f"/api/red-packets/{made['id']}/revoke")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['revoked'], 5)

    def test_model_scope_rule_through_http(self) -> None:
        """模型范围的规则在 HTTP 层同样生效（两类相反）。

        这条走完整路由（不是直接调 validate），确保 400 真的是接口返回的
        而不是测试自己抛的。
        """
        self._as(self.ADMIN)
        base = {'title': 'x', 'total_amount': 100, 'shares': 5,
                'mode': MODE_LUCKY, 'ttl_days': 7}

        # token 不给模型 → 400
        r = self.c.post('/api/red-packets',
                        json={**base, 'quota_kind': KIND_TOKEN, 'total_amount': 1000})
        self.assertEqual(r.status_code, 400, f'token 红包不给模型应被拒: {r.text}')

        # 积分给了模型 → 400
        r = self.c.post('/api/red-packets',
                        json={**base, 'quota_kind': KIND_CREDIT, 'models': ['glm-5.2']})
        self.assertEqual(r.status_code, 400, f'积分红包带模型应被拒: {r.text}')


if __name__ == '__main__':
    unittest.main()
