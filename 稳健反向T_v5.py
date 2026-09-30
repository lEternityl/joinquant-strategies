# -*- coding: utf-8 -*-
"""
稳健反向 T 策略 v5（强趋势过滤 + 隔夜风险硬约束）

核心改进
========
针对反向T在单边上涨行情中持续止损的问题，引入：

1. 趋势过滤（最重要）：
   - 计算 10日 EMA 斜率
   - 当价格远离均线 > UPTREND_THRESHOLD 时认定上升趋势
   - 上升趋势中**完全禁止开 short**（反向T 在牛市是灾难）

2. 隔夜风险硬约束：
   - 跨日持有 short 仓时，次日 09:35-09:50 立即检查
   - 浮亏 >= OVERNIGHT_STOP_PCT (1.5%) 直接平仓，不等日内 3% 止损
   - 这就堵住了 -7% 大亏损的来源

3. 最大持仓天数下调：MAX_HOLD_DAYS 从 5 天 → 2 天

4. 单次仓位下调：T_POSITION_RATIO 从 30% → 20%
   单次最大可控损失 = 20% × 3% = 0.6% 总资产

5. 开仓条件强化：
   - VWAP 偏离 >= GRID_TRIGGER_PCT (1.0%)
   - 5min MACD 死叉
   - 10日 EMA 斜率 <= 0（震荡或下行）
   - 5min 内未创新高（避免突破开多T方向卖出）

6. 早盘隔夜检查改在 09:35（比 09:50 提前 15 分钟）

行为
====
- 首日只建底仓
- 第 2 日起进入做 T，但仅在非上升趋势中开仓
- 持有 short 时跨日次日早盘优先检查跳空亏损
- 价格回归 VWAP 且盈利时锁利
- 最多持仓 2 天，超过强平

使用
====
- 分钟级回测
- 建议初始资金 >= 10 万
- 适合震荡或下行行情，单边牛市会大幅减少交易频次
"""

# ============ 参数 ============
SECURITY = '000426.XSHE'
BASE_POSITION_RATIO = 0.70
T_POSITION_RATIO = 0.20            # 降至 20%
MAX_T_PER_DAY = 2

GRID_TRIGGER_PCT = 0.010
TAKE_PROFIT_PCT = 0.010
STOP_LOSS_PCT = 0.030               # 日内硬止损
OVERNIGHT_STOP_PCT = 0.015          # 隔夜浮亏止损（更严格）

MAX_HOLD_DAYS = 2                   # 最多持仓 2 天
MIN_BASE_RATIO = 0.50

# 趋势过滤
UPTREND_EMA_PERIOD = 10             # 10日 EMA
UPTREND_THRESHOLD = 0.03            # 价格高于 10日EMA 3% 视为强上升趋势
EMA_SLOPE_LOOKBACK = 3              # 比较 3 天前的 EMA 判定斜率

TRADE_START = '09:50'
OVERNIGHT_CHECK_START = '09:35'     # 跨日持仓的早盘检查时间
OPEN_CUTOFF = '14:30'
END_OF_DAY = '14:55'

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_CACHE_SECONDS = 300
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

    g.security = SECURITY
    g.base_built = False
    g.base_target_shares = 0
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_entry_date = None
    g.t_shares = 0
    g.t_count_today = 0
    g.macd_sig = 'neutral'
    g.macd_ts = None
    g.trend_cache_date = None
    g.trend_is_up = False

    run_daily(daily_reset, time='before_open')
    run_daily(build_base, time='09:31')
    run_daily(on_bar, time='every_bar')
    run_daily(eod_check, time='after_close')


# ----------------------------------------------------------------------
def daily_reset(context):
    g.t_count_today = 0
    g.macd_sig = 'neutral'
    g.macd_ts = None


# ----------------------------------------------------------------------
def build_base(context):
    sec = g.security
    cd = get_current_data()
    if cd[sec].paused:
        return

    if not g.base_built:
        target_value = context.portfolio.total_value * BASE_POSITION_RATIO
        price = cd[sec].last_price
        target_shares = int(target_value / price / 100) * 100
        if target_shares < 100:
            log.warn('[底仓] 资金不足无法建仓')
            return
        if order(sec, target_shares) is None:
            log.warn('[底仓] 下单失败')
            return
        g.base_target_shares = target_shares
        g.base_built = True
        log.info('[底仓-首建] %d股 价格%.3f' % (target_shares, price))


# ----------------------------------------------------------------------
def on_bar(context):
    sec = g.security
    now = context.current_dt
    hhmm = now.strftime('%H:%M')

    if hhmm >= END_OF_DAY:
        return

    cd = get_current_data()
    if cd[sec].paused:
        return

    bar = attribute_history(sec, 1, '1m', ['close'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])

    # ---- 跨日 short 仓的早盘隔夜风险检查（09:35 起即可执行）----
    if g.t_state == 'short_t' and hhmm >= OVERNIGHT_CHECK_START:
        is_overnight = (g.t_entry_date is not None
                        and now.date() > g.t_entry_date)
        if is_overnight:
            pnl = (g.t_entry_price - price) / g.t_entry_price
            if pnl <= -OVERNIGHT_STOP_PCT:
                do_close_short(context, price,
                               'overnight_stop %.2f%%' % (pnl * 100), cd)
                return

    # 正常时间窗
    if hhmm < TRADE_START:
        return

    vwap = calc_today_vwap(sec, now)
    if vwap is None or vwap <= 0:
        return

    # ---- 持有 short：考虑平仓 ----
    if g.t_state == 'short_t':
        try_close_short(context, price, vwap, now, cd)
        return

    # ---- 无仓位：开仓 ----
    if hhmm >= OPEN_CUTOFF:
        return
    if g.t_count_today >= MAX_T_PER_DAY:
        return

    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.total_amount <= 0:
        return

    if pos.total_amount * price < context.portfolio.total_value * MIN_BASE_RATIO:
        return

    if pos.closeable_amount <= 0:
        return

    # 关键：趋势过滤
    if is_uptrend(sec, now):
        return

    deviation = (price - vwap) / vwap
    if deviation < GRID_TRIGGER_PCT:
        return

    if cached_macd(sec, now) != 'bear':
        return

    open_short(context, price, pos, now)


# ----------------------------------------------------------------------
def open_short(context, price, pos, now):
    sec = g.security
    base_value = g.base_target_shares * price
    sell_value = base_value * T_POSITION_RATIO
    target_shares = int(sell_value / price / 100) * 100

    min_keep = int(g.base_target_shares * (1 - T_POSITION_RATIO))
    max_sellable = pos.closeable_amount - min_keep
    sell_shares = min(target_shares, max_sellable)
    sell_shares = int(sell_shares / 100) * 100
    if sell_shares < 100:
        return

    order_obj = order(sec, -sell_shares)
    if order_obj is None:
        return
    filled = int(order_obj.filled) if order_obj.filled else 0
    if filled < 100:
        return

    g.t_state = 'short_t'
    g.t_entry_price = price
    g.t_entry_date = now.date()
    g.t_shares = filled
    g.t_count_today += 1
    log.info('[开T空] 价格=%.3f 数量=%d 第%d次' %
             (price, filled, g.t_count_today))


def try_close_short(context, price, vwap, now, cd):
    pnl = (g.t_entry_price - price) / g.t_entry_price
    days_held = 0
    if g.t_entry_date is not None:
        days_held = (now.date() - g.t_entry_date).days

    reason = None
    if pnl <= -STOP_LOSS_PCT:
        reason = 'stop_loss %.2f%%' % (pnl * 100)
    elif days_held >= MAX_HOLD_DAYS:
        reason = 'max_hold_%dd' % days_held
    elif pnl >= TAKE_PROFIT_PCT:
        reason = 'take_profit %.2f%%' % (pnl * 100)
    elif price <= vwap and pnl > 0:
        reason = 'vwap_revert %.2f%%' % (pnl * 100)
    else:
        return

    do_close_short(context, price, reason, cd)


def do_close_short(context, price, reason, cd):
    sec = g.security
    if g.t_shares < 100:
        g.t_state = 'flat'
        return

    high_limit = cd[sec].high_limit
    if high_limit > 0 and price >= high_limit * 0.998:
        log.info('[平T空-跳过] 接近涨停 price=%.3f limit=%.3f' %
                 (price, high_limit))
        return

    buy_shares = g.t_shares
    cost = buy_shares * price * 1.005
    available = context.portfolio.available_cash
    if available < cost:
        buy_shares = int(available * 0.95 / price / 100) * 100
    if buy_shares < 100:
        log.warn('[平T空-失败] 现金不足 available=%.0f' % available)
        return

    order_obj = order(sec, buy_shares)
    if order_obj is None:
        return
    filled = int(order_obj.filled) if order_obj.filled else 0
    if filled < 100:
        log.info('[平T空-未成交] filled=%d 待次日' % filled)
        return

    pnl_pct = (g.t_entry_price - price) / g.t_entry_price * 100
    log.info('[平T空] 价格=%.3f 数量=%d 盈亏=%.2f%% %s' %
             (price, filled, pnl_pct, reason))
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_entry_date = None
    g.t_shares = 0


# ----------------------------------------------------------------------
def eod_check(context):
    sec = g.security
    pos = context.portfolio.positions.get(sec)
    actual = pos.total_amount if pos is not None else 0
    if g.t_state == 'short_t':
        days_held = 0
        if g.t_entry_date is not None:
            days_held = (context.current_dt.date() - g.t_entry_date).days
        log.info('[跨日持仓] short %d股 入场%.3f 已持%d天 底仓%d' %
                 (g.t_shares, g.t_entry_price, days_held, actual))


# ======================================================================
# 趋势过滤
# ======================================================================
def is_uptrend(sec, now):
    """判断当前是否处于强上升趋势。

    条件（任一满足即视为上升趋势，禁止开 short）：
      a) 收盘价 > 10日EMA × (1 + UPTREND_THRESHOLD)
      b) 10日EMA 较 3 天前上升幅度 > 1.5%

    日内同一天复用缓存，避免重复计算。
    """
    today = now.date()
    if g.trend_cache_date == today:
        return g.trend_is_up

    need = UPTREND_EMA_PERIOD + EMA_SLOPE_LOOKBACK + 5
    df = attribute_history(sec, need, '1d', ['close'], skip_paused=True)
    if df is None or len(df) < need - 2:
        g.trend_cache_date = today
        g.trend_is_up = False
        return False

    close = df['close']
    ema = close.ewm(span=UPTREND_EMA_PERIOD, adjust=False).mean()
    last_close = float(close.iloc[-1])
    last_ema = float(ema.iloc[-1])
    past_ema = float(ema.iloc[-1 - EMA_SLOPE_LOOKBACK])

    # 价格远离均线
    far_above = last_close > last_ema * (1 + UPTREND_THRESHOLD)
    # 均线斜率显著向上
    slope_up = (last_ema - past_ema) / past_ema > 0.015 if past_ema > 0 else False

    is_up = bool(far_above or slope_up)
    g.trend_cache_date = today
    g.trend_is_up = is_up
    if is_up:
        log.info('[趋势过滤] 上升趋势 close=%.3f ema=%.3f 偏离=%.2f%% 斜率=%.2f%%' %
                 (last_close, last_ema,
                  (last_close / last_ema - 1) * 100,
                  (last_ema / past_ema - 1) * 100 if past_ema > 0 else 0))
    return is_up


# ======================================================================
def calc_today_vwap(sec, now):
    minutes_passed = (now.hour - 9) * 60 + now.minute - 30
    if now.hour >= 13:
        minutes_passed -= 90
    minutes_passed = max(1, min(minutes_passed, 240))

    df = attribute_history(sec, minutes_passed, '1m',
                           ['volume', 'money'], skip_paused=True)
    if df is None or len(df) == 0:
        return None
    vol = float(df['volume'].sum())
    money = float(df['money'].sum())
    if vol <= 0:
        return None
    return money / vol


def cached_macd(sec, now):
    if g.macd_ts is None or (now - g.macd_ts).total_seconds() >= MACD_CACHE_SECONDS:
        g.macd_sig = calc_macd(sec)
        g.macd_ts = now
    return g.macd_sig


def calc_macd(sec):
    need = MACD_SLOW + MACD_SIGNAL + 5
    df = attribute_history(sec, need, '5m', ['close'], skip_paused=True)
    if df is None or len(df) < MACD_SLOW + MACD_SIGNAL:
        return 'neutral'

    close = df['close']
    ema_f = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_s = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = (dif - dea) * 2
    if len(hist) < 3:
        return 'neutral'

    h_last, h_prev = hist.iloc[-1], hist.iloc[-2]
    dif_last, dif_prev = dif.iloc[-1], dif.iloc[-2]
    dea_last, dea_prev = dea.iloc[-1], dea.iloc[-2]

    if dif_prev <= dea_prev and dif_last > dea_last and dif_last < 0:
        return 'bull'
    if h_prev < 0 < h_last:
        return 'bull'
    if dif_prev >= dea_prev and dif_last < dea_last and dif_last > 0:
        return 'bear'
    if h_prev > 0 > h_last:
        return 'bear'
    return 'neutral'
