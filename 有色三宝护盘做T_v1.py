# -*- coding: utf-8 -*-
"""
有色三宝护盘做T 策略 v1
=========================
针对持有【北方稀土 600111 / 兴业银锡 000426 / 紫金矿业 601899】
（高相关性有色板块）的护盘+做T一体化策略。

行情诊断（基于历史盈亏）
------------------------
- 胜率约 44%，亏多盈少
- 三只共振性极高（有色板块β），等于一笔敞口
- 单日大亏（>800）多于大盈
- 5/14~5/21 连续 6 日亏 → 趋势下跌时缺乏止损
- 盈利日常被次日吞掉 → 缺少锁利机制

策略核心
--------
1. 板块共振过滤
   - 计算三只 5 日均价的同步跌幅
   - 若三只同跌且板块指数（801050 有色金属）下行 → 触发"板块避险模式"
   - 避险模式下：禁止加仓，触发轻减仓（卖出 10% 应急减压）

2. 正向 T（低买高卖，与反向 T 互补）
   - 急跌买入条件：
     * 价格 < 当日 VWAP × (1 - 1.0%)
     * 5min MACD 金叉
     * 板块未处于强势下跌
   - 卖出条件：
     * 浮盈 >= 1.2% 锁利
     * 价格回归 VWAP 且盈利
     * 持仓 >= 2 天强平
     * 浮亏 >= 2.5% 止损

3. 单股权重隔离
   - 每只股票独立状态机，互不影响
   - 单只仓位上限 = 总资产 × 35%
   - 单只单笔做 T 上限 = 该股底仓 × 25%

4. 大盘+板块双重止损
   - 沪深300 单日跌幅 > 1.5% → 当日禁止开仓
   - 板块指数 5 日累计跌 > 5% → 进入避险模式
"""

# ============ 标的与权重 ============
SECURITIES = {
    '600111.XSHG': {'name': '北方稀土', 'weight': 0.30, 'shares_init': 100},
    '000426.XSHE': {'name': '兴业银锡', 'weight': 0.25, 'shares_init': 100},
    '601899.XSHG': {'name': '紫金矿业', 'weight': 0.35, 'shares_init': 400},
}
INDUSTRY_INDEX = '801050.XSHG'   # 申万有色金属指数
MARKET_INDEX = '000300.XSHG'

# ============ 仓位参数 ============
SINGLE_MAX_RATIO = 0.35           # 单只最大仓位
T_POSITION_RATIO = 0.20           # 单次 T 仓占该只底仓
MIN_BASE_RATIO = 0.60             # 单只底仓最低保留比

# ============ 做T参数 ============
GRID_BUY_PCT = 0.010              # 低于 VWAP 1% 触发买
GRID_SELL_PCT = 0.010             # 高于 VWAP 1% 触发卖
TAKE_PROFIT_PCT = 0.012           # 锁利 1.2%
STOP_LOSS_PCT = 0.025             # 止损 2.5%
OVERNIGHT_STOP_PCT = 0.015        # 隔夜止损 1.5%
MAX_HOLD_DAYS = 2

# ============ 风控参数 ============
MARKET_BAN_PCT = -0.015           # 沪深300当日跌1.5%以上禁开
INDUSTRY_PANIC_PCT = -0.05        # 板块5日跌5%以上进入避险
INDUSTRY_DAILY_PCT = -0.02        # 板块当日跌2%加强避险
EMERGENCY_SELL_RATIO = 0.10       # 板块避险时减仓比例

# ============ 时间窗 ============
TRADE_START = '09:45'
OVERNIGHT_CHECK_START = '09:35'
OPEN_CUTOFF = '14:30'
END_OF_DAY = '14:55'
PROFIT_LOCK_TIME = '14:50'        # 当日盈利锁定时刻

# ============ 技术指标 ============
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_CACHE_SECONDS = 300


# ======================================================================
def initialize(context):
    set_benchmark(MARKET_INDEX)
    set_option('use_real_price', True)
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5,
    ), type='stock')
    set_slippage(PriceRelatedSlippage(0.00246))

    g.base_built = False
    g.base_shares = {}                 # 每只股票的底仓份额
    g.t_state = {}                     # {sec: 'flat'|'long_t'}
    g.t_entry_price = {}
    g.t_entry_date = {}
    g.t_shares = {}
    g.macd_cache = {}                  # {sec: ('bull'|..., timestamp)}
    g.industry_panic = False           # 板块避险模式
    g.market_ban_today = False         # 当日大盘禁开
    g.day_start_value = None           # 当日开盘账户价值
    g.profit_locked_today = False

    for sec in SECURITIES:
        g.t_state[sec] = 'flat'
        g.t_entry_price[sec] = 0.0
        g.t_entry_date[sec] = None
        g.t_shares[sec] = 0
        g.macd_cache[sec] = ('neutral', None)

    run_daily(daily_reset, time='before_open')
    run_daily(check_market_risk, time='09:30')
    run_daily(build_base, time='09:31')
    run_daily(check_industry_panic, time='09:40')
    run_daily(on_bar, time='every_bar')
    run_daily(eod_check, time='after_close')


# ----------------------------------------------------------------------
def daily_reset(context):
    g.market_ban_today = False
    g.profit_locked_today = False
    g.day_start_value = context.portfolio.total_value
    for sec in SECURITIES:
        g.macd_cache[sec] = ('neutral', None)


# ----------------------------------------------------------------------
def check_market_risk(context):
    """开盘前检查大盘风险。"""
    df = attribute_history(MARKET_INDEX, 2, '1d', ['close'])
    if df is None or len(df) < 2:
        return
    prev_close = float(df['close'].iloc[-2])
    cd = get_current_data()
    cur = cd[MARKET_INDEX].last_price
    if cur <= 0:
        return
    pct = (cur - prev_close) / prev_close
    if pct <= MARKET_BAN_PCT:
        g.market_ban_today = True
        log.warn('[大盘禁开] 沪深300当日 %.2f%%' % (pct * 100))


# ----------------------------------------------------------------------
def build_base(context):
    """首日按初始持仓建立底仓。"""
    if g.base_built:
        return
    cd = get_current_data()
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        target = conf['shares_init']
        pos = context.portfolio.positions.get(sec)
        held = pos.total_amount if pos else 0
        if held >= target:
            g.base_shares[sec] = held
            continue
        diff = target - held
        if order(sec, diff) is not None:
            g.base_shares[sec] = target
            log.info('[底仓-%s] %d股' % (conf['name'], target))
    g.base_built = True


# ----------------------------------------------------------------------
def check_industry_panic(context):
    """板块共振避险检查。"""
    df = attribute_history(INDUSTRY_INDEX, 6, '1d', ['close'])
    if df is None or len(df) < 6:
        g.industry_panic = False
        return
    close = df['close']
    pct_5d = (float(close.iloc[-1]) - float(close.iloc[0])) / float(close.iloc[0])
    pct_1d = (float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2])

    panic = (pct_5d <= INDUSTRY_PANIC_PCT) or (pct_1d <= INDUSTRY_DAILY_PCT)
    if panic and not g.industry_panic:
        log.warn('[板块避险] 5日%.2f%% 当日%.2f%% 触发减仓'
                 % (pct_5d * 100, pct_1d * 100))
        emergency_reduce(context)
    g.industry_panic = panic


def emergency_reduce(context):
    """避险减仓：每只卖 EMERGENCY_SELL_RATIO 仓位。"""
    cd = get_current_data()
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        pos = context.portfolio.positions.get(sec)
        if pos is None or pos.closeable_amount <= 0:
            continue
        sell = int(pos.closeable_amount * EMERGENCY_SELL_RATIO / 100) * 100
        if sell < 100:
            continue
        order(sec, -sell)
        log.info('[避险减仓-%s] %d股' % (conf['name'], sell))


# ----------------------------------------------------------------------
def on_bar(context):
    now = context.current_dt
    hhmm = now.strftime('%H:%M')
    if hhmm >= END_OF_DAY:
        return

    # 当日盈利锁定（盈利后吐回保护）
    if hhmm >= PROFIT_LOCK_TIME and not g.profit_locked_today:
        try_lock_profit(context)
        g.profit_locked_today = True

    cd = get_current_data()
    for sec in SECURITIES:
        if cd[sec].paused:
            continue
        process_security(context, sec, now, hhmm, cd)


def process_security(context, sec, now, hhmm, cd):
    bar = attribute_history(sec, 1, '1m', ['close'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])

    # 跨日持仓早盘检查
    if g.t_state[sec] == 'long_t' and hhmm >= OVERNIGHT_CHECK_START:
        if g.t_entry_date[sec] is not None and now.date() > g.t_entry_date[sec]:
            pnl = (price - g.t_entry_price[sec]) / g.t_entry_price[sec]
            if pnl <= -OVERNIGHT_STOP_PCT:
                do_close_long(context, sec, price,
                              'overnight_stop %.2f%%' % (pnl * 100), cd)
                return

    if hhmm < TRADE_START:
        return

    vwap = calc_today_vwap(sec, now)
    if vwap is None or vwap <= 0:
        return

    # 持有 long_t 仓：考虑卖出
    if g.t_state[sec] == 'long_t':
        try_close_long(context, sec, price, vwap, now, cd)
        return

    # 开仓阶段
    if hhmm >= OPEN_CUTOFF:
        return
    if g.market_ban_today or g.industry_panic:
        return

    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.total_amount <= 0:
        return

    # 仓位上限保护
    sec_value = pos.total_amount * price
    if sec_value >= context.portfolio.total_value * SINGLE_MAX_RATIO:
        return

    # 急跌触发：低于 VWAP 1%
    deviation = (price - vwap) / vwap
    if deviation > -GRID_BUY_PCT:
        return

    # MACD 金叉（防接飞刀）
    if cached_macd(sec, now) != 'bull':
        return

    open_long(context, sec, price, pos, now)


# ----------------------------------------------------------------------
def open_long(context, sec, price, pos, now):
    base = g.base_shares.get(sec, pos.total_amount)
    buy_value = base * price * T_POSITION_RATIO
    cash = context.portfolio.available_cash
    buy_value = min(buy_value, cash * 0.95)
    buy_shares = int(buy_value / price / 100) * 100
    if buy_shares < 100:
        return

    order_obj = order(sec, buy_shares)
    if order_obj is None:
        return
    filled = int(order_obj.filled) if order_obj.filled else 0
    if filled < 100:
        return

    g.t_state[sec] = 'long_t'
    g.t_entry_price[sec] = price
    g.t_entry_date[sec] = now.date()
    g.t_shares[sec] = filled
    log.info('[正T开-%s] 价%.3f 量%d' %
             (SECURITIES[sec]['name'], price, filled))


def try_close_long(context, sec, price, vwap, now, cd):
    entry = g.t_entry_price[sec]
    pnl = (price - entry) / entry
    days_held = 0
    if g.t_entry_date[sec] is not None:
        days_held = (now.date() - g.t_entry_date[sec]).days

    reason = None
    if pnl <= -STOP_LOSS_PCT:
        reason = 'stop_loss %.2f%%' % (pnl * 100)
    elif days_held >= MAX_HOLD_DAYS:
        reason = 'max_hold_%dd' % days_held
    elif pnl >= TAKE_PROFIT_PCT:
        reason = 'take_profit %.2f%%' % (pnl * 100)
    elif price >= vwap and pnl > 0:
        reason = 'vwap_revert %.2f%%' % (pnl * 100)
    else:
        return
    do_close_long(context, sec, price, reason, cd)


def do_close_long(context, sec, price, reason, cd):
    shares = g.t_shares[sec]
    if shares < 100:
        g.t_state[sec] = 'flat'
        return
    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.closeable_amount < 100:
        return
    sell = min(shares, pos.closeable_amount)
    sell = int(sell / 100) * 100
    if sell < 100:
        return

    low_limit = cd[sec].low_limit
    if low_limit > 0 and price <= low_limit * 1.002:
        log.info('[正T平-跳过] 接近跌停 %.3f' % price)
        return

    order_obj = order(sec, -sell)
    if order_obj is None:
        return
    filled = int(order_obj.filled) if order_obj.filled else 0
    if filled < 100:
        return

    pnl_pct = (price - g.t_entry_price[sec]) / g.t_entry_price[sec] * 100
    log.info('[正T平-%s] 价%.3f 量%d 盈亏%.2f%% %s' %
             (SECURITIES[sec]['name'], price, filled, pnl_pct, reason))
    g.t_state[sec] = 'flat'
    g.t_entry_price[sec] = 0.0
    g.t_entry_date[sec] = None
    g.t_shares[sec] = 0


# ----------------------------------------------------------------------
def try_lock_profit(context):
    """当日大幅盈利后强制锁定（防吐回）。

    规则：当日浮盈 > 1.5% 时，把持有的所有 long_t 仓位平掉。
    """
    if g.day_start_value is None or g.day_start_value <= 0:
        return
    cur = context.portfolio.total_value
    pct = (cur - g.day_start_value) / g.day_start_value
    if pct < 0.015:
        return
    cd = get_current_data()
    for sec in SECURITIES:
        if g.t_state[sec] != 'long_t':
            continue
        bar = attribute_history(sec, 1, '1m', ['close'], skip_paused=True)
        if bar is None or len(bar) == 0:
            continue
        price = float(bar['close'].iloc[-1])
        if price > g.t_entry_price[sec]:
            do_close_long(context, sec, price,
                          'profit_lock day+%.2f%%' % (pct * 100), cd)


# ----------------------------------------------------------------------
def eod_check(context):
    cur = context.portfolio.total_value
    if g.day_start_value:
        pct = (cur - g.day_start_value) / g.day_start_value * 100
        log.info('[日终] 总值%.0f 当日%.2f%% 板块避险=%s' %
                 (cur, pct, g.industry_panic))
    for sec in SECURITIES:
        if g.t_state[sec] == 'long_t':
            days = 0
            if g.t_entry_date[sec]:
                days = (context.current_dt.date() - g.t_entry_date[sec]).days
            log.info('  跨日 %s %d股 入场%.3f 已持%d天'
                     % (SECURITIES[sec]['name'], g.t_shares[sec],
                        g.t_entry_price[sec], days))


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
    sig, ts = g.macd_cache.get(sec, ('neutral', None))
    if ts is None or (now - ts).total_seconds() >= MACD_CACHE_SECONDS:
        sig = calc_macd(sec)
        g.macd_cache[sec] = (sig, now)
    return sig


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
