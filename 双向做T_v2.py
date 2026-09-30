# -*- coding: utf-8 -*-
"""
双向做 T 策略 v2（全新独立实现，非旧版改进版）

设计原则
========
1. 底仓与T仓严格分账：
   - g.base_target_shares：底仓目标股数（首日建立，全程不变）
   - g.t_shares：当日做T仓股数（每日开盘清零，收盘清零）
   - 平仓时数量校验确保不破坏底仓总量

2. 底仓只在首日建一次（g.base_built 标记），避免每日重复触发 order_target_value
   导致的资金紧张恶性循环

3. VWAP 由当日累计计算：用 attribute_history 拉当日所有分钟bar的 money/volume，
   一次性求和；不在 every_bar 中手动累加，避免重复/漏加风险

4. 实际成交回填：order_value 返回订单对象，平仓前用 order.filled
   作为真实持仓股数；不用 buy_value/price 的理论计算

5. 平多T的可用卖出量 = pos.closeable_amount - 安全保留量
   保证平多T只卖到原底仓的一部分时，不会让账户跌穿底仓目标

6. 去除模拟盘无效选项 avoid_future_data

7. 正确的持仓判空：用 pos.total_amount > 0

T+0 模拟逻辑
============
A股 T+1，当日买入不能卖。本策略用以下方式模拟双向做T：

- 正向回转（先买后卖，赚跌后反弹）：
  开仓时 order_value 用现金买入 t_shares 股 → 当日 t_shares 锁定为 T+1
  平仓时卖出原底仓中 t_shares 股（这是昨日已持有的，可卖）
  ≈ 等价于"先低买、后高卖"完成一次T+0
  收盘后总持仓 = 原底仓 - 卖出t_shares + 新买入t_shares = 原底仓数量不变

- 反向回转（先卖后买，赚涨后回落）：
  开仓时 order 卖出底仓中 t_shares 股（必须是 closeable_amount）
  平仓时 order_value 用现金买入 t_shares 股
  ≈ 等价于"先高卖、后低买"完成一次T+0
  收盘后总持仓 = 原底仓 - 卖出t_shares + 新买入t_shares = 原底仓数量不变

参数说明
========
- 改 SECURITY 为你要做T的股票代码（深市 .XSHE，沪市 .XSHG）
- BASE_POSITION_RATIO 控制底仓占比（保证有足够股数提供T仓"卖出额度"）
- T_POSITION_RATIO 控制单次T仓占底仓的比例
- GRID_TRIGGER_PCT 越大触发越保守，做T次数越少

使用方法
========
- 回测必须选择"分钟"频率
- 起始日 09:31 会一次性建底仓，建议初始资金 >= 10万
- 回测前确认标的当前未停牌、未ST
"""

# ============ 参数 ============
SECURITY = '000426.XSHE'           # 标的
BASE_POSITION_RATIO = 0.70         # 底仓占总资产比例
T_POSITION_RATIO = 0.30            # 单次T仓占底仓比例（提高至0.30提升单次盈利覆盖手续费）
MAX_T_PER_DAY = 3                  # 每日最大做T次数（提高至3次，充分利用震荡）

GRID_TRIGGER_PCT = 0.008           # VWAP偏离触发阈值 0.8%（从1.5%下调以提高信号频次）
TAKE_PROFIT_PCT = 0.008            # 单次T止盈 0.8%（覆盖往返手续费约0.16%+滑点）
STOP_LOSS_PCT = 0.015              # 硬止损 1.5%

TRADE_START = '09:50'              # 早盘观察期，避开开盘脉冲
TRADE_END = '14:30'                # 14:30 后禁止开新T仓，给T仓留 25 分钟交易空间
FORCE_CLOSE_TIME = '14:55'         # 强制平T仓时间

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_CACHE_SECONDS = 300           # 5分钟MACD信号缓存周期，减少重复计算
# ===============================


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)

    set_order_cost(OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5,
    ), type='stock')
    set_slippage(PriceRelatedSlippage(0.00246))

    # 持久化全局状态
    g.security = SECURITY
    g.base_built = False                # 底仓是否已建立
    g.base_target_shares = 0            # 底仓目标股数（建仓后写一次）
    g.t_state = 'flat'                  # 'flat' | 'long_t' | 'short_t'
    g.t_entry_price = 0.0               # T仓开仓价
    g.t_shares = 0                      # 当前T仓实际成交股数
    g.t_count_today = 0                 # 今日已做T次数
    g.pending_sell_shares = 0           # T+1 平仓不足时的延迟卖出股数
    # MACD 缓存
    g.macd_cache_sig = 'neutral'
    g.macd_cache_ts = None              # 上次计算的 datetime

    # 调度
    run_daily(before_market_reset, time='before_open')
    run_daily(build_base_position_once, time='09:31')
    run_daily(intraday_t0, time='every_bar')
    run_daily(force_close_t, time=FORCE_CLOSE_TIME)
    run_daily(end_of_day_check, time='after_close')


# ----------------------------------------------------------------------
# 每日重置
# ----------------------------------------------------------------------
def before_market_reset(context):
    """每日开盘前：清零做T相关状态。底仓状态保持。"""
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_shares = 0
    g.t_count_today = 0
    g.pending_sell_shares = 0
    g.macd_cache_sig = 'neutral'
    g.macd_cache_ts = None


# ----------------------------------------------------------------------
# 建底仓（仅首日生效）
# ----------------------------------------------------------------------
def build_base_position_once(context):
    """首日 09:31 建底仓，之后只在底仓被吃掉时补足。
    同时优先处理上一日因 T+1 限制残留的 pending_sell_shares。
    """
    security = g.security
    cd = get_current_data()
    if cd[security].paused:
        return

    pos = context.portfolio.positions.get(security)
    current_shares = pos.total_amount if pos is not None else 0

    if not g.base_built:
        # 首次建仓
        target_value = context.portfolio.total_value * BASE_POSITION_RATIO
        price = cd[security].last_price
        target_shares = int(target_value / price / 100) * 100
        if target_shares < 100:
            log.warn('[底仓] 资金不足 100 股，无法建仓')
            return
        order_obj = order(security, target_shares)
        if order_obj is None:
            log.warn('[底仓] 下单失败')
            return
        g.base_target_shares = target_shares
        g.base_built = True
        log.info('[底仓-首建] 目标 %d 股，价格 %.3f' % (target_shares, price))
        return

    # 优先处理昨日 T+1 残留的延迟卖出
    if g.pending_sell_shares > 0 and pos is not None:
        pending = min(g.pending_sell_shares, pos.closeable_amount)
        pending = int(pending / 100) * 100
        if pending >= 100:
            order(security, -pending)
            log.info('[T+1延迟卖出] 卖出 %d 股（昨日残留）' % pending)
            g.pending_sell_shares -= pending
            current_shares -= pending  # 修正本次补底仓判断

    # 之后：只在底仓被做T平仓"吃掉"时补足
    if current_shares < g.base_target_shares:
        miss = g.base_target_shares - current_shares
        miss = int(miss / 100) * 100
        if miss <= 0:
            return
        price = cd[security].last_price
        need_cash = miss * price * 1.005
        if context.portfolio.available_cash < need_cash:
            log.warn('[底仓-补回] 现金不足，需 %.0f，可用 %.0f' %
                     (need_cash, context.portfolio.available_cash))
            return
        order(security, miss)
        log.info('[底仓-补回] 缺 %d 股已补' % miss)


# ----------------------------------------------------------------------
# 分钟级做T
# ----------------------------------------------------------------------
def intraday_t0(context):
    security = g.security
    now = context.current_dt
    hhmm = now.strftime('%H:%M')

    # 时间窗口
    if hhmm < TRADE_START or hhmm >= TRADE_END:
        return

    cd = get_current_data()
    if cd[security].paused:
        return

    # 必须先有底仓
    pos = context.portfolio.positions.get(security)
    if pos is None or pos.total_amount <= 0:
        return

    # 当前 bar 价格
    bar = attribute_history(security, 1, '1m', ['close'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])

    # 计算当日 VWAP（一次性，避免手动累加）
    vwap = calc_today_vwap(security, now)
    if vwap is None or vwap <= 0:
        return
    deviation = (price - vwap) / vwap

    # ---- 持有T多仓：管理平仓 ----
    if g.t_state == 'long_t':
        pnl = (price - g.t_entry_price) / g.t_entry_price
        if pnl <= -STOP_LOSS_PCT:
            close_t_long(context, price, 'stop_loss %.2f%%' % (pnl * 100))
        elif pnl >= TAKE_PROFIT_PCT or price >= vwap:
            close_t_long(context, price, 'take_profit %.2f%%' % (pnl * 100))
        return

    # ---- 持有T空仓：管理平仓 ----
    if g.t_state == 'short_t':
        pnl = (g.t_entry_price - price) / g.t_entry_price
        if pnl <= -STOP_LOSS_PCT:
            close_t_short(context, price, 'stop_loss %.2f%%' % (pnl * 100))
        elif pnl >= TAKE_PROFIT_PCT or price <= vwap:
            close_t_short(context, price, 'take_profit %.2f%%' % (pnl * 100))
        return

    # ---- 无仓位：考虑开仓 ----
    if g.t_count_today >= MAX_T_PER_DAY:
        return

    macd_sig = get_cached_macd_signal(security, now)

    # 正向回转：跌过 VWAP - GRID_TRIGGER_PCT 且 MACD 转多
    if deviation <= -GRID_TRIGGER_PCT and macd_sig == 'bull':
        open_t_long(context, price)
        return

    # 反向回转：涨过 VWAP + GRID_TRIGGER_PCT 且 MACD 转空
    if deviation >= GRID_TRIGGER_PCT and macd_sig == 'bear':
        open_t_short(context, price)
        return


# ----------------------------------------------------------------------
# 开/平 T仓（核心：T+1 安全 & 真实成交回填）
# ----------------------------------------------------------------------
def open_t_long(context, price):
    """正向回转开仓：用现金买入 T仓。"""
    security = g.security
    base_value = g.base_target_shares * price
    t_value = base_value * T_POSITION_RATIO
    available = context.portfolio.available_cash * 0.95
    buy_value = min(t_value, available)
    if buy_value < price * 100:
        return

    order_obj = order_value(security, buy_value)
    if order_obj is None:
        return

    # 用真实成交回填，而非理论计算
    filled = int(order_obj.filled) if order_obj.filled else 0
    if filled < 100:
        log.info('[开T多-放弃] 实际成交不足100股 filled=%d' % filled)
        return

    g.t_state = 'long_t'
    g.t_entry_price = price
    g.t_shares = filled
    g.t_count_today += 1
    log.info('[开T多] 价格=%.3f 股数=%d 第%d次' %
             (price, filled, g.t_count_today))


def close_t_long(context, price, reason):
    """正向回转平仓：卖出原底仓中相应股数。

    关键：今日 order_value 买入的 g.t_shares 是 T+1 锁定的，
    我们卖的是昨日及之前的底仓部分（pos.closeable_amount）。
    若可卖量不足，部分平仓 + 剩余记入 pending_sell_shares 明日补卖。
    """
    security = g.security
    pos = context.portfolio.positions.get(security)
    if pos is None or pos.total_amount <= 0:
        g.t_state = 'flat'
        g.t_shares = 0
        return

    sell_shares = min(g.t_shares, pos.closeable_amount)
    sell_shares = int(sell_shares / 100) * 100
    if sell_shares < 100:
        # 完全无法平仓 → 全部记入 pending，明日开盘后处理
        log.warn('[平T多-延迟] 可卖0，%d股转明日 reason=%s' % (g.t_shares, reason))
        g.pending_sell_shares += g.t_shares
        g.t_state = 'flat'
        g.t_shares = 0
        g.t_entry_price = 0.0
        return

    order(security, -sell_shares)
    pnl_pct = (price - g.t_entry_price) / g.t_entry_price * 100
    remain = g.t_shares - sell_shares
    if remain > 0:
        log.info('[平T多-部分] 价格=%.3f 已卖=%d 延迟=%d 盈亏=%.2f%% %s' %
                 (price, sell_shares, remain, pnl_pct, reason))
        g.pending_sell_shares += remain
    else:
        log.info('[平T多] 价格=%.3f 数量=%d 盈亏=%.2f%% %s' %
                 (price, sell_shares, pnl_pct, reason))
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_shares = 0


def open_t_short(context, price):
    """反向回转开仓：卖出底仓做空T。"""
    security = g.security
    pos = context.portfolio.positions[security]
    if pos.closeable_amount <= 0:
        return

    base_value = g.base_target_shares * price
    sell_value = base_value * T_POSITION_RATIO
    sell_shares = int(sell_value / price / 100) * 100
    sell_shares = min(sell_shares, pos.closeable_amount)
    # 安全约束：卖完后底仓不能跌破 (1 - T_POSITION_RATIO) * 目标
    min_keep = int(g.base_target_shares * (1 - T_POSITION_RATIO))
    max_sellable = pos.closeable_amount - min_keep
    sell_shares = min(sell_shares, max_sellable)
    sell_shares = int(sell_shares / 100) * 100
    if sell_shares < 100:
        return

    order_obj = order(security, -sell_shares)
    if order_obj is None:
        return
    filled = int(order_obj.filled) if order_obj.filled else 0
    if filled < 100:
        log.info('[开T空-放弃] 实际成交不足100股 filled=%d' % filled)
        return

    g.t_state = 'short_t'
    g.t_entry_price = price
    g.t_shares = filled
    g.t_count_today += 1
    log.info('[开T空] 价格=%.3f 股数=%d 第%d次' %
             (price, filled, g.t_count_today))


def close_t_short(context, price, reason):
    """反向回转平仓：买回相应股数恢复底仓。"""
    security = g.security
    if g.t_shares < 100:
        g.t_state = 'flat'
        return

    buy_shares = g.t_shares
    cost = buy_shares * price * 1.005
    available = context.portfolio.available_cash
    if available < cost:
        # 现金不足，按可用现金调整
        buy_shares = int(available * 0.95 / price / 100) * 100
    if buy_shares < 100:
        log.warn('[平T空-失败] 现金不足 available=%.0f' % available)
        return

    order(security, buy_shares)
    pnl_pct = (g.t_entry_price - price) / g.t_entry_price * 100
    log.info('[平T空] 价格=%.3f 数量=%d 盈亏=%.2f%% %s' %
             (price, buy_shares, pnl_pct, reason))
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_shares = 0


# ----------------------------------------------------------------------
# 尾盘强平
# ----------------------------------------------------------------------
def force_close_t(context):
    security = g.security
    cd = get_current_data()
    if cd[security].paused:
        return
    price = cd[security].last_price

    if g.t_state == 'long_t':
        close_t_long(context, price, 'force_EOD')
    elif g.t_state == 'short_t':
        close_t_short(context, price, 'force_EOD')

    open_orders = get_open_orders()
    for o in list(open_orders.values()):
        cancel_order(o)


def end_of_day_check(context):
    """收盘后核对：底仓是否完整。"""
    security = g.security
    pos = context.portfolio.positions.get(security)
    actual = pos.total_amount if pos is not None else 0
    if g.base_built and actual < g.base_target_shares:
        log.warn('[尾盘核对] 底仓不足！实际%d 目标%d 缺%d' %
                 (actual, g.base_target_shares, g.base_target_shares - actual))


# ======================================================================
# 工具函数
# ======================================================================
def calc_today_vwap(security, now):
    """计算当日累计 VWAP。

    用 attribute_history 拉出当日所有1分钟bar，一次性 sum(money) / sum(volume)。
    """
    minutes_passed = (now.hour - 9) * 60 + now.minute - 30
    if now.hour >= 13:
        minutes_passed -= 90  # 减去午休
    minutes_passed = max(1, min(minutes_passed, 240))

    df = attribute_history(security, minutes_passed, '1m',
                           ['volume', 'money'], skip_paused=True)
    if df is None or len(df) == 0:
        return None
    total_vol = float(df['volume'].sum())
    total_money = float(df['money'].sum())
    if total_vol <= 0:
        return None
    return total_money / total_vol


def get_cached_macd_signal(security, now):
    """带缓存的 5分钟 MACD 信号获取。

    在 MACD_CACHE_SECONDS 秒内复用上次计算结果，
    避免每根1分钟bar都计算一遍 EMA。
    """
    if g.macd_cache_ts is None:
        sig = calc_5min_macd_signal(security)
        g.macd_cache_sig = sig
        g.macd_cache_ts = now
        return sig

    delta = (now - g.macd_cache_ts).total_seconds()
    if delta >= MACD_CACHE_SECONDS:
        sig = calc_5min_macd_signal(security)
        g.macd_cache_sig = sig
        g.macd_cache_ts = now
        return sig

    return g.macd_cache_sig


def calc_5min_macd_signal(security):
    """5分钟MACD信号：'bull' / 'bear' / 'neutral'。"""
    need = MACD_SLOW + MACD_SIGNAL + 5
    df = attribute_history(security, need, '5m', ['close'], skip_paused=True)
    if df is None or len(df) < MACD_SLOW + MACD_SIGNAL:
        return 'neutral'

    close = df['close']
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = (dif - dea) * 2

    if len(hist) < 3:
        return 'neutral'

    h_last, h_prev = hist.iloc[-1], hist.iloc[-2]
    dif_last, dif_prev = dif.iloc[-1], dif.iloc[-2]
    dea_last, dea_prev = dea.iloc[-1], dea.iloc[-2]

    # 低位金叉
    if dif_prev <= dea_prev and dif_last > dea_last and dif_last < 0:
        return 'bull'
    if h_prev < 0 < h_last:
        return 'bull'
    # 高位死叉
    if dif_prev >= dea_prev and dif_last < dea_last and dif_last > 0:
        return 'bear'
    if h_prev > 0 > h_last:
        return 'bear'

    return 'neutral'