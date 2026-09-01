#encoding:utf-8
"""
QMT 策略端：交易执行服务（完整版 QMT 版，基于 workedser 通讯机制）

运行在【完整版 QMT 内置 Python】中，作为交易执行端。
通讯机制沿用已验证可行的 workedser 模式：
  - init(ContextInfo) 直接在主线程调用阻塞的 go(...)，永不返回。
    （不是后台线程、不依赖 handlebar——完整版 QMT 里只有这种“主线程阻塞
    accept”才能正常收发连接；后台线程能 bind 但收不到连接。）
  - go() 里 while True 长连接循环：客户端连上→读一帧→处理→回一帧。
    客户端关闭连接则断开，继续 accept 下一个。
  - 帧协议仍是 [4字节大端长度][UTF-8 JSON]。

策略端做几件事：设账号、按绝对数量下单、查询（资产/持仓/委托）、撤单、
获取行情（quote / 全量 tick）、获取板块成份股。所有 满/1/2/1/3/1/4 的
数量计算与比例估算都在 GUI 侧本地完成；本服务只根据收到的数据执行，
不做任何比例估算。

端点 / op 一览：
  {"op":"account","accountid":"...","accttype":"STOCK"}   设置账号
  {"op":"trade","side":23|24,"code":"601005","vol":100,"price":"latest"|
       "market"|"limit"|数字, "strategy":..., "remark":..., "qtr":2,"otype":1101}  下单(vol必填绝对股数)
  {"op":"query","kind":"asset"|"position"|"order","cancelable_only":true}
  {"op":"quote","codes":["601005","000001"]}   最新价 + 买卖二档(与账号无关)
  {"op":"tick","codes":["601005","000001"]}   全量 tick(get_full_tick 载体，返回全代码 key)
  {"op":"sector","sector":"沪深A股"}   板块成份股代码列表(get_stock_list_in_sector 载体)
  {"op":"cancel","mode":"all"|"buy"|"sell"|"id","order_id":123}
  {"op":"status"}   服务状态

账号由 GUI 侧在 CMD 窗口录入后通过 account 指令下发，本文件不写死。

QMT 关键 API 签名（依据大QMT内置端权威文档 docs/api_mapping.md + 模板）：
  - 账号：界面选定后注入【全局变量 account / accountType(STOCK/CREDIT)】，
    代码直接引用（_read_account() 优先取它），不要自定义同名覆盖。
  - 委托合同号字段：m_strOrderSysID（注意结尾大写 ID）
  - 可撤判断：can_cancel_order(单号, account, 'stock')  返回是否可撤
  - 撤单：cancel(单号, account, accountType, C)  四参，单号在前，末尾带 ContextInfo
  - 下单：passorder(opType, 1101, account, code, prType, price, vol,
            strategy, 2, userOrderId, C)  账号用注入的全局 account
  - 查询：get_trade_detail_data(account, accountType, 'order'/'position'/'account')
"""
import os
import time
import json
import socket
import struct

PY3 = True

HOST = '127.0.0.1'
PORT = int(os.environ.get('QMT_SER_PORT', '8890'))

ACCOUNT_ID = ''            # 账号由 account 指令下发，不写死
ACCOUNT_TYPE = 'STOCK'
_ctx_holder = {'ctx': None}
stats = {'cmds': 0, 'orders': 0, 'cancels': 0, 'ticks': 0, 'sectors': 0,
         'started': ''}


def _ctx():
    return _ctx_holder['ctx']


def _read_account():
    """读取委托/撤单/下单用的账号。

    大QMT内置端权威做法（docs/api_mapping.md / SKILL.md）：账号由界面选定后
    注入为【全局变量 account / accountType】，代码直接引用、不要自定义同名覆盖。
    这里优先取注入的全局，退回自设置的 ACCOUNT_ID/ACCOUNT_TYPE。
    """
    try:
        g_acct = globals().get('account') or ''
        g_type = globals().get('accountType') or ''
        if g_acct:
            return str(g_acct).strip(), (str(g_type).strip().upper() or 'STOCK')
    except Exception:
        pass
    return ACCOUNT_ID, (ACCOUNT_TYPE or 'STOCK').upper()


def _have_account():
    aid, _ = _read_account()
    return bool(aid)


def _market_of(code):
    """按首位推断市场后缀：6/5=SH，4/8/9=BJ，其余=SZ。"""
    p = code[0] if code else ''
    if p in ('6', '5'):
        return 'SH'
    if p in ('4', '8', '9'):
        return 'BJ'
    return 'SZ'


def _full_code(code):
    code = (code or '').strip()
    if not code:
        return ''
    if '.' in code:
        return code.upper()
    return code + '.' + _market_of(code)


# ===================== 指令处理 =====================

def _cmd_account(p):
    global ACCOUNT_ID, ACCOUNT_TYPE
    acct = (p.get('accountid') or '').strip()
    if not acct:
        return {'ok': False, 'msg': 'account 缺少 accountid'}
    ACCOUNT_ID = acct
    ACCOUNT_TYPE = (p.get('accttype') or 'STOCK').strip() or 'STOCK'
    try:
        if _ctx() is not None and hasattr(_ctx(), 'set_account'):
            _ctx().set_account(ACCOUNT_ID)
        return {'ok': True, 'msg': '账号设置成功'}
    except Exception as e:
        return {'ok': False, 'msg': 'set_account 失败: %s' % e}


def _cmd_trade(p):
    """只按绝对数量 vol 开/平仓。所有比例计算已在 GUI 本地完成。"""
    aid, atype = _read_account()
    if not aid:
        return {'ok': False, 'msg': '尚未设置账号'}
    ctx = _ctx()
    if ctx is None:
        return {'ok': False, 'msg': 'ContextInfo 未初始化'}
    side = int(p.get('side') or 0)
    if side not in (23, 24):
        return {'ok': False, 'msg': 'side 必须为 23(买) 或 24(卖)'}
    code_qmt = _full_code(p.get('code') or '')
    if not code_qmt:
        return {'ok': False, 'msg': '缺少 code'}
    try:
        vol = int(p.get('vol') or 0)
    except Exception:
        vol = 0
    if vol <= 0:
        return {'ok': False, 'msg': 'vol 必须为正整数股数'}
    vol = vol // 100 * 100
    if vol <= 0:
        return {'ok': False, 'msg': 'vol 不足一手(100股)'}

    pr = (p.get('price') or '').strip().lower()
    strategy = (p.get('strategy') or 'gui').strip()
    remark = (p.get('remark') or '').strip()
    qtr = int(p.get('qtr', 2) or 2)
    otype = int(p.get('otype', 1101) or 1101)
    # 取消 latest 兜底：必须显式给出价格方式（对手价/涨跌停/具体限价），
    # 缺价格或 latest 一律判为无效，避免柜台悄悄按最新价撮合。
    if pr in ('', 'latest'):
        return {'ok': False, 'msg': '未指定价格方式（price 必填，可用 market/limit/数字限价）'}
    if pr == 'market':
        pr_type, price = 14, 0
    elif pr == 'limit':
        pr_type, price = 15, 0
    else:
        try:
            pr_type, price = 11, float(pr)
        except Exception:
            return {'ok': False, 'msg': 'price 格式无效: %s' % pr}

    try:
        passorder(side, otype, aid, code_qmt, pr_type, price, vol,
                  strategy, qtr, remark, ctx)
        stats['orders'] += 1
        return {'ok': True, 'code': code_qmt, 'side': side, 'volume': vol}
    except Exception as e:
        return {'ok': False, 'msg': 'passorder 异常: %s' % e}


def _cmd_query(p):
    if not _have_account():
        return {'ok': False, 'msg': '尚未设置账号'}
    kind = (p.get('kind') or '').strip().lower()
    aid, atype = _read_account()
    try:
        if kind == 'asset':
            data = [{'总资产': round(float(getattr(a, 'm_dBalance', 0) or 0), 2),
                     '可用金额': round(float(getattr(a, 'm_dAvailable', 0) or 0), 2),
                     '总市值': round(float(getattr(a, 'm_dInstrumentValue', 0) or 0), 2)}
                    for a in get_trade_detail_data(aid, atype, 'ACCOUNT')]
            return {'ok': True, 'kind': 'asset', 'data': data}
        if kind == 'position':
            data = [{'code': getattr(q, 'm_strInstrumentID', ''),
                     'exchange': getattr(q, 'm_strExchangeID', ''),
                     'name': getattr(q, 'm_strInstrumentName', ''),
                     'volume': int(getattr(q, 'm_nVolume', 0) or 0),
                     'can_use': int(getattr(q, 'm_nCanUseVolume', 0) or 0),
                     'cost': round(float(getattr(q, 'm_dOpenPrice', 0) or 0), 4),
                     'market_value': round(float(getattr(q, 'm_dInstrumentValue', 0) or 0), 2),
                     'profit': round(float(getattr(q, 'm_dPositionProfit', 0) or 0), 2)}
                    for q in get_trade_detail_data(aid, atype, 'POSITION')]
            return {'ok': True, 'kind': 'position', 'data': data}
        if kind == 'order':
            only = bool(p.get('cancelable_only', False))
            data = []
            for o in get_trade_detail_data(aid, atype, 'ORDER'):
                status = int(getattr(o, 'm_nOrderStatus', 0))
                # 订单合同号：真实字段是 m_strOrderSysID（注意大写 ID）。
                # m_nOrderSysid / m_nOrderID 常为 0，不能用作撤单号。
                s_oid = str(getattr(o, 'm_strOrderSysID', '') or '')
                # 可撤判断：用 QMT 自带的 can_cancel_order(单号, 账号, 'stock')。
                # 这比自维护状态集合(48/49/50/55/255)更可靠——未成交/部分成交
                # 等真正可撤的状态才返回给 GUI。
                cancelable = 0
                try:
                    cancelable = int(bool(can_cancel_order(s_oid, aid, 'stock')))
                except Exception:
                    cancelable = 0
                if only and not cancelable:
                    continue
                side = _order_side(o)
                data.append({'code': getattr(o, 'm_strInstrumentID', ''),
                             'exchange': getattr(o, 'm_strExchangeID', ''),
                             'name': getattr(o, 'm_strInstrumentName', ''),
                             'order_id': s_oid,
                             'side': side,
                             # 委托量/成交量/成交价按权威 api_mapping.md 字段名取，
                             # 并用多候选兜底不同版本命名差异（如 m_nOrderVolume 旧名）。
                             'volume': int(_attr(o, ('m_nVolumeTotalOriginal', 'm_nOrderVolume'), 0) or 0),
                             'price': round(float(_attr(o, 'm_dLimitPrice', 0) or 0), 2),
                             'traded_volume': int(_attr(o, ('m_nVolumeTraded', 'm_nTradedVolume'), 0) or 0),
                             'traded_avgprice': round(float(_attr(o, ('m_dTradedPrice', 'm_dTradedAvgPrice'), 0) or 0), 2),
                             'status': status,
                             'status_text': _order_status_text(status),
                             'cancelable': cancelable})
            return {'ok': True, 'kind': 'order', 'data': data}
        return {'ok': False, 'msg': '未知 kind: %s' % kind}
    except Exception as e:
        return {'ok': False, 'msg': '查询异常: %s' % e}


_STATE_TEXT = {
    48: '未报', 49: '待报', 50: '已报', 51: '已报待撤', 52: '部成待撤',
    53: '部撤', 54: '已撤', 55: '部成', 56: '已成', 57: '废单', 58: '未知',
    255: '已报待撤/未知',
}


def _order_status_text(status):
    return _STATE_TEXT.get(int(status or 0), str(status))




def _order_side(o):
    """委托买卖方向（依据大QMT权威 api_mapping.md 字段映射）：
      优先按 m_nOpType(23/33买、24/34卖，27融资买、28融券卖)；
      否则按 m_nOffsetFlag(48买 / 49卖)；
      最后按方向文本 m_strDirection('B'/'S')。
      （注意：之前误把 m_nOffsetFlag 当 0开/1平 用，会把卖单判成买——已修正。）
    """
    op = int(getattr(o, 'm_nOpType', 0) or 0)
    if op in (23, 27, 33):
        return 23
    if op in (24, 28, 34):
        return 24
    off = int(getattr(o, 'm_nOffsetFlag', 0) or 0)
    if off == 48:
        return 23
    if off == 49:
        return 24
    dir_ = getattr(o, 'm_strDirection', '') or getattr(o, 'm_direction', '')
    if dir_ in ('B', 'BUY', '买'):
        return 23
    if dir_ in ('S', 'SELL', '卖'):
        return 24
    return 23


def _attr(o, keys, default=0):
    """从对象 o 依次取多个候选字段中第一个不为 None 的值。
    兼容不同版本 QMT 对象字段名差异；都取不到返回 default。"""
    if not isinstance(keys, (list, tuple)):
        keys = (keys,)
    for k in keys:
        try:
            v = getattr(o, k, None)
        except Exception:
            v = None
        if v is not None:
            return v
    return default


def _get_attr(d, key):
    try:
        if isinstance(d, dict):
            return d.get(key)
        return getattr(d, key, None)
    except Exception:
        return None


def _num(d, key):
    try:
        return float(_get_attr(d, key) or 0)
    except Exception:
        return 0.0


def _num_list(d, key, idx):
    try:
        v = _get_attr(d, key)
        v = v[idx] if isinstance(v, (list, tuple)) else None
        return float(v or 0)
    except Exception:
        return 0.0


def _cmd_quote(p):
    raw = p.get('codes') or []
    if isinstance(raw, str):
        raw = [c.strip() for c in raw.split(',') if c.strip()]
    else:
        raw = [str(c).strip() for c in raw if str(c).strip()]
    codes = [_full_code(c) for c in raw]
    if not codes:
        return {'ok': False, 'msg': '缺少 codes'}
    try:
        # 取 get_full_tick：优先用 ContextInfo 的方法（QMT 里正确方式），
        # 否则退回到全局同名函数；两者都没有则返回空。
        ctx = _ctx()
        ticker = None
        if ctx is not None and callable(getattr(ctx, 'get_full_tick', None)):
            ticker = ctx.get_full_tick
        else:
            g = globals().get('get_full_tick')
            if callable(g):
                ticker = g
        ticks = ticker(codes) if ticker else {}
        data = []
        for i, c in enumerate(codes):
            d = _get_attr(ticks, c) or {}
            price = _num(d, 'lastPrice')
            # 盘后/无最新成交时 lastPrice 可能为 0，退回到昨收/今开等参考价，
            # 避免 GUI 现价一直显示 0(非交易时段的常态)。
            if price <= 0:
                price = (_num(d, 'lastClose') or _num(d, 'preClose')
                         or _num(d, 'openPrice') or 0)
            bid2 = (_num(d, 'bidPrice2') or _num(d, 'b2Price')
                    or _num_list(d, 'bidPrice', 2) or price)
            ask2 = (_num(d, 'askPrice2') or _num(d, 'a2Price')
                    or _num_list(d, 'askPrice', 2) or price)
            # 涨跌停价：QMT 字段名可能是 upLimit/downLimit 或 highLimit/lowLimit，
            # 用于买卖二在涨跌停时取不到时的兜底（用涨跌停价挂单才"抢"得到）。
            up_limit = (_num(d, 'upLimit') or _num(d, 'highLimit') or 0)
            down_limit = (_num(d, 'downLimit') or _num(d, 'lowLimit') or 0)
            data.append({'code': raw[i], 'price': round(price, 3),
                         'bid2': round(float(bid2 or 0), 3),
                         'ask2': round(float(ask2 or 0), 3),
                         'up_limit': round(float(up_limit or 0), 3),
                         'down_limit': round(float(down_limit or 0), 3)})
        return {'ok': True, 'data': data}
    except Exception as e:
        return {'ok': False, 'msg': '行情查询异常'}


def _cmd_tick(p):
    """获取全量 Tick（get_full_tick 的载体）。

    优先用 ContextInfo.get_full_tick(codes)（完整版 QMT 内置策略的正确方式），
    退回全局同名函数 get_full_tick。返回按【全代码】(带市场后缀) 为 key 的
    {code: tick_dict}，tick_dict 的字段结构即 QMT 原生 full-tick 字段
    （lastPrice / lastClose / bidPrice[5] / askPrice[5] / upLimit / downLimit 等）。
    """
    raw = p.get('codes') or []
    if isinstance(raw, str):
        raw = [c.strip() for c in raw.split(',') if c.strip()]
    else:
        raw = [str(c).strip() for c in raw if str(c).strip()]
    codes = [_full_code(c) for c in raw]
    codes = [c for c in codes if c]
    if not codes:
        return {'ok': False, 'msg': '缺少 codes'}
    try:
        ctx = _ctx()
        ticker = None
        if ctx is not None and callable(getattr(ctx, 'get_full_tick', None)):
            ticker = ctx.get_full_tick
        else:
            g = globals().get('get_full_tick')
            if callable(g):
                ticker = g
        ticks = ticker(codes) if ticker else {}
        data = {}
        for c in codes:
            d = ticks.get(c) if hasattr(ticks, 'get') else None
            data[c] = d or {}
        stats['ticks'] += 1
        return {'ok': True, 'data': data}
    except Exception as e:
        return {'ok': False, 'msg': 'tick 查询异常: %s' % e}


def _cmd_sector(p):
    """获取指定板块的成份股代码列表（get_stock_list_in_sector 的载体）。

    优先用 ContextInfo.get_stock_list_in_sector(板块名)，退回全局同名函数。
    返回【全代码】(带市场后缀) 的列表，如 ['600000.SH', '000001.SZ', ...]。
    """
    name = (p.get('sector') or '').strip()
    if not name:
        return {'ok': False, 'msg': '缺少 sector'}
    try:
        ctx = _ctx()
        fn = None
        if ctx is not None and callable(getattr(ctx, 'get_stock_list_in_sector', None)):
            fn = ctx.get_stock_list_in_sector
        else:
            g = globals().get('get_stock_list_in_sector')
            if callable(g):
                fn = g
        if fn is None:
            return {'ok': False, 'msg': '当前运行环境不支持板块查询'}
        result = fn(name)
        result = [_full_code(c) for c in (result or []) if c]
        result = [c for c in result if c]
        stats['sectors'] += 1
        return {'ok': True, 'data': result}
    except Exception as e:
        return {'ok': False, 'msg': '板块查询异常: %s' % e}


def _cancel_side(side):
    """服务端按买卖方向撤单：遍历全部委托，取出该方向所有可撤订单号逐个撤销。"""
    if not _have_account():
        return {'ok': False, 'msg': '尚未设置账号'}
    aid, atype = _read_account()
    n = 0
    for o in get_trade_detail_data(aid, atype, 'ORDER'):
        # 订单合同号：真实字段是 m_strOrderSysID（大写 ID）。
        oid = str(getattr(o, 'm_strOrderSysID', '') or '')
        if not oid:
            continue
        # 可撤判断：用 QMT 自带 can_cancel_order，未成交/部分成交等才可撤。
        try:
            if not can_cancel_order(oid, aid, 'stock'):
                continue
        except Exception:
            continue
        if side is not None:
            if _order_side(o) != side:
                continue
        try:
            cancel(oid, aid, atype, _ctx())
            n += 1
        except Exception:
            pass
    stats['cancels'] += n
    return n


def _cmd_cancel(p):
    mode = (p.get('mode') or 'all').strip().lower()
    if mode == 'all':
        n = _cancel_side(None)
        return {'ok': True, 'msg': '全撤完成，撤销 %d 笔' % n, 'n': n}
    if mode in ('buy', 'sell'):
        side = 23 if mode == 'buy' else 24
        n = _cancel_side(side)
        txt = '撤买' if side == 23 else '撤卖'
        return {'ok': True, 'msg': '%s完成，撤销 %d 笔' % (txt, n), 'n': n}
    if mode == 'id':
        # order_id 现在可能是字符串单号(sysid)；兼容 int。
        oid = str(p.get('order_id') or '').strip()
        if not oid or oid == '0':
            return {'ok': False, 'msg': '缺少有效 order_id'}
        try:
            aid, atype = _read_account()
            cancel(oid, aid, atype, _ctx())
            stats['cancels'] += 1
            return {'ok': True, 'msg': '已撤单 %s' % oid, 'n': 1}
        except Exception as e:
            return {'ok': False, 'msg': '撤单异常: %s' % e}
    return {'ok': False, 'msg': '未知撤单模式: %s(仅支持 all/buy/sell/id)' % mode}


def _dispatch(p):
    op = (p.get('op') or '').strip().lower()
    stats['cmds'] += 1
    try:
        if op == 'account':
            return _cmd_account(p)
        if op == 'trade':
            return _cmd_trade(p)
        if op == 'query':
            return _cmd_query(p)
        if op == 'quote':
            return _cmd_quote(p)
        if op == 'tick':
            return _cmd_tick(p)
        if op == 'sector':
            return _cmd_sector(p)
        if op == 'cancel':
            return _cmd_cancel(p)
        if op == 'status':
            return {'op': 'status', 'run': True, 'started': stats['started'],
                    'cmds': stats['cmds'], 'orders': stats['orders'],
                    'cancels': stats['cancels'], 'ticks': stats['ticks'],
                    'sectors': stats['sectors'],
                    'account_set': bool(ACCOUNT_ID)}
        return {'ok': False, 'msg': '未知 op: %s' % op}
    except Exception as e:
        return {'ok': False, 'msg': '处理异常: %s' % e}


# ===================== 通讯服务（主线程阻塞，workedser 机制） =====================
# 关键约束（完整版 QMT 内置策略多轮实测）：后台线程的 socket 服务能 bind/listen
# 但收不到连接；只有【主线程阻塞 accept】才能正常收发。因此这里不用后台线程、
# 不用 select、不用 handlebar，直接在 init 里阻塞跑 go()。

def recv_exact(conn, n):
    buf = b''
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            raise ConnectionError('对端断开')
        buf += part
    return buf


def go(host=HOST, port=PORT):
    """阻塞监听+回复（主线程跑，永不返回）。供 QMT 的 init() 直接调用。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)

    while True:
        conn, addr = srv.accept()
        try:
            # 长连接：一个客户端连上后可连续发多条，每帧一答。
            # 客户端断开时 recv_exact 抛 ConnectionError，自动收尾。
            conn.settimeout(300)   # 空闲超时 5 分钟
            while True:
                hdr = recv_exact(conn, 4)
                n = struct.unpack('>I', hdr)[0]
                body = recv_exact(conn, n)
                result = _dispatch_json(body)
                out = json.dumps(result, ensure_ascii=False).encode('utf-8')
                conn.sendall(struct.pack('>I', len(out)) + out)
        except socket.timeout:
            pass
        except ConnectionError:
            pass
        except Exception as e:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _dispatch_json(raw):
    try:
        p = json.loads(raw.decode('utf-8', 'replace')) if raw.strip() else {}
    except Exception as e:
        return {'ok': False, 'msg': '解析异常: %s' % e}
    return _dispatch(p if isinstance(p, dict) else {})


# ===================== QMT 生命周期入口 =====================

def init(ContextInfo):
    global _ctx_holder
    _ctx_holder['ctx'] = ContextInfo
    stats['started'] = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        go(HOST, PORT)   # 阻塞，主线程跑服务，永不返回
    except Exception as e:
        pass


def stop(ContextInfo):
    pass
