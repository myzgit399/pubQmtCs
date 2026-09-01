#encoding:utf-8
"""
QMT 服务客户端：GUI 与 QmtSer.py 通信的唯一封装。

通信协议与 QmtSer 配套（QmtCS 式纯 socket，不使用 HTTP）：
  - QmtSer 在后台线程里阻塞 accept，每个连接独立线程处理，不依赖 handlebar。
  - 帧协议：每个请求/响应都是  [4字节大端长度][JSON 体]。
  - 顺序交互：client 发一帧 → server 回一帧 → client 读取并返回。

所有指令统一在此定义（对齐极致精简的 QmtSer）：
- trade 只传绝对数量 vol（GUI 已在本地算好 满/1/2/1/3/1/4）。
- cancel 支持 all(全撤)/buy(撤买)/sell(撤卖)/id(按委托号)。
- account 由 GUI 在 CMD 窗口录入后下发。
- quote / full_tick 获取行情；get_stock_list_in_sector 获取板块成份股。

对应指令函数一览：
  set_account, trade, query(asset/position/order), quote, full_tick,
  get_stock_list_in_sector, cancel, status, is_service_up
"""
import json
import socket
import struct

SER_HOST = '127.0.0.1'
SER_PORT = 8890
TIMEOUT = 5


class QmtError(Exception):
    pass


def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError('连接被远端断开')
        buf += part
    return buf


def _send_frame(sock, obj):
    body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    sock.sendall(struct.pack('>I', len(body)) + body)


def _recv_frame(sock):
    hdr = _recv_exact(sock, 4)
    n = struct.unpack('>I', hdr)[0]
    return json.loads(_recv_exact(sock, n).decode('utf-8', 'replace'))


def _raw_socket(op, params, timeout=TIMEOUT):
    """发一帧请求，收一帧响应。连接失败/协议失败都抛 QmtError。"""
    payload = dict(params or {})
    payload['op'] = op
    s = socket.create_connection((SER_HOST, SER_PORT), timeout=timeout)
    try:
        _send_frame(s, payload)
        return _recv_frame(s)
    except Exception as e:
        raise QmtError('无法连接 QMT 策略服务(%s:%d)：%s\n'
                       '请确认完整版 QMT 已启动 QmtSer 策略。' % (SER_HOST, SER_PORT, e))
    finally:
        try:
            s.close()
        except Exception:
            pass


def _call(op, params=None, timeout=TIMEOUT):
    result = _raw_socket(op, params, timeout=timeout)
    if result.get('ok') is False:
        raise QmtError(result.get('msg', '指令执行失败'))
    return result


# ---------------- 指令 ----------------

def set_account(accountid, accttype='STOCK'):
    return _call('account', {'accountid': accountid, 'accttype': accttype})


def trade(side, code, vol, price=None, strategy='gui', remark='',
          qtr=2, otype=1101):
    """下单。side:23买/24卖；vol 为 GUI 本地已算好的绝对股数。
    price: None/空=不指定(服务端会因无价格返回失败)；数字=限价；
    'market'=对手价；'limit'=涨跌停。取消 latest 兜底。"""
    return _call('trade', {'side': side, 'code': code, 'vol': int(vol),
                           'price': price, 'strategy': strategy,
                           'remark': remark, 'qtr': int(qtr), 'otype': int(otype)})


def query(kind, cancelable_only=False):
    p = {'kind': kind}
    if kind == 'order':
        p['cancelable_only'] = bool(cancelable_only)
    return _call('query', p)


def quote(codes):
    if isinstance(codes, str):
        codes = [codes]
    return _call('quote', {'codes': list(codes)})


def full_tick(codes):
    """获取全量 tick（对应服务端 get_full_tick）。

    codes: 代码或代码列表，可带或不带市场后缀('601005' / '601005.SH')。
    返回 {'ok':True,'data':{全代码: tick_dict, ...}}，tick_dict 为 QMT 原生
    full-tick 字段(数量字段可能为 list，各字段名与 QMT 一致)。
    """
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.split(',') if c.strip()]
    codes = [str(c).strip() for c in codes if str(c).strip()]
    return _call('tick', {'codes': list(codes)})


def get_stock_list_in_sector(sector):
    """获取指定板块的成份股代码列表（对应服务端 get_stock_list_in_sector）。

    sector: 板块名称，如 '沪深A股'。
    返回 {'ok':True,'data':[全代码, ...]}。
    """
    return _call('sector', {'sector': (sector or '').strip()})


def cancel(mode='all', order_id=None):
    """mode: all 全撤 / buy 撤所有买单 / sell 撤所有卖单 / id 按委托号(配合 order_id)。"""
    p = {'mode': mode}
    if mode == 'buy':
        p['side'] = 23
    elif mode == 'sell':
        p['side'] = 24
    if order_id is not None:
        # order_id 现在可能是字符串单号(sysid)，qmt ser 已兼容；不再强转 int。
        p['order_id'] = str(order_id)
    return _call('cancel', p)


def status(timeout=TIMEOUT):
    """服务状态。失败抛 QmtError。"""
    try:
        return _raw_socket('status', {}, timeout=timeout)
    except QmtError:
        raise
    except Exception as e:
        raise QmtError('无法连接服务(%s:%d)：%s' % (SER_HOST, SER_PORT, e))


def is_service_up(timeout=TIMEOUT):
    """检测 QmtSer 是否正常启动。拉取 status。"""
    try:
        st = status(timeout=timeout)
        return bool(st.get('run'))
    except Exception:
        return False
