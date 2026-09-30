# -*- coding: utf-8 -*-
"""
波段反向 T 策略 v4（不强制日内闭环版）

设计理念
========
本策略放弃"日内必须平仓"的约束。核心洞察：
- 反向 T（卖底仓→买回）赚的是**价差**而非时间
- 强制 14:55 买回会变成"追涨"，是负EV行为（实测胜率不到一半）
- A股 T+1 允许卖出仓位现金过夜，明日继续等买点

行为
====
卖出条件（开 short T）：
  - 价格 >= VWAP + GRID_TRIGGER_PCT
  - 5min MACD 死叉
  - 当前底仓 closeable_amount >= 计划卖出量
  - 当日做 T 次数未达上限

买回条件（close short T）：满足任一即触发
  a) 止盈：浮盈 >= TAKE_PROFIT_PCT
  b) 止损：浮亏 >= STOP_LOSS_PCT（硬约束）
  c) 价格回归：current <= VWAP（最优买回区）
  d) 跨日仍持仓时：每日检查买回条件，触发即买
  e) 持仓超过 MAX_HOLD_DAYS 天：强制止损买回（防止深套）

时间窗
======
- TRADE_START 后允许开仓和平仓
- 14:30 后只允许平仓，不开新仓
- 不再有 14:55 强制 force_EOD（除非超过最大持有天数）

风险约束
========
- 任何时点持有的 short T 仓 <= 当前底仓的 T_POSITION_RATIO
- 现金不足时按可用现金降量买回
- 涨停日跳过买回（保留 short 仓位待次日）
- 底仓低于阈值时禁止新开 short

状态
====
g.t_state: 'flat' | 'short_t'
g.t_entry_price: 开仓价
g.t_entry_date: 开仓日期（用于计算持仓天数）
g.t_shares: 已卖出股数
g.t_count_today: 当日做 T 次数

使用
====
- 分钟级回测
- 首日只建底仓不做 T（无可卖额度）
- 建议回测 >= 3 个月，观察波段效果
"""

# ============ 参数 ============
SECURITY = '000426.XSHE'
BASE_POSITION_RATIO = 0.70
T_POSITION_RATIO = 0.30
MAX_T_PER_DAY = 2

GRID_TRIGGER_PCT = 0.010           # 高位卖出阈值 1.0%
TAKE_PROFIT_PCT = 0.010            # 买回止盈 1.0%
STOP_LOSS_PCT = 0.030              # 硬止损 3.0%（放宽，配合跨日持仓）

MAX_HOLD_DAYS = 5                  # short 仓最多持有 5 个交易日，超过强平
MIN_BASE_RATIO = 0.50              # 底仓占总资产低于此比例时禁止再开 short

TRADE_START = '09:50'
OPEN_CUTOFF = '14:30'              # 14:30 后不开新仓（但仍可平仓）
END_OF_DAY = '14:55'               # 14:55 后停止任何交易

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

    run_daily(daily_reset, time='before_open')
    run_daily(build_base, time='09:31')
    run_daily(on_bar, time='every_bar')
    run_daily(eod_check, time='after_close')


# ----------------------------------------------------------------------
def daily_reset(context):
    """每日重置当日计数；T仓状态跨日保留。"""
    g.t_count_today = 0
    g.macd_sig = 'neutral'
    g.macd_ts = None


# ----------------------------------------------------------------------
def build_base(context):
    """首日建底仓；后续不强制补回（让波段交易自然恢复）。"""
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
    """分钟主循环。"""
    sec = g.security
    now = context.current_dt
    hhmm = now.strftime('%H:%M')

    if hhmm < TRADE_START or hhmm >= END_OF_DAY:
        return

    cd = get_current_data()
    if cd[sec].paused:
        return

    bar = attribute_history(sec, 1, '1m', ['close'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])

    vwap = calc_today_vwap(sec, now)
    if vwap is None or vwap <= 0:
        return

    # ---- 持有 short T 仓：考虑买回 ----
    if g.t_state == 'short_t':
        try_close_short(context, price, vwap, now, cd)
        return

    # ---- 无仓位：考虑开 short T ----
    if hhmm >= OPEN_CUTOFF:
        return  # 14:30 后不开新仓
    if g.t_count_today >= MAX_T_PER_DAY:
        return

    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.total_amount <= 0:
        return

    # 底仓占比过低时禁止再开 short
    if pos.total_amount * price < context.portfolio.total_value * MIN_BASE_RATIO:
        return

    if pos.closeable_amount <= 0:
        return  # T+1 限制

    deviation = (price - vwap) / vwap
    if deviation < GRID_TRIGGER_PCT:
        return

    macd_sig = cached_macd(sec, now)
    if macd_sig != 'bear':
        return

    open_short(context, price, pos, now)


# ----------------------------------------------------------------------
def open_short(context, price, pos, now):
    """高位卖出底仓部分。"""
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
        log.info('[开T空-放弃] 成交不足100股 filled=%d' % filled)
        return

    g.t_state = 'short_t'
    g.t_entry_price = price
    g.t_entry_date = now.date()
    g.t_shares = filled
    g.t_count_today += 1
    log.info('[开T空] 价格=%.3f 数量=%d 第%d次' %
             (price, filled, g.t_count_today))


def try_close_short(context, price, vwap, now, cd):
    """检查是否触发买回条件，触发则平仓。"""
    sec = g.security
    pnl = (g.t_entry_price - price) / g.t_entry_price

    # 持仓天数检查
    days_held = 0
    if g.t_entry_date is not None:
        days_held = (now.date() - g.t_entry_date).days

    reason = None

    # 优先级：硬止损 > 最大持仓天数 > 止盈 > 价格回归
    if pnl <= -STOP_LOSS_PCT:
        reason = 'stop_loss %.2f%%' % (pnl * 100)
    elif days_held >= MAX_HOLD_DAYS:
        reason = 'max_hold_days %d天' % days_held
    elif pnl >= TAKE_PROFIT_PCT:
        reason = 'take_profit %.2f%%' % (pnl * 100)
    elif price <= vwap and pnl > 0:
        # 价格回归到 VWAP 下方，且还在盈利 → 锁定利润
        reason = 'vwap_revert %.2f%%' % (pnl * 100)
    else:
        return

    do_close_short(context, price, reason, cd)


def do_close_short(context, price, reason, cd):
    """执行买回。涨停时跳过等次日。"""
    sec = g.security
    if g.t_shares < 100:
        g.t_state = 'flat'
        return

    # 涨停检查：last_price 接近涨停价时跳过买回
    high_limit = cd[sec].high_limit
    if high_limit > 0 and price >= high_limit * 0.998:
        log.info('[平T空-跳过] 接近涨停 price=%.3f limit=%.3f，待次日' %
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
        log.info('[平T空-未成交] filled=%d，待次日' % filled)
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
    """收盘核对：记录跨日持仓状态。"""
    sec = g.security
    pos = context.portfolio.positions.get(sec)
    actual = pos.total_amount if pos is not None else 0
    if g.t_state == 'short_t':
        days_held = 0
        if g.t_entry_date is not None:
            days_held = (context.current_dt.date() - g.t_entry_date).days
        log.info('[跨日持仓] short_t %d股 入场价%.3f 已持%d天 当前底仓%d' %
                 (g.t_shares, g.t_entry_price, days_held, actual))
    elif g.base_built and actual < g.base_target_shares:
        log.info('[底仓状态] 实际%d 目标%d（差%d 等买点恢复）' %
                 (actual, g.base_target_shares, g.base_target_shares - actual))


# ======================================================================
def calc_today_vwap(sec, now):
    """当日累计 VWAP。"""
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
    """带缓存的 5min MACD 信号。"""
    if g.macd_ts is None or (now - g.macd_ts).total_seconds() >= MACD_CACHE_SECONDS:
        g.macd_sig = calc_macd(sec)
        g.macd_ts = now
    return g.macd_sig


def calc_macd(sec):
    """5min MACD 信号：'bull' / 'bear' / 'neutral'。"""
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
