# -*- coding: utf-8 -*-
"""
周内日历 T 策略 v1
====================
利用 A 股周内日历效应，每周最多 2 次操作：
- 周一 / 周二 / 周三 中某天若大涨 -> 卖出 T 仓（高位减仓）
- 周四 / 周五 中某天若大跌 -> 买回 T 仓（低位补仓）

策略行为
--------
1. 每周一开盘前重置周内交易状态（已卖/已买标记清零）
2. 周一 ~ 周三 14:50 检查当日涨幅，若 >= SELL_TRIGGER_PCT 立即卖 T
   - 一周仅触发一次，第一个达标日生效
3. 周四 ~ 周五 14:50 检查当日跌幅，若 <= BUY_TRIGGER_PCT 立即买 T
   - 一周仅触发一次，必须当周已经卖过才能买（保留资金优先级）
   - 也可独立买入（在 INDEPENDENT_BUY=True 时不要求先卖）

适用场景
--------
- 高 β 周期股（有色、券商、地产）
- 已有底仓的"减亏增厚"型操作
- 不适合单边强趋势行情

使用前请按账户实际持仓修改 SECURITIES 字典中的 init_shares 字段
"""

# ============ 标的与初始持仓 ============
SECURITIES = {
    '600111.XSHG': {'name': '北方稀土', 'init_shares': 100},
    '000426.XSHE': {'name': '兴业银锡', 'init_shares': 100},
    '601899.XSHG': {'name': '紫金矿业', 'init_shares': 400},
}

# ============ 触发阈值 ============
SELL_TRIGGER_PCT = 0.030      # 当日涨幅 >= 3% 触发卖
BUY_TRIGGER_PCT = -0.025      # 当日跌幅 <= -2.5% 触发买
T_RATIO = 0.25                # 单次 T 仓占该只底仓比例

# ============ 行为开关 ============
INDEPENDENT_BUY = True        # 周四五买入不依赖周一二三是否已卖
USE_INTRADAY_PEAK = True      # True=用日内最高/最低价判触发, False=用现价

# ============ 时间 ============
CHECK_TIME = '14:50'          # 每日尾盘判定与下单时间
END_OF_DAY = '14:55'

# ============ 风控 ============
MARKET_INDEX = '000300.XSHG'
MARKET_BAN_PCT = -0.020       # 沪深300当日跌2%以上停止买入（防接飞刀）
MIN_BASE_SHARES_RATIO = 0.50  # 卖出后保底比例


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
    g.base_shares = {}        # {sec: 底仓股数}
    g.t_shares = {}           # {sec: 已建立的 T 仓股数（>0 表示已加仓）}
    g.week_sold = False       # 本周是否已经卖出
    g.week_bought = False     # 本周是否已经买入
    g.week_anchor = None      # 本周锚点日期（周一）

    for sec in SECURITIES:
        g.t_shares[sec] = 0

    run_daily(weekly_reset_check, time='before_open')
    run_daily(build_base, time='09:31')
    run_daily(daily_decision, time=CHECK_TIME)
    run_daily(eod_log, time='after_close')


# ----------------------------------------------------------------------
def weekly_reset_check(context):
    """每周一 before_open 重置周内状态。"""
    today = context.current_dt.date()
    weekday = today.weekday()  # 0=Mon
    # 计算本周一日期
    monday = today - _timedelta(days=weekday)
    if g.week_anchor != monday:
        g.week_anchor = monday
        g.week_sold = False
        g.week_bought = False
        log.info('[周重置] 新一周锚点 %s' % monday)


def _timedelta(days):
    """避免依赖外部 import，简单包装。"""
    import datetime as _dt
    return _dt.timedelta(days=days)


# ----------------------------------------------------------------------
def build_base(context):
    if g.base_built:
        return
    cd = get_current_data()
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        target = conf['init_shares']
        pos = context.portfolio.positions.get(sec)
        held = pos.total_amount if pos else 0
        if held < target:
            diff = target - held
            if order(sec, diff) is not None:
                log.info('[底仓-%s] 补足至%d股' % (conf['name'], target))
        g.base_shares[sec] = target
    g.base_built = True


# ----------------------------------------------------------------------
def daily_decision(context):
    """每日 14:50 判定本日是否触发卖出或买入。"""
    weekday = context.current_dt.weekday()  # 0=Mon ... 4=Fri
    if weekday > 4:
        return

    # 周一二三 -> 看是否大涨卖出
    if weekday in (0, 1, 2):
        if not g.week_sold:
            try_sell(context)
        return

    # 周四五 -> 看是否大跌买入
    if weekday in (3, 4):
        if g.week_bought:
            return
        if not INDEPENDENT_BUY and not g.week_sold:
            return
        try_buy(context)


# ----------------------------------------------------------------------
def try_sell(context):
    """检查当日涨幅，符合条件则卖出。"""
    cd = get_current_data()
    sold_any = False
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        change = day_change_pct(sec, cd, use_peak=USE_INTRADAY_PEAK,
                                direction='up')
        if change is None or change < SELL_TRIGGER_PCT:
            continue

        pos = context.portfolio.positions.get(sec)
        if pos is None or pos.closeable_amount <= 0:
            continue

        base = g.base_shares.get(sec, pos.total_amount)
        plan_sell = int(base * T_RATIO / 100) * 100
        keep_min = int(base * MIN_BASE_SHARES_RATIO)
        max_sellable = pos.closeable_amount - keep_min
        sell = min(plan_sell, max_sellable)
        sell = int(sell / 100) * 100
        if sell < 100:
            continue

        price = cd[sec].last_price
        high_limit = cd[sec].high_limit
        if high_limit > 0 and price >= high_limit * 0.999:
            log.info('[周内卖-跳过] %s 涨停 %.3f' % (conf['name'], price))
            continue

        order_obj = order(sec, -sell)
        if order_obj is None:
            continue
        filled = int(order_obj.filled) if order_obj.filled else 0
        if filled < 100:
            continue
        g.t_shares[sec] = g.t_shares.get(sec, 0) - filled
        sold_any = True
        log.info('[周内卖-%s] 涨幅%.2f%% 价%.3f 量%d'
                 % (conf['name'], change * 100, price, filled))

    if sold_any:
        g.week_sold = True


def try_buy(context):
    """检查当日跌幅，符合条件则买入。"""
    cd = get_current_data()
    # 大盘极端日避险
    mkt_change = day_change_pct(MARKET_INDEX, cd, use_peak=False,
                                direction='down')
    if mkt_change is not None and mkt_change <= MARKET_BAN_PCT:
        log.warn('[周内买-禁] 大盘 %.2f%%, 暂停接飞刀' % (mkt_change * 100))
        return

    bought_any = False
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        change = day_change_pct(sec, cd, use_peak=USE_INTRADAY_PEAK,
                                direction='down')
        if change is None or change > BUY_TRIGGER_PCT:
            continue

        base = g.base_shares.get(sec, 0)
        if base <= 0:
            continue
        plan_buy = int(base * T_RATIO / 100) * 100
        if plan_buy < 100:
            continue

        price = cd[sec].last_price
        low_limit = cd[sec].low_limit
        if low_limit > 0 and price <= low_limit * 1.001:
            log.info('[周内买-跳过] %s 跌停 %.3f' % (conf['name'], price))
            continue

        cash = context.portfolio.available_cash
        cost = plan_buy * price * 1.005
        if cash < cost:
            plan_buy = int(cash * 0.95 / price / 100) * 100
        if plan_buy < 100:
            log.info('[周内买-跳过] %s 现金不足' % conf['name'])
            continue

        order_obj = order(sec, plan_buy)
        if order_obj is None:
            continue
        filled = int(order_obj.filled) if order_obj.filled else 0
        if filled < 100:
            continue
        g.t_shares[sec] = g.t_shares.get(sec, 0) + filled
        bought_any = True
        log.info('[周内买-%s] 跌幅%.2f%% 价%.3f 量%d'
                 % (conf['name'], change * 100, price, filled))

    if bought_any:
        g.week_bought = True


# ----------------------------------------------------------------------
def day_change_pct(sec, cd, use_peak, direction):
    """计算当日涨跌幅。

    use_peak=True 时：
        direction='up'   -> 用日内最高价计算（捕捉早盘冲高）
        direction='down' -> 用日内最低价计算（捕捉日内跌幅极值）
    use_peak=False 时直接用现价。
    """
    df = attribute_history(sec, 1, '1d', ['close'], skip_paused=True,
                           df=True)
    if df is None or len(df) == 0:
        return None
    prev_close = float(df['close'].iloc[-1])
    if prev_close <= 0:
        return None

    if use_peak:
        intraday = attribute_history(sec, 240, '1m', ['high', 'low'],
                                     skip_paused=True, df=True)
        if intraday is None or len(intraday) == 0:
            ref = cd[sec].last_price
        elif direction == 'up':
            ref = float(intraday['high'].max())
        else:
            ref = float(intraday['low'].min())
    else:
        ref = cd[sec].last_price

    if ref <= 0:
        return None
    return (ref - prev_close) / prev_close


# ----------------------------------------------------------------------
def eod_log(context):
    weekday = context.current_dt.weekday()
    weekname = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][weekday]
    log.info('[%s日终] 已卖=%s 已买=%s 总值=%.0f'
             % (weekname, g.week_sold, g.week_bought,
                context.portfolio.total_value))
