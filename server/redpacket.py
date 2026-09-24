"""红包：一次创建 N 个「一份一个 key」的密钥，额度按拼手气或均分分配。

为什么叫红包而不是「批量建密钥」
--------------------------------
它解决的是**分发**场景：管理员一次拿到 N 个现成的 key，各自额度不同，
随手发给不同的人。手动建 N 个 key 要填 N 次表单（还要自己想额度怎么分），
红包只填两次——总额与份数。

为什么不做「领取页 / 分享码」
----------------------------
目标是**熟人分发**（同事、朋友），管理员自己把 key 发出去即可。加领取页
就要配套用户体系、登录校验、防刷（否则一个脚本就能扫空），成本远高于收益。
真要公开撒，那是另一套设计（需要手机号/邀请码级的风控），不该和这个混在一起。

两条设计约束值得先知道
----------------------
1. **key 明文只在此刻返回一次**（库里只存 hash + 前缀，与 create_key 一致）。
   所以结果必须一次性给全，且界面要提示「离开后无法再看到」——这是「直接发
   key」方案的固有代价，不是缺陷，但 UI 必须处理好。
2. **默认 7 天失效**。发出去的东西收不回，且熟人场景里常见「拿到就忘了」；
   给个默认期限让它们自动清理，比留一堆永不失效的 key 干净。
"""
from __future__ import annotations

import json
import secrets
import time

from . import db, keysvc

# 额度类别。两者的**限制对象不同**，不只是单位不同：
#   · credit —— 限制上游返回的真实扣费（usage.credit）。口径准：同样 1M token，
#     便宜模型与贵模型的实际扣费能差几十倍，按积分限才反映真实成本。
#   · token  —— 限制 prompt+completion 总数。直观、与客户端显示的数字一致，
#     但估不准花了多少钱。
# 所以两条路都要保留：管理员知道自己在发什么就行。
KIND_CREDIT = 'credit'
KIND_TOKEN = 'token'
KINDS = (KIND_CREDIT, KIND_TOKEN)

# 每份的最小额度。积分支持小数（上游 credit 本身就是小数，如 0.05），
# token 是整数。
MIN_UNIT = {KIND_CREDIT: 0.01, KIND_TOKEN: 1}

# 分配方式
MODE_LUCKY = 'lucky'   # 拼手气：随机，有人多有人少
MODE_EVEN = 'even'     # 均分：每份一样
MODES = (MODE_LUCKY, MODE_EVEN)

# 默认有效期（天）。见模块头注释第 2 条。
DEFAULT_TTL_DAYS = 7

# 单次红包的份数上限。上限存在的理由不是技术限制，而是**防手滑**：
# 一次建几千个 key 会把密钥列表刷爆，而管理员大概率是填错了。
MAX_SHARES = 100


class RedPacketError(ValueError):
    """参数不合法。路由层把它翻成 400。"""


def split_amount(total: float, shares: int, kind: str,
                 mode: str = MODE_LUCKY) -> list[float]:
    """把 total 分成 shares 份。调用方须先过 `validate`。

    拼手气用**二倍均值法**：每份的随机上限是「当前剩余均值 × 2」。

    为什么要设上限：不设的话第一份可能抽走绝大部分（均匀随机会这样），
    后面的人拿到接近 0——那是 bug 不是惊喜。上限保证了**越往后越稳**，
    同时保留随机性（有人多有人少，但不会有人什么都拿不到）。

    均分就是轮流发固定值，最后一份拿余数：浮点累加会有误差，
    让最后一份兜底才能保证**总和精确等于 total**（否则界面上「合计」
    与各份相加对不上，用户会以为少了）。
    """
    unit = MIN_UNIT[kind]
    decimals = 2 if kind == KIND_CREDIT else 0

    if mode == MODE_EVEN:
        each = round(total / shares, decimals)
        out = [each] * (shares - 1)
        out.append(round(total - each * (shares - 1), decimals))
        return out

    # 拼手气
    out: list[float] = []
    remaining = float(total)
    for i in range(shares - 1):
        left = shares - i
        # 上限 = 剩余均值 × 2；下限 = 最小单位。
        # 两者相等时（剩余刚好够每人一份最小值）不随机，直接取最小值，
        # 避免 uniform 的下界越界。
        hi = remaining / left * 2
        lo = unit
        if hi <= lo:
            amt = round(lo, decimals)
        else:
            amt = round(secrets.SystemRandom().uniform(lo, hi), decimals)
            # 舍入可能把 amt 顶到 hi 之上（或压到 lo 之下），钳一下：
            # 越界会让后面的人拿不到最小值，最后一份变成负数。
            amt = min(max(amt, lo), round(remaining - lo * (left - 1), decimals))
        out.append(amt)
        remaining = round(remaining - amt, decimals)
    out.append(round(remaining, decimals))
    return out


def validate(total: float, shares: int, kind: str, mode: str,
             ttl_days: int | None, models: list[str] | None = None) -> None:
    """校验创建参数。不合法时抛 RedPacketError（路由层翻成 400）。

    模型范围的规则**两类相反**，这不是疏漏：

      · **token 红包必须限定模型**。token 是「量」，与模型强相关——同一段
        上下文在不同模型下的 token 数、输出长度、上下文窗口都不同，不限定
        范围的话「10 万 token 红包」的含义是浮动的，收的人也不知道自己
        能拿它干什么。
      · **积分红包必须不限定**。积分是「钱」，按上游返回的真实扣费算，
        任何模型都能用；再叠一层模型限制只会让人算不清「这红包到底值多少」
        （想控成本就少发点积分）。所以传了模型反而拦下来，把语义钉死。
    """
    if kind not in KINDS:
        raise RedPacketError(f'额度类别只能是 {" 或 ".join(KINDS)}')
    if mode not in MODES:
        raise RedPacketError(f'分配方式只能是 {" 或 ".join(MODES)}')

    names = [str(m).strip() for m in (models or []) if str(m).strip()]
    if kind == KIND_TOKEN:
        if not names:
            raise RedPacketError('Token 红包必须限定模型范围（token 数与模型强相关）')
    else:
        if names:
            raise RedPacketError('积分红包不限制模型：按真实扣费计，任何模型都能用；'
                                 '想控成本请调小总额')

    unit = MIN_UNIT[kind]
    if not isinstance(shares, int) or isinstance(shares, bool) or shares < 1:
        raise RedPacketError('份数必须是正整数')
    if shares > MAX_SHARES:
        raise RedPacketError(f'份数最多 {MAX_SHARES} 份（一次建太多会把密钥列表刷爆）')

    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        raise RedPacketError('总额必须大于 0')
    # 关键约束：分到每份不能低于最小单位。
    # 不拦的话 split_amount 会产出 0 或负数的份额，那些 key 等于一建出来
    # 就超限（配额 0 = 不限！见 keysvc 的语义），反而变成无限额度的钥匙。
    if total < unit * shares:
        raise RedPacketError(
            f'总额太小：{shares} 份每份至少 {unit}，合计至少 {unit * shares}')

    if ttl_days is not None:
        if not isinstance(ttl_days, int) or isinstance(ttl_days, bool) or ttl_days < 1:
            raise RedPacketError('有效期必须是不小于 1 的天数')
        if ttl_days > 3650:
            raise RedPacketError('有效期最长 10 年')


def create_packet(name: str, kind: str, total: float, shares: int,
                  mode: str, ttl_days: int | None, actor: str,
                  models: list[str] | None = None) -> dict:
    """创建红包：生成 shares 个密钥 + 记录这一批。返回含**明文 key** 的结果。

    整批写在**同一个事务**里：中途失败就整体回滚，不会留下「几个 key 建好了、
    红包记录却没有」的半成品——那些 key 没人知道是红包发的，也就收不回来。

    `models`：token 红包必填、积分红包必须为空（见 `validate`）。
    """
    validate(total, shares, kind, mode, ttl_days, models)

    amounts = split_amount(total, shares, kind, mode)
    expires_at = int(time.time()) + (ttl_days or DEFAULT_TTL_DAYS) * 86400
    title = db._clean(name, 64) or '红包'
    # 归一化后再存：与密钥侧的白名单是**同一份**数据（都从用户输入来），
    # 两边规则不同的话，红包说限了 A、密钥实际限了 B，排查时会怀疑人生。
    model_list = [str(m).strip() for m in (models or []) if str(m).strip()] \
        if kind == KIND_TOKEN else []

    # 与 create_key 共用同一段 INSERT（见 keysvc.create_key 的 `_conn` 参数），
    # 不把 SQL 抄第二遍——两份 SQL 漂移过一次就够了（count_tokens 的鉴权）。
    conn = db.connect()
    created: list[dict] = []
    with db._lock:
        try:
            conn.execute('BEGIN')
            packet_id = conn.execute(
                'INSERT INTO red_packets(title, quota_kind, total_amount, shares, '
                'mode, models, created_by, created_at, expires_at) '
                'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (title, kind, float(total), shares, mode,
                 json.dumps(model_list), actor, int(time.time()), expires_at),
            ).lastrowid
            for i, amount in enumerate(amounts, start=1):
                made = keysvc.create_key(
                    name=f'{title}-{i}',
                    expires_at=expires_at,
                    models=model_list,      # 积分红包这里是 []（= 不限制）
                    quota=amount if kind == KIND_TOKEN else 0,
                    quota_credit=amount if kind == KIND_CREDIT else 0,
                    _conn=conn,          # ← 在同一个事务里，由本函数统一提交
                )
                conn.execute(
                    'INSERT INTO red_packet_shares(packet_id, key_id, amount) '
                    'VALUES(?, ?, ?)',
                    (packet_id, made['id'], float(amount)),
                )
                created.append(made)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        'id': packet_id,
        'title': title,
        'quota_kind': kind,
        'total_amount': float(total),
        'shares': shares,
        'mode': mode,
        'models': model_list,   # token 红包非空、积分红包恒为空
        'expires_at': expires_at,
        'created_at': int(time.time()),
        'keys': created,        # 含明文 key —— **仅此一次**
    }


def _models_of(raw: object) -> list[str]:
    """把库里存的 JSON 数组还原成列表；坏数据按空处理，不让它把接口打成 500。

    读接口不该因为一行脏数据整个挂掉——这是旁路展示数据，不是决策依据。
    """
    try:
        out = json.loads(str(raw or '[]'))
    except (TypeError, ValueError):
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


def list_packets() -> list[dict]:
    """红包列表（不含明文 key —— 库里本来也没有）。"""
    rows = db.query(
        'SELECT p.*, '
        '  (SELECT COUNT(*) FROM red_packet_shares s WHERE s.packet_id = p.id) AS shares_actual '
        'FROM red_packets p ORDER BY p.id DESC'
    )
    out = []
    for r in rows:
        used = db.query_one(
            'SELECT COUNT(*) AS n FROM red_packet_shares s '
            'JOIN api_keys k ON k.id = s.key_id '
            'WHERE s.packet_id = ? AND k.enabled = 0', (r['id'],))['n']
        out.append({
            'id': r['id'],
            'title': r['title'],
            'quota_kind': r['quota_kind'],
            'total_amount': float(r['total_amount']),
            'shares': int(r['shares']),
            'mode': r['mode'],
            'models': _models_of(r['models']),
            'created_by': r['created_by'],
            'created_at': int(r['created_at']),
            'expires_at': int(r['expires_at']),
            'revoked': int(used) >= int(r['shares']),
        })
    return out


def packet_detail(packet_id: int) -> dict | None:
    """红包详情：每一份的密钥与用量（仍不含明文）。"""
    p = db.query_one('SELECT * FROM red_packets WHERE id = ?', (packet_id,))
    if not p:
        return None
    rows = db.query(
        'SELECT s.amount, k.id AS key_id, k.prefix, k.enabled, k.expires_at, '
        '       k.used_tokens, k.used_credit '
        'FROM red_packet_shares s JOIN api_keys k ON k.id = s.key_id '
        'WHERE s.packet_id = ? ORDER BY s.id', (packet_id,))
    return {
        'id': p['id'],
        'title': p['title'],
        'quota_kind': p['quota_kind'],
        'total_amount': float(p['total_amount']),
        'shares': int(p['shares']),
        'mode': p['mode'],
        'models': _models_of(p['models']),
        'created_by': p['created_by'],
        'created_at': int(p['created_at']),
        'expires_at': int(p['expires_at']),
        'items': [{
            'key_id': r['key_id'],
            'prefix': r['prefix'],
            'amount': float(r['amount']),
            'enabled': bool(r['enabled']),
            'used_tokens': int(r['used_tokens']),
            'used_credit': float(r['used_credit']),
        } for r in rows],
    }


def revoke_packet(packet_id: int) -> int:
    """收回整批：停用这批 key。返回停用的数量。

    为什么是「停用」而不是「删除」：停用是可逆的（后悔了能放开），
    而且用量记录还在——删了就查不到「这批红包到底被用掉多少」。
    """
    rows = db.query(
        'SELECT k.id FROM red_packet_shares s JOIN api_keys k ON k.id = s.key_id '
        'WHERE s.packet_id = ? AND k.enabled = 1', (packet_id,))
    for r in rows:
        keysvc.update_key(int(r['id']), {'enabled': False})
    return len(rows)
