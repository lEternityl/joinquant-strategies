# -*- coding: utf-8 -*-
"""
反向做 T 策略 v3（A股 T+1 友好版）

为什么只做反向回转
==================
A股实行 T+1 制度：当日买入的股票当日不能卖出。
这意味着"先买后卖"模式（正向回转）在日内必须依赖**昨日已建立**的库存
作为"卖出额度"。在策略首日、底仓刚建立时，没有任何可卖额度，正向回转
完全无法成立。

而"先卖后买"模式（反向回转）符合 T+1 规则：
- 上午高位卖出昨日已持有的底仓 → 释放现金
- 下午低位用现金买回 → 总持仓数量不变，赚取价差
- 当日买回的股票 T+1 锁定，明日变为新的底仓部分

这是 A股市场上**唯一在策略首日就可执行**的真正 T+0 行为。

策略逻辑
========
1. 首日（或资金账户首次接入）09:31 一次性建立底仓（T+1 锁定一日）
2. 从第 2 个交易日起，进入做 T 阶段：
   - 每根 1 分钟 bar 检查 VWAP 偏离 + 5min MACD 死叉/底背离信号
   - 触发开仓：卖出底仓的 T_POSITION_RATIO 比例
   - 触发平仓（止盈/止损/价格回归 VWAP）：用现金买回相同股数
3. 14:55 强制平仓做 T 仓位（防止隔夜持仓不一致）
4. 收盘核对：底仓是否完整恢复

核心安全约束
============
- pos.closeable_amount 是唯一可卖额度来源（即昨日及之前的底仓）
- 卖出后保留 (1 - T_POSITION_RATIO) 的底仓作为安全边际
- 买回时 g.t_shares 来自实际成交回填，不靠理论值
- 现金不足时按可用现金降量买回，并记录警告
- 不存在"延迟买入"概念：买回当日就完成，不会跨日

参数
====
SECURITY: 标的代码（深市 .XSHE，沪市 .XSHG）
BASE_POSITION_RATIO: 底仓占总资产比例
T_POSITION_RATIO: 单次T仓占底仓比例

使用
====
- 回测必须选"分钟"频率
- 首日全天不会做 T，仅建底仓（因 T+1 限制）
- 第 2 个交易日起开始做 T
"""

# ============ 参数 ============
SECURITY = '000426.XSHE'
BASE_POSITION_RATIO = 0.70
T_POSITION_RATIO = 0.30
MAX_T_PER_DAY = 3

GRID_TRIGGER_PCT = 0.008           # VWAP 偏离 0.8% 触发
TAKE_PROFIT_PCT = 0.008            # 止盈 0.8%
STOP_LOSS_PCT = 0.015              # 止损 1.5%

TRADE_START = '09:50'
TRADE_END = '14:30'
FORCE_CLOSE_TIME = '14:55'

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
    g.t_state = 'flat'              # 'flat' | 'short_t'
    g.t_entry_price = 0.0
    g.t_shares = 0
    g.t_count_today = 0
    g.macd_sig = 'neutral'
    g.macd_ts = None

    run_daily(daily_reset, time='before_open')
    run_daily(build_base, time='09:31')
    run_daily(on_bar, time='every_bar')
    run_daily(force_eod_close, time=FORCE_CLOSE_TIME)
    run_daily(eod_check, time='after_close')


# ----------------------------------------------------------------------
def daily_reset(context):
    """每日开盘前重置当日做T状态。"""
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_shares = 0
    g.t_count_today = 0
    g.macd_sig = 'neutral'
    g.macd_ts = None


# ----------------------------------------------------------------------
def build_base(context):
    """首日建底仓；之后只在底仓亏损时补足。"""
    sec = g.security
    cd = get_current_data()
    if cd[sec].paused:
        return

    pos = context.portfolio.positions.get(sec)
    held = pos.total_amount if pos is not None else 0

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
        return

    # 后续：仅在底仓不足时补
    if held < g.base_target_shares:
        miss = int((g.base_target_shares - held) / 100) * 100
        if miss < 100:
            return
        price = cd[sec].last_price
        need = miss * price * 1.005
        if context.portfolio.available_cash < need:
            log.warn('[底仓-补回] 现金不足 需%.0f 有%.0f' %
                     (need, context.portfolio.available_cash))
            return
        order(sec, miss)
        log.info('[底仓-补回] +%d股' % miss)


# ----------------------------------------------------------------------
def on_bar(context):
    """每分钟 bar 主逻辑。"""
    sec = g.security
    now = context.current_dt
    hhmm = now.strftime('%H:%M')

    if hhmm < TRADE_START or hhmm >= TRADE_END:
        return

    cd = get_current_data()
    if cd[sec].paused:
        return

    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.total_amount <= 0:
        return

    bar = attribute_history(sec, 1, '1m', ['close'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])

    vwap = calc_today_vwap(sec, now)
    if vwap is None or vwap <= 0:
        return
    deviation = (price - vwap) / vwap

    # ---- 持有空T仓 → 管理平仓 ----
    if g.t_state == 'short_t':
        pnl = (g.t_entry_price - price) / g.t_entry_price
        if pnl <= -STOP_LOSS_PCT:
            close_short(context, price, 'stop_loss %.2f%%' % (pnl * 100))
        elif pnl >= TAKE_PROFIT_PCT or price <= vwap:
            close_short(context, price, 'take_profit %.2f%%' % (pnl * 100))
        return

    # ---- 无仓位 → 考虑开仓 ----
    if g.t_count_today >= MAX_T_PER_DAY:
        return

    # 反向回转条件：股价显著高于 VWAP + MACD 转空
    if deviation < GRID_TRIGGER_PCT:
        return
    macd_sig = cached_macd(sec, now)
    if macd_sig != 'bear':
        return

    # closeable_amount > 0 才能卖出做空T；首日 closeable=0 自然跳过
    if pos.closeable_amount <= 0:
        return

    open_short(context, price, pos)


# ----------------------------------------------------------------------
def open_short(context, price, pos):
    """反向回转开仓：高位卖出底仓。"""
    sec = g.security
    base_value = g.base_target_shares * price
    sell_value = base_value * T_POSITION_RATIO
    target_shares = int(sell_value / price / 100) * 100

    # 安全边际：卖完后底仓不能跌破 (1 - T_POSITION_RATIO) * base_target
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
    g.t_shares = filled
    g.t_count_today += 1
    log.info('[开T空] 价格=%.3f 数量=%d 第%d次' %
             (price, filled, g.t_count_today))


def close_short(context, price, reason):
    """反向回转平仓：低位买回相应股数恢复底仓。"""
    sec = g.security
    if g.t_shares < 100:
        g.t_state = 'flat'
        return

    buy_shares = g.t_shares
    cost = buy_shares * price * 1.005
    available = context.portfolio.available_cash
    if available < cost:
        buy_shares = int(available * 0.95 / price / 100) * 100
    if buy_shares < 100:
        log.warn('[平T空-失败] 现金不足 available=%.0f' % available)
        return

    order(sec, buy_shares)
    pnl_pct = (g.t_entry_price - price) / g.t_entry_price * 100
    log.info('[平T空] 价格=%.3f 数量=%d 盈亏=%.2f%% %s' %
             (price, buy_shares, pnl_pct, reason))
    g.t_state = 'flat'
    g.t_entry_price = 0.0
    g.t_shares = 0


# ----------------------------------------------------------------------
def force_eod_close(context):
    """14:55 强平做T仓。"""
    sec = g.security
    cd = get_current_data()
    if cd[sec].paused:
        return
    price = cd[sec].last_price

    if g.t_state == 'short_t':
        close_short(context, price, 'force_EOD')

    open_orders = get_open_orders()
    for o in list(open_orders.values()):
        cancel_order(o)


def eod_check(context):
    """收盘后核对底仓完整性。"""
    sec = g.security
    pos = context.portfolio.positions.get(sec)
    actual = pos.total_amount if pos is not None else 0
    if g.base_built and actual < g.base_target_shares:
        log.warn('[尾盘核对] 底仓不足 实际%d 目标%d' %
                 (actual, g.base_target_shares))


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
