#encoding:utf-8
# -*- coding: utf-8 -*-
"""
echo_client.py —— 菜单式指令测试工具 / 自主 Python 与 QMT 策略互动的样例程序。

本脚本在【QMT 策略环境之外】的自主 Python 里运行，通过 qmt_client 与内嵌在
完整版 QMT 里的 QmtSer.py 策略通信，逐条测试协议中支持的每一个指令，也是
开发者复制改写自己代码的现成样例。

用法：
  python echo_client.py [端口]

通过列表菜单选择指令，每个指令都提供『默认选项』，直接回车即用默认值，
输入则覆盖。每执行完一个指令后循环回到菜单，输入 0 或 q 退出。

菜单指令与默认值：
  1  设置账号                 提示输入资金账号(可带默认类型 STOCK)
  2  获取代码（板块成份股）     默认板块：沪深A股
  3  获取行情（最新价+买卖二档） 默认代码：重庆钢铁(601005)
  4  获取全量Tick(get_full_tick) 默认代码：重庆钢铁(601005)
  5  获取持仓
  6  获取资产
  7  获取订单                   默认仅显示可撤销订单：否
  8  下买单                     默认代码 重庆钢铁(601005)，数量 100 股，对手价
  9  下卖单                     默认数量 100 股，对手价
  10 撤销指定订单               提示输入委托单号(必须输入)
  11 撤销所有订单
  12 查看服务状态
  0/q 退出

说明：下单/撤单/查询资产/持仓/订单需要先在 QMT 侧设置账号；
     行情(quote/get_full_tick)与板块成份股与账号无关，可随时测试。
"""
import os
import sys

# 将脚本所在目录加入 path，确保能 import 同目录的 qmt_client。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qmt_client as Q

# 常用默认值
DEFAULT_PORT = 8890
DEFAULT_CODE = '601005'      # 重庆钢铁
DEFAULT_SECTOR = '沪深A股'    # 默认板块
DEFAULT_VOL = 100            # 默认股数
DEFAULT_PRICE = 'market'     # 默认价格方式：对手价

# 买卖方向常量（与 QMT passorder 一致）
SIDE_BUY = 23
SIDE_SELL = 24


# ---------------- 交互辅助 ----------------

def _ask(prompt, default=None):
    """输出提示，回车用默认值，否则用输入值。返回 str。"""
    if default is not None:
        full = '%s [默认: %s] > ' % (prompt, default)
    else:
        full = '%s > ' % prompt
    try:
        inp = input(full)
    except (EOFError, KeyboardInterrupt):
        return None
    inp = inp.strip()
    if inp == '':
        return str(default) if default is not None else ''
    return inp


def _ask_int(prompt, default=None):
    """取整数输入，非法时回退默认值。"""
    v = _ask(prompt, default)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return default


# ---------------- 指令实现（每个对应一个菜单项） ----------------

def do_status():
    st = Q.status()
    print('[结果] 服务状态: run=%s started=%s' % (st.get('run'), st.get('started')))
    print('       累计指令=%s 下单=%s 撤单=%s tick=%s 板块=%s 已设账号=%s'
          % (st.get('cmds'), st.get('orders'), st.get('cancels'),
             st.get('ticks'), st.get('sectors'), st.get('account_set')))


def do_sector():
    name = _ask('板块名称', DEFAULT_SECTOR)
    if name is None:
        return
    r = Q.get_stock_list_in_sector(name)
    data = r.get('data') or []
    print('[结果] 板块[%s] 共 %d 只:' % (name, len(data)))
    # 分块打印，避免终端刷屏
    for i in range(0, len(data), 20):
        print('       ' + ', '.join(data[i:i + 20]))


def do_quote():
    codes = _ask('代码(逗号分隔)', DEFAULT_CODE)
    if codes is None:
        return
    r = Q.quote(codes)
    print('[结果]')
    for d in r.get('data') or []:
        print('       %s  最新价=%s  买二=%s 卖二=%s 涨停=%s 跌停=%s'
              % (d.get('code'), d.get('price'), d.get('bid2'), d.get('ask2'),
                 d.get('up_limit'), d.get('down_limit')))


def do_full_tick():
    codes = _ask('代码(逗号分隔)', DEFAULT_CODE)
    if codes is None:
        return
    r = Q.full_tick(codes)
    data = r.get('data') or {}
    print('[结果]')
    for code, t in data.items():
        # 挑选常用字段展示；t 为 QMT 原生字段
        last = t.get('lastPrice') if isinstance(t, dict) else None
        lastc = t.get('lastClose') if isinstance(t, dict) else None
        vol = t.get('volume') if isinstance(t, dict) else None
        amt = t.get('amount') if isinstance(t, dict) else None
        print('       %s  lastPrice=%s  lastClose=%s  volume=%s  amount=%s'
              % (code, last, lastc, vol, amt))
        if isinstance(t, dict) and ('bidPrice' in t or 'askPrice' in t):
            print('           买一~五=%s 卖一~五=%s'
                  % (t.get('bidPrice'), t.get('askPrice')))


def do_position():
    r = Q.query('position')
    data = r.get('data') or []
    print('[结果] 共 %d 条持仓:' % len(data))
    for p in data:
        print('       %s(%s) %s  持仓=%s 可用=%s 成本=%s 市值=%s 浮盈=%s'
              % (p.get('code'), p.get('exchange'), p.get('name'),
                 p.get('volume'), p.get('can_use'), p.get('cost'),
                 p.get('market_value'), p.get('profit')))


def do_asset():
    r = Q.query('asset')
    data = r.get('data') or []
    print('[结果] 共 %d 条资产:' % len(data))
    for a in data:
        print('       总资产=%s 可用金额=%s 总市值=%s'
              % (a.get('总资产'), a.get('可用金额'), a.get('总市值')))


def do_order():
    only = _ask_int('仅显示可撤销订单(1=是/0=否)', '0')
    if only is None:
        return
    r = Q.query('order', cancelable_only=bool(only))
    data = r.get('data') or []
    print('[结果] 共 %d 条委托:' % len(data))
    for o in data:
        print('       %s %s %s 单号=%s 方向=%s 量=%s 价=%s 已成交=%s 均价=%s 状态=%s 可撤=%s'
              % (o.get('code'), o.get('exchange'), o.get('name'), o.get('order_id'),
                 o.get('side'), o.get('volume'), o.get('price'),
                 o.get('traded_volume'), o.get('traded_avgprice'),
                 o.get('status_text'), o.get('cancelable')))


def _do_trade(side):
    if side == SIDE_BUY:
        title = '下买单'
        code_def = DEFAULT_CODE
        code_prompt = '买入代码'
    else:
        title = '下卖单'
        code_def = None
        code_prompt = '卖出代码'
    print('== %s ==' % title)
    code = _ask(code_prompt, code_def)
    if code is None:
        return
    vol = _ask_int('数量(股)', DEFAULT_VOL)
    if vol is None:
        return
    price = _ask('价格方式(market=对手价/limit=涨跌停/数字=限价)', DEFAULT_PRICE)
    if price is None:
        return
    ack = Q.trade(side, code, vol, price=price or None, strategy='echo',
                  remark='echo_client')
    print('[结果] %s 已报: code=%s side=%s volume=%s'
          % (title, ack.get('code'), ack.get('side'), ack.get('volume')))


def do_buy():
    _do_trade(SIDE_BUY)


def do_sell():
    _do_trade(SIDE_SELL)


def do_cancel_order():
    print('== 撤销指定订单 ==')
    oid = _ask('委托单号(必填)')
    if oid is None:
        return
    if not oid.strip():
        print('[提示] 委托单号为空，已取消。')
        return
    r = Q.cancel(mode='id', order_id=oid.strip())
    print('[结果] %s' % (r.get('msg') or 'ok'))


def do_cancel_all():
    print('== 撤销所有订单 ==')
    r = Q.cancel(mode='all')
    print('[结果] %s' % (r.get('msg') or 'ok'))


def do_set_account():
    print('== 设置账号 ==')
    aid = _ask('资金账号')
    if aid is None:
        return
    if not aid.strip():
        print('[提示] 账号为空，已取消。')
        return
    accttype = _ask('类型(STOCK=股票/CREDIT=信用)', 'STOCK')
    if accttype is None:
        return
    accttype = (accttype.strip().upper() or 'STOCK')
    r = Q.set_account(aid.strip(), accttype)
    print('[结果] %s' % (r.get('msg') or 'ok'))


# ---------------- 菜单 ----------------

MENU = [
    ('设置账号', do_set_account),
    ('获取代码（板块成份股）', do_sector),
    ('获取行情（最新价+买卖二档）', do_quote),
    ('获取全量Tick (get_full_tick)', do_full_tick),
    ('获取持仓', do_position),
    ('获取资产', do_asset),
    ('获取订单', do_order),
    ('下买单', do_buy),
    ('下卖单', do_sell),
    ('撤销指定订单', do_cancel_order),
    ('撤销所有订单', do_cancel_all),
    ('查看服务状态', do_status),
]


def print_menu():
    print('\n==================== QMT 指令测试菜单 ====================')
    print('  服务端口: %s:%d' % (Q.SER_HOST, Q.SER_PORT))
    for i, (label, _) in enumerate(MENU, start=1):
        print('  %2d. %s' % (i, label))
    print('   0/q. 退出')
    print('==========================================================')


def main(port=None):
    if port:
        Q.SER_PORT = port
    print('连接 QMT 策略服务 %s:%d ...' % (Q.SER_HOST, Q.SER_PORT))
    if Q.is_service_up():
        print('服务在线，可开始测试。')
    else:
        print('[警告] 服务未响应。请确认完整版 QMT 已启动 QmtSer 策略，'
              '且端口为 %d。行情/账户相关指令可能失败。' % Q.SER_PORT)
    while True:
        try:
            print_menu()
            try:
                s = input('请选择 [0-%d]（0/q 退出，回车默认 1）> ' % len(MENU))
            except EOFError:
                break
            s = s.strip()
            if s == '':
                s = '1'
            if s.lower() in ('0', 'q', 'exit', 'quit'):
                print('退出。')
                break
            try:
                idx = int(s)
            except Exception:
                print('[提示] 无效输入，请输入数字 0-%d。' % len(MENU))
                continue
            if 1 <= idx <= len(MENU):
                label, fn = MENU[idx - 1]
                print('\n---------- %s ----------' % label)
                try:
                    fn()
                except Q.QmtError as e:
                    print('[错误] %s' % e)
                except Exception as e:
                    print('[错误] %s: %s' % (type(e).__name__, e))
            else:
                print('[提示] 数字超出范围，请输入 0-%d。' % len(MENU))
        except KeyboardInterrupt:
            print('\n退出。')
            break


if __name__ == '__main__':
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    main(_port)
