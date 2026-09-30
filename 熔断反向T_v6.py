# -*- coding: utf-8 -*-
"""
熔断反向 T 策略 v6（短期暴涨熔断 + 趋势过滤 + 隔夜风险）

核心机制
========
在 v5 趋势过滤基础上，增加"短期暴涨熔断"主动避险：

【触发条件】（任一满足即进入避险态）
  1. 3 日累计涨幅 >= 8%
  2. 单日涨幅 >= 5%
  3. 价格距离 10 日 EMA >= 10%

【避险态行为】
  - 立即清掉持有的 short T 仓（即使亏损也止损出局，避免被持续轧空）
  - 冻结所有新开仓（即使 VWAP 偏离 + MACD 死叉满足也不开）
  - 跟踪 g.surge_peak_price = 自进入避险态以来的最高价

【退出避险态】（任一满足）
  1. 价格从 surge_peak_price 回调 >= 5%
  2. 价格回到 10 日 EMA 的 ±2% 范围内
  3. 进入避险态超过 10 个交易日（防止永远卡住）
    
为什么这样设计
==============
反向 T 的最大敌人是"单边轧空"。v4-v5 的所有 -5% 以上止损都发生在
"卖了之后股票还在涨"的场景。被动止损永远慢半拍，主动避险是唯一出路：
看到苗头不对就**离场观察**，等价格"真的回调下来"再说。

宁可错过，不可错杀。

其他保留 v5 的所有约束
=====================
- 趋势过滤（10日EMA偏离/斜率）
- 隔夜浮亏 1.5% 强平
- 最大持仓 2 天
- T_POSITION_RATIO 20%
- VWAP 偏离 + 5min MACD 双确认
"""

# ============ 参数 ============
SECURITY = '000426.XSHE'
BASE_POSITION_RATIO = 0.70
T_POSITION_RATIO = 0.20
MAX_T_PER_DAY = 2

GRID_TRIGGER_PCT = 0.010
TAKE_PROFIT_PCT = 0.010
STOP_LOSS_PCT = 0.030
OVERNIGHT_STOP_PCT = 0.015

MAX_HOLD_DAYS = 2
MIN_BASE_RATIO = 0.50

# 趋势过滤（v5 保留）
UPTREND_EMA_PERIOD = 10
UPTREND_THRESHOLD = 0.03
EMA_SLOPE_LOOKBACK = 3

# 短期暴涨熔断（v6 新增）
SURGE_3D_THRESHOLD = 0.08           # 3 日累计涨幅 >= 8%
SURGE_1D_THRESHOLD = 0.05           # 单日涨幅 >= 5%
SURGE_FAR_FROM_EMA = 0.10           # 距离 10日EMA >= 10%
PULLBACK_THRESHOLD = 0.05           # 从高点回调 5% 退出避险
EMA_NEAR_BAND = 0.02                # 价格回到 EMA ±2% 退出避险
MAX_RISK_MODE_DAYS = 10             # 避险态最长 10 天

TRADE_START = '09:50'
OVERNIGHT_CHECK_START = '09:35'
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

    # 趋势 & 风险态缓存
    g.trend_cache_date = None
    g.trend_is_up = False
    g.risk_mode = 'normal'              # 'normal' | 'surge_alert'
    g.risk_enter_date = None
    g.surge_peak_price = 0.0

    run_daily(daily_reset, time='before_open')
    run_daily(build_base, time='09:31')
    run_daily(surge_detector, time='09:32')     # 每日开盘先做熔断检测
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
# 短期暴涨熔断
# ----------------------------------------------------------------------
def surge_detector(context):
    """每日 09:32 检测是否进入避险态，或是否退出避险态。"""
    sec = g.security
    cd = get_current_data()
    if cd[sec].paused:
        return

    df = attribute_history(sec, 12, '1d', ['close'], skip_paused=True)
    if df is None or len(df) < 5:
        return

    last_close = float(df['close'].iloc[-1])
    prev_close = float(df['close'].iloc[-2])
    three_ago = float(df['close'].iloc[-4]) if len(df) >= 4 else prev_close

    # 涨幅指标
    daily_up = (last_close - prev_close) / prev_close if prev_close > 0 else 0
    three_day_up = (last_close - three_ago) / three_ago if three_ago > 0 else 0

    # EMA 偏离
    ema = df['close'].ewm(span=UPTREND_EMA_PERIOD, adjust=False).mean()
    last_ema = float(ema.iloc[-1])
    ema_dev = (last_close - last_ema) / last_ema if last_ema > 0 else 0

    today = context.current_dt.date()

    # ---- 当前处于 normal：检查是否进入避险态 ----
    if g.risk_mode == 'normal':
        triggered = False
        reason = ''
        if three_day_up >= SURGE_3D_THRESHOLD:
            triggered = True
            reason = '3日涨幅%.2f%%' % (three_day_up * 100)
        elif daily_up >= SURGE_1D_THRESHOLD:
            triggered = True
            reason = '单日涨%.2f%%' % (daily_up * 100)
        elif ema_dev >= SURGE_FAR_FROM_EMA:
            triggered = True
            reason = '远离EMA %.2f%%' % (ema_dev * 100)

        if triggered:
            g.risk_mode = 'surge_alert'
            g.risk_enter_date = today
            g.surge_peak_price = last_close
            log.warn('[熔断-进入避险] %s 价格%.3f' % (reason, last_close))

            # 立即清掉 short T 仓
            if g.t_state == 'short_t':
                price = cd[sec].last_price
                do_close_short(context, price, 'risk_mode_enter', cd)
        return

    # ---- 当前处于 surge_alert：检查是否退出 ----
    # 更新历史高点
    if last_close > g.surge_peak_price:
        g.surge_peak_price = last_close

    days_in_risk = (today - g.risk_enter_date).days if g.risk_enter_date else 0
    pullback = (g.surge_peak_price - last_close) / g.surge_peak_price \
        if g.surge_peak_price > 0 else 0
    near_ema = abs(ema_dev) <= EMA_NEAR_BAND

    exit_reason = None
    if pullback >= PULLBACK_THRESHOLD:
        exit_reason = '回调%.2f%%' % (pullback * 100)
    elif near_ema:
        exit_reason = '回归EMA偏离%.2f%%' % (ema_dev * 100)
    elif days_in_risk >= MAX_RISK_MODE_DAYS:
        exit_reason = '超时%d天' % days_in_risk

    if exit_reason:
        log.info('[熔断-退出避险] %s 价格%.3f 高点%.3f' %
                 (exit_reason, last_close, g.surge_peak_price))
        g.risk_mode = 'normal'
        g.risk_enter_date = None
        g.surge_peak_price = 0.0


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

    # ---- 隔夜浮亏检查（即使在避险态也要执行）----
    if g.t_state == 'short_t' and hhmm >= OVERNIGHT_CHECK_START:
        is_overnight = (g.t_entry_date is not None
                        and now.date() > g.t_entry_date)
        if is_overnight:
            pnl = (g.t_entry_price - price) / g.t_entry_price
            if pnl <= -OVERNIGHT_STOP_PCT:
                do_close_short(context, price,
                               'overnight_stop %.2f%%' % (pnl * 100), cd)
                return

    if hhmm < TRADE_START:
        return

    vwap = calc_today_vwap(sec, now)
    if vwap is None or vwap <= 0:
        return

    # ---- 持有 short：考虑平仓（避险态下也要止盈/止损/到期）----
    if g.t_state == 'short_t':
        try_close_short(context, price, vwap, now, cd)
        return

    # ---- 避险态：禁止开新仓 ----
    if g.risk_mode == 'surge_alert':
        return

    # ---- 无仓位 + normal 态：考虑开仓 ----
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
        log.info('[跨日持仓] short %d股 入场%.3f 已持%d天 底仓%d 风险态=%s' %
                 (g.t_shares, g.t_entry_price, days_held, actual, g.risk_mode))


# ======================================================================
def is_uptrend(sec, now):
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

    far_above = last_close > last_ema * (1 + UPTREND_THRESHOLD)
    slope_up = (last_ema - past_ema) / past_ema > 0.015 if past_ema > 0 else False

    is_up = bool(far_above or slope_up)
    g.trend_cache_date = today
    g.trend_is_up = is_up
    return is_up


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
