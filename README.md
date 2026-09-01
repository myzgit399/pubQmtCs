# pubQmtCs —— 用自主 Python 与 QMT 策略互动的桥接工具集

通过本目录下的三个文件，可以在【完整版 QMT 内置策略环境之外】的自主 Python
代码里，完成查询行情（最新价 / 全量 tick）、获取板块成份股、查询持仓、查询资产、
查询委托、开仓 / 平仓、撤单等动作。

```
┌─────────────────────────┐         ┌──────────────────────────┐
│ 自主 Python 代码         │         │ 完整版 QMT（内置 Python） │
│  (Grok Build / 脚本)    │  TCP    │  内嵌策略：QmtSer.py      │
│                         │ ──────► │  执行交易 + 获取行情       │
│  echo_client.py         │  socket │                          │
│  或 qmt_client.py        │ ◄────── │  ContextInfo / 柜台      │
└─────────────────────────┘         └──────────────────────────┘
```

## 文件说明

| 文件 | 运行位置 | 作用 |
| --- | --- | --- |
| `QmtSer.py` | 完整版 QMT 内置策略 | 交易执行 / 行情桥接**服务端**，内嵌在 QMT 策略里运行，主线程阻塞轮询 socket 请求 |
| `qmt_client.py` | 自主 Python | 客户端封装，提供与 QmtSer 一一对应的指令函数 |
| `echo_client.py` | 自主 Python（命令行） | 菜单式**测试工具**，也是“自主代码如何与 QMT 策略互动”的样例程序 |
| `README.md` | — | 本说明 |

## 工作原理

- **QmtSer.py** 作为 QMT 内置策略，在 `init(ContextInfo)` 里把 QMT 注入的
  `ContextInfo` 保存下来，然后**在主线程阻塞地**跑一个 TCP socket 服务
  （frameserver 模式，见代码注释）。它直接调用 QMT 原生接口 `passorder` /
  `cancel` / `get_trade_detail_data` / `get_full_tick` /
  `get_stock_list_in_sector`，把结果打包成 JSON 返回。
- **qmt_client.py** 只发一帧请求、收一帧响应，与端口 / op 严格配对。
- **echo_client.py** 基于 `qmt_client`，用一个数字菜单遍历测试所有指令，
  同时也是新手复制改写的样例。

### 通信协议

- 传输：纯 TCP socket（非 HTTP）。
- 帧格式：每帧 `[4 字节大端序长度][UTF-8 JSON 体]`。
- 交互：客户端发一帧 → 服务端回一帧 → 客户端读取返回。
- 默认地址端口：`127.0.0.1:8890`（可用环境变量 `QMT_SER_PORT` 覆盖）。

### 支持指令（op）

| op | 参数 | 说明 |
| --- | --- | --- |
| `account` | `accountid`, `accttype`(默认 STOCK) | 设置资金账号（下单/撤单/查询前必须先设） |
| `trade` | `side`(23买/24卖), `code`, `vol`(绝对股数), `price`(market/limit/数字限价), `strategy`, `remark` | 开仓/平仓 |
| `query` | `kind`(asset/position/order), `cancelable_only` | 查资产/持仓/委托 |
| `quote` | `codes` | 最新价 + 买二/卖二 + 涨跌停（与账号无关） |
| `tick` | `codes` | **全量 Tick**（`get_full_tick` 的载体，返回全代码 key） |
| `sector` | `sector` | **板块成份股代码列表**（`get_stock_list_in_sector` 的载体） |
| `cancel` | `mode`(all/buy/sell/id), `order_id` | 撤单 |
| `status` | — | 服务运行状态 |

## 安装与使用前准备

1. **启动完整版 QMT**，在【模型研究 / 策略交易】中新建一个 Python 策略，
   把 `QmtSer.py` 的内容粘贴进去并保存。

2. 在策略里运行它（启动即会在主线程跑 socket 服务）。策略界面应显示服务启动；
   若需改端口，用环境变量 `QMT_SER_PORT` 指定（默认 `8890`）。

3. 在 QMT 客户端的【交易】界面里选定资金账号——QMT 会把账号注入为全局变量
   `account` / `accountType`，`QmtSer.py` 会自动读取。若没选账户，也可通过
   下面 `account` 指令手动下发账号。

> 提示：完整版 QMT 内置 Python 可以直接调 `passorder`/`cancel`/
> `get_full_tick`/`get_stock_list_in_sector` 这些原生函数，因此 `QmtSer.py`
> 无需安装任何第三方包。

4. 自主 Python 侧不需要安装第三方依赖，只要标准库（`json`/`socket`/`struct`）
   即可。

## 在自主 Python 中使用 qmt_client（编程方式）

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'pubQmtCs'))
import qmt_client as Q

# 1) 检查服务是否在线
if not Q.is_service_up():
    print('服务未启动')
    exit()

# 2) 设置账号（下单/查询/撤单前必须）
Q.set_account('资金账号', 'STOCK')

# 3) 获取行情（与账号无关）
print(Q.quote('601005'))            # 重庆钢铁最新价 + 买卖二档

# 4) 获取全量 Tick（get_full_tick 的载体）
r = Q.full_tick(['601005.SH', '000001.SZ'])
print(r['data']['601005.SH']['lastPrice'])   # 最近成交价

# 5) 获取板块成份股代码列表（get_stock_list_in_sector 的载体）
codes = Q.get_stock_list_in_sector('沪深A股')
print(len(codes), codes[:5])

# 6) 查询资产 / 持仓 / 委托
Q.query('asset')
Q.query('position')
Q.query('order', cancelable_only=True)

# 7) 买 100 股重庆钢铁（对手价）
Q.trade(23, '601005', 100, price='market', strategy='my_algo', remark='demo')

# 8) 卖 100 股重庆钢铁（限价 10.50）
Q.trade(24, '601005', 100, price='10.50')

# 9) 撤单：按模式（全部/买/卖）或按委托单号
Q.cancel(mode='all')
Q.cancel(mode='id', order_id='委托单号')
```

每个函数返回服务端 JSON（`ok`/`data`/`msg` 等）；失败会抛 `QmtError`。
看 `qmt_client.py` 的 docstring 即可了解每个函数签名。

## 用 echo_client 测试（菜单方式）

`echo_client.py` 是一个命令行的菜单式测试工具，同时是“自主 Python 与 QMT 策略
互动”的可运行样例。它逐个测试所有指令，每个指令都有**默认选项**，回车即用默认值。

### 运行

```bash
python echo_client.py            # 默认端口 8890
python echo_client.py 8890       # 显式指定端口
```

启动后会先探测服务是否在线，然后显示菜单：

```
==================== QMT 指令测试菜单 ====================
  服务端口: 127.0.0.1:8890
   1. 设置账号
   2. 获取代码（板块成份股）
   3. 获取行情（最新价+买卖二档）
   4. 获取全量Tick (get_full_tick)
   5. 获取持仓
   6. 获取资产
   7. 获取订单
   8. 下买单
   9. 下卖单
  10. 撤销指定订单
  11. 撤销所有订单
  12. 查看服务状态
   0/q. 退出
==========================================================
请选择 [0-12]（0/q 退出，回车默认 1）>
```

- 直接回车默认进入第 1 项；输入数字进入对应项。
- 每个指令执行完后自动回到菜单，可继续选择；输入 `0`/`q`/`exit`/`quit` 退出。
- 执行中会逐个询问参数，**直接回车采用默认值**，输入则覆盖。

### 各菜单项的默认值

| 菜单项 | 默认且可改的参数 |
| --- | --- |
| 1 设置账号 | 资金账号(必填)，类型默认 `STOCK` |
| 2 获取代码（板块成份股） | 板块：`沪深A股` |
| 3 获取行情 | 代码：`601005`（重庆钢铁） |
| 4 获取全量Tick | 代码：`601005` |
| 5/6/7 持仓 / 资产 / 订单 | 订单可加 `仅显示可撤销`（默认否） |
| 8 下买单 | 代码 **重庆钢铁 `601005`**，数量 **100 股**，价格方式**对手价** |
| 9 下卖单 | 数量 100 股，价格方式对手价（代码需输入） |
| 10 撤销指定订单 | 委托单号（必填） |
| 11 撤销所有订单 | — |
| 12 服务状态 | — |

若要测试下单/撤单/查询，先选第 1 项设置账号；行情/板块类指令与账号无关。

### 典型测试流程

先设置账号，再跑只读/行情类指令（行情与板块不受账号与风险影响）：

```text
请选择 [0-12]> 1        ← 设置账号（输入资金账号，类型默认 STOCK）
请选择 [0-12]> 2        ← 获取板块成份股（默认沪深A股）
请选择 [0-12]> 3        ← 获取行情（默认重庆钢铁）
请选择 [0-12]> 4        ← 获取全量Tick（默认重庆钢铁）
请选择 [0-12]> 5        ← 持仓
请选择 [0-12]> 6        ← 资产
请选择 [0-12]> 7        ← 委托
```

再用第 7 项看到的一笔委托单号，测撤单；用第 8/9 项测下买单（默认直接买 100 股
重庆钢铁）、下卖单。测完用第 11 项一键清空全部可撤委托。

> ⚠️ 交易涉及真实资金，请务必在测试环境/模拟柜台或无持仓的账户上操作。
> `下买单` 默认对手价、100 股，确认无误后再让它执行。

## 常见问题

- **连不上 / 提示服务未启动**：确认完整版 QMT 已启动含 `QmtSer.py` 的策略，
  端口一致（默认 `8890`，可用 `QMT_SER_PORT` 改）。QMT 的内置服务须在
  **主线程阻塞**运行（`QmtSer.py` 的 `go()` 这么做），后台线程收不到连接。
- **下单/查询提示“尚未设置账号”**：先在 QMT 交易界面选定资金账号，或在
  `echo_client` 菜单选第 1 项「设置账号」手动下发，也可在 python 里调
  `Q.set_account(...)`。
- **下买单提示价格无效**：`trade` 的 `price` 必须显式给出 `market`/`limit`/
  数字限价，「latest」等兜底已被禁用。
- **中文乱码**：文件均以 UTF-8 保存并在首行声明 `#encoding:utf-8`；
  请在支持 UTF-8 的终端运行（Windows 可先 `chcp 65001`）。

## 自行扩展

新增指令非常容易，按三步走（以新增 `foo` 指令为例）：

1. **服务端 `QmtSer.py`**：加 `def _cmd_foo(p): ...` 返回 dict，并在
   `_dispatch` 里注册 `if op == 'foo': return _cmd_foo(p)`。
2. **客户端 `qmt_client.py`**：加 `def foo(...): return _call('foo', {...})`。
3. **`echo_client.py`**：写一个 `do_foo()`，把 `(标签, do_foo)` 追加进 `MENU`。

## 版权 / 免责

本工具仅供学习与内网自用，作者不对使用造成的损失负责。使用真实交易指令前，
请确保理解 QMT 交易机制并做好风控。
