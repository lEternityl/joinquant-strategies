# -*- coding: utf-8 -*-
"""
日内回转做 T 策略（网格 + VWAP 偏离 + 5分钟MACD双确认 + 尾盘清算）

核心逻辑：
  1. 底仓固定持有（BASE_POSITION_RATIO）
  2. 分时 VWAP 作为均价线，偏离 >= GRID_TRIGGER_CNT 格即触发信号
  3. 5 分钟 MACD 做双确认（金叉/底背离买 T，死叉/顶背离卖 T）
  4. 时间窗口过滤：避开 09:30-09:50 与 14:50 后
  5. 双向做 T：先买后卖 / 先卖后买 各 1 次，当日强平
  6. 硬止损：逆向浮亏 STOP_LOSS_PCT 无条件平仓
  7. 15:00 前无论盈亏，做T仓位必清，底仓不变

使用方法：
  - 回测需选择【分钟级】频率
  - 修改 SECURITY / GRID_STEP / GRID_TRIGGER_CNT / T_POSITION_RATIO 即可

注意：
  - A 股 T+1，当日买入股票当日不能卖。策略通过【底仓+做T仓】的“冲销”方式模拟 T+0：
      * 正向回转：先用现金买入 T 仓（H1），收盘前卖出等量底仓（H2）——实际是卖底仓+新增仓
      * 反向回转：先卖出 T 仓（来自底仓）（H1），收盘前再买回同等金额（H2）
  - 回测用 run_daily(..., time='every_bar') 每分钟触发
"""

# ============ 可调参数 ============
SECURITY = '000426.XSHE'       # 标的（深市 SZ → .XSHE；沪市 SH → .XSHG）
BASE_POSITION_RATIO = 0.70      # 底仓占总资产比例
T_POSITION_RATIO = 0.30         # 单次做 T 仓位占底仓的比例
MAX_T_PER_DAY = 1               # 每日做 T 次数上限

GRID_STEP = 0.005               # 网格单位：0.5%
GRID_TRIGGER_CNT = 3            # 偏离 >= 3 格触发（即 1.5%）
STOP_LOSS_PCT = 0.015           # 硬止损：逆向浮亏 1.5% 强平

# 时间窗口过滤（字符串 "HHMM"）
TRADE_START = '0950'            # 避开开盘前 20 分钟
TRADE_END = '1450'              # 避开收盘前 10 分钟
FORCE_CLOSE_TIME = '1455'       # 尾盘强制清算做T仓位

# MACD 参数（5 分钟）
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# =================================


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    set_order_cost(OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5,
    ), type='stock')
    set_slippage(PriceRelatedSlippage(0.00246))

    # 全局状态（用 g.*）
    g.security = SECURITY
    g.base_amount = 0           # 底仓目标股数
    g.t_state = 'flat'          # 'flat' | 'long_t' | 'short_t'
    g.t_entry_price = 0.0       # 做T开仓价
    g.t_amount = 0              # 做T开仓股数
    g.t_count_today = 0
    g.vwap_cum_amount = 0.0
    g.vwap_cum_volume = 0.0

    # 每日任务
    run_daily(before_market, time='before_open')     # 重置
    run_daily(build_base_position, time='09:31')     # 建底仓（首日）
    run_daily(intraday_t0, time='every_bar')         # 每根 bar 执行做T
    run_daily(force_close_t, time='14:55')           # 尾盘清算


def before_market(context):
    """每日重置：清零当日状态与 VWAP 累积"""
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_amount = 0
    g.t_count_today = 0
    g.vwap_cum_amount = 0.0
    g.vwap_cum_volume = 0.0


def build_base_position(context):
    """建立/维持底仓到目标仓位（仅在底仓不足时补足）"""
    security = g.security
    cd = get_current_data()
    if cd[security].paused:
        return

    target_value = context.portfolio.total_value * BASE_POSITION_RATIO
    pos = context.portfolio.positions.get(security)
    current_value = pos.value if pos else 0

    # 只在明显偏低时补足底仓，避免频繁调整
    if current_value < target_value * 0.95:
        order_target_value(security, target_value)
        g.base_amount = int(target_value / cd[security].last_price / 100) * 100
        log.info('[底仓] 调整至 %.0f 元，约 %d 股' % (target_value, g.base_amount))
    else:
        g.base_amount = pos.total_amount


def intraday_t0(context):
    """分钟级做 T 主逻辑"""
    security = g.security
    now = context.current_dt
    hhmm = now.strftime('%H%M')

    # --- 1. 时间窗口过滤 ---
    if hhmm < TRADE_START or hhmm >= TRADE_END:
        return

    cd = get_current_data()
    if cd[security].paused:
        return

    # --- 2. 取当前分钟 bar（用 attribute_history，自动避免未来函数）---
    bar = attribute_history(security, 1, '1m', ['close', 'volume', 'money'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])
    vol = float(bar['volume'].iloc[-1])
    amt = float(bar['money'].iloc[-1])

    # --- 3. 累积 VWAP（分时均价线）---
    g.vwap_cum_amount += amt
    g.vwap_cum_volume += vol
    if g.vwap_cum_volume <= 0:
        return
    vwap = g.vwap_cum_amount / g.vwap_cum_volume
    deviation = (price - vwap) / vwap        # 正值偏高，负值偏低
    grid_n = deviation / GRID_STEP           # 偏离多少格

    # --- 4. 持有做T仓位 → 先处理平仓（止损 / 止盈 / 回归均线）---
    if g.t_state == 'long_t':
        pnl = (price - g.t_entry_price) / g.t_entry_price
        # 硬止损
        if pnl <= -STOP_LOSS_PCT:
            close_t_long(context, price, reason='hard_stop %.2f%%' % (pnl * 100))
            return
        # 止盈：价格回升到 VWAP 上方或涨幅达 1 格
        if price >= vwap or pnl >= GRID_STEP:
            close_t_long(context, price, reason='take_profit %.2f%%' % (pnl * 100))
            return
        return

    if g.t_state == 'short_t':
        pnl = (g.t_entry_price - price) / g.t_entry_price    # 反向：跌则盈
        if pnl <= -STOP_LOSS_PCT:
            close_t_short(context, price, reason='hard_stop %.2f%%' % (pnl * 100))
            return
        if price <= vwap or pnl >= GRID_STEP:
            close_t_short(context, price, reason='take_profit %.2f%%' % (pnl * 100))
            return
        return

    # --- 5. 无仓位 → 看是否开仓做 T ---
    if g.t_count_today >= MAX_T_PER_DAY:
        return

    # 5 分钟 MACD 双确认
    macd_sig = calc_5min_macd_signal(security)

    # 正向回转：股价低于 VWAP 3 格以上 + MACD 金叉/底背离 → 买入做多T
    if grid_n <= -GRID_TRIGGER_CNT and macd_sig == 'bull':
        open_t_long(context, price)
        return

    # 反向回转：股价高于 VWAP 3 格以上 + MACD 死叉/顶背离 → 卖出做空T（卖底仓）
    if grid_n >= GRID_TRIGGER_CNT and macd_sig == 'bear':
        open_t_short(context, price)
        return


def open_t_long(context, price):
    """正向回转开仓：用现金买入 T 仓"""
    security = g.security
    pos = context.portfolio.positions.get(security)
    if pos is None or pos.total_amount <= 0:
        return
    base_value = pos.value
    t_value = base_value * T_POSITION_RATIO
    available = context.portfolio.available_cash * 0.95
    buy_value = min(t_value, available)
    if buy_value < price * 100:
        return

    order_obj = order_value(security, buy_value)
    if order_obj is None:
        return

    g.t_state = 'long_t'
    g.t_entry_price = price
    g.t_amount = int(buy_value / price / 100) * 100
    g.t_count_today += 1
    log.info('[开T多] 价格=%.3f 金额=%.0f 格数=%d' %
             (price, buy_value, g.t_count_today))


def close_t_long(context, price, reason=''):
    """正向回转平仓：卖出等量底仓（因 T+1 当日买入的不能卖）"""
    security = g.security
    # 由于 T+1，卖出今日刚买入的份额会被拒绝；这里卖的是原底仓的等量部分，
    # 实际账面上做T新买的股票变为明日可卖，等效完成一次 T+0 冲销。
    sell_shares = g.t_amount
    if sell_shares <= 0:
        g.t_state = 'flat'
        return
    pos = context.portfolio.positions.get(security)
    if pos is None:
        g.t_state = 'flat'
        return
    # closeable_amount = 昨日及以前可卖的股数（底仓）
    sellable = pos.closeable_amount
    sell_shares = min(sell_shares, sellable)
    if sell_shares < 100:
        log.warn('[平T多-失败] 可卖不足 sellable=%d' % sellable)
        g.t_state = 'flat'
        return

    order(security, -sell_shares)
    pnl_pct = (price - g.t_entry_price) / g.t_entry_price * 100
    log.info('[平T多] 价格=%.3f 数量=%d 盈亏=%.2f%% 原因=%s' %
             (price, sell_shares, pnl_pct, reason))
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_amount = 0


def open_t_short(context, price):
    """反向回转开仓：卖出底仓（可卖的部分）做空T"""
    security = g.security
    pos = context.portfolio.positions.get(security)
    if pos is None or pos.closeable_amount <= 0:
        return
    sell_value = pos.value * T_POSITION_RATIO
    sell_shares = int(sell_value / price / 100) * 100
    sell_shares = min(sell_shares, pos.closeable_amount)
    if sell_shares < 100:
        return

    order(security, -sell_shares)
    g.t_state = 'short_t'
    g.t_entry_price = price
    g.t_amount = sell_shares
    g.t_count_today += 1
    log.info('[开T空] 价格=%.3f 数量=%d 格数=%d' %
             (price, sell_shares, g.t_count_today))


def close_t_short(context, price, reason=''):
    """反向回转平仓：买回同样股数恢复底仓"""
    security = g.security
    buy_shares = g.t_amount
    if buy_shares <= 0:
        g.t_state = 'flat'
        return
    cost = buy_shares * price * 1.002  # 含手续费缓冲
    if context.portfolio.available_cash < cost:
        buy_shares = int(context.portfolio.available_cash * 0.95 / price / 100) * 100
    if buy_shares < 100:
        log.warn('[平T空-失败] 资金不足')
        g.t_state = 'flat'
        return

    order(security, buy_shares)
    pnl_pct = (g.t_entry_price - price) / g.t_entry_price * 100
    log.info('[平T空] 价格=%.3f 数量=%d 盈亏=%.2f%% 原因=%s' %
             (price, buy_shares, pnl_pct, reason))
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_amount = 0


def force_close_t(context):
    """尾盘 14:55 强制清算做 T 仓，恢复底仓"""
    security = g.security
    cd = get_current_data()
    if cd[security].paused:
        return
    price = cd[security].last_price

    if g.t_state == 'long_t':
        close_t_long(context, price, reason='force_close_EOD')
    elif g.t_state == 'short_t':
        close_t_short(context, price, reason='force_close_EOD')

    # 取消所有未成交单
    open_orders = get_open_orders()
    for o in open_orders.values():
        cancel_order(o)


# ============ 工具函数 ============

def calc_5min_macd_signal(security):
    """计算 5 分钟 MACD 信号：'bull' / 'bear' / 'neutral'"""
    need = MACD_SLOW + MACD_SIGNAL + 5
    df = attribute_history(security, need, '5m', ['close'], skip_paused=True)
    if df is None or len(df) < MACD_SLOW + MACD_SIGNAL:
        return 'neutral'

    close = df['close']
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    macd = (dif - dea) * 2

    if len(macd) < 3:
        return 'neutral'

    last, prev = macd.iloc[-1], macd.iloc[-2]
    dif_last, dif_prev = dif.iloc[-1], dif.iloc[-2]
    dea_last, dea_prev = dea.iloc[-1], dea.iloc[-2]

    # 金叉（低位）
    if dif_prev <= dea_prev and dif_last > dea_last and dif_last < 0:
        return 'bull'
    # 柱状图由负转正
    if prev < 0 and last > 0:
        return 'bull'
    # 死叉（高位）
    if dif_prev >= dea_prev and dif_last < dea_last and dif_last > 0:
        return 'bear'
    if prev > 0 and last < 0:
        return 'bear'

    return 'neutral'
