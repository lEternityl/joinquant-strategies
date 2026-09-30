# -*- coding: utf-8 -*-
"""
防御循环阻断策略 v1
=====================
基于历史交易规律提取的"反人性"防御型策略：

历史教训
--------
1. 胜率仅 41%，盈亏比 1.07 → 必须主动提高胜率
2. 周四/周五胜率仅 20%，是亏损重灾区
3. 周二胜率 67%，是最佳操作日
4. 月底（21日后）几乎全亏
5. 亏损有传染性：亏损后次日续亏概率 65%（15/23）
6. 大盈后必吐回：千元盈利日之后全是亏损
7. 摇摆日小赚 → 大亏（如 5/13 +236 → 5/14 -825）
8. 月底密集交易 → 连续小亏 → 单日大亏（致命循环）

策略核心：六道闸门
==================
闸门1：星期闸门
  - 周一/周二/周三 允许开仓（周二优先）
  - 周四/周五 禁止开仓，只允许止损/止盈/平仓
  - 周二胜率最高 → 阈值更宽松（FAVORABLE_DAY_BONUS）

闸门2：月底闸门
  - 每月 1~10 日（上旬）正常操作
  - 每月 11~20 日（中旬）仓位 70%
  - 每月 21 日及以后（下旬）禁止开仓 + 强制减仓 50%

闸门3：连亏冷静闸门
  - 连续亏损 2 日 → 第 3 日强制冷静（不开仓）
  - 连续亏损 3 日 → 强制冷静 2 日 + 减仓
  - 单日大亏（< -1%）当日清空 T 仓 + 次日冷静

闸门4：盈利锁定闸门
  - 当日盈利 > 1.5% 强制清 T 仓（防吐回）
  - 大盈日（> 0.8%）次日开仓上限减半

闸门5：摇摆日闸门
  - 前一日盈利 → 当日开仓上限减半
  - 防止"今天赚明天亏"的小赚大亏循环

闸门6：周内累亏闸门
  - 当周累计亏损 > 1% 总资产 → 冻结本周剩余交易
  - 防止"密集交易 → 大幅回撤"的恶性循环

操作动作
--------
- 每周二：第一选择交易日（阈值放宽 30%）
- 每周一/三：备选交易日（标准阈值）
- 周四/周五：仅允许风控平仓
- 月底+连亏+周累亏 任一触发即停手

适用场景
--------
- 已有底仓的有色周期股（北方稀土/兴业银锡/紫金矿业）
- 目标：把胜率从 41% 拉到 55% 以上
"""

# ============ 标的与初始持仓 ============
SECURITIES = {
    '600111.XSHG': {'name': '北方稀土', 'init_shares': 100},
    '000426.XSHE': {'name': '兴业银锡', 'init_shares': 100},
    '601899.XSHG': {'name': '紫金矿业', 'init_shares': 400},
}

MARKET_INDEX = '000300.XSHG'

# ============ 闸门1：星期参数 ============
ALLOWED_OPEN_WEEKDAYS = (0, 1, 2)   # 周一/二/三
FAVORABLE_DAY = 1                   # 周二（胜率 67%）
FAVORABLE_DAY_BONUS = 0.7           # 周二阈值乘以 0.7（更易触发）

# ============ 闸门2：月内参数 ============
MID_MONTH_START = 11                # 中旬起始
LATE_MONTH_START = 21               # 下旬起始（强制减仓）
MID_MONTH_RATIO = 0.7               # 中旬开仓上限折扣
LATE_MONTH_REDUCE = 0.5             # 下旬强制减到 50% 仓位

# ============ 闸门3：连亏冷静参数 ============
COOL_AFTER_2_LOSS = 1               # 连亏 2 → 冷静 1 日
COOL_AFTER_3_LOSS = 2               # 连亏 3 → 冷静 2 日
BIG_LOSS_PCT = -0.010               # 单日 < -1% 视为大亏
COOL_AFTER_BIG_LOSS = 1             # 大亏后冷静 1 日

# ============ 闸门4：盈利锁定参数 ============
PROFIT_LOCK_PCT = 0.015             # 当日 +1.5% 强制清 T
BIG_PROFIT_PCT = 0.008              # 当日 +0.8% 视为大盈
NEXT_DAY_HALF_RATIO = 0.5           # 大盈次日仓位减半

# ============ 闸门5：摇摆日参数 ============
SWING_DAY_HALF_RATIO = 0.5          # 前日盈利 → 当日仓位减半

# ============ 闸门6：周累亏参数 ============
WEEK_LOSS_LIMIT = -0.010            # 当周累亏 > -1% 冻结

# ============ 交易触发参数 ============
SELL_TRIGGER_PCT = 0.020            # 卖：当日涨 2%
BUY_TRIGGER_PCT = -0.018            # 买：当日跌 1.8%
T_RATIO = 0.20                      # 单次 T 仓占该只底仓比例

# ============ 风控参数 ============
MARKET_BAN_PCT = -0.020
MIN_BASE_SHARES_RATIO = 0.50

# ============ 时间窗 ============
CHECK_TIME = '14:50'
END_OF_DAY = '14:55'


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
    g.base_shares = {}

    # 闸门状态
    g.day_start_value = None
    g.consec_loss_days = 0          # 连续亏损天数
    g.cool_remaining = 0            # 冷静期剩余天数
    g.last_day_profit = False       # 前一日是否盈利（摇摆日识别）
    g.last_day_big_profit = False   # 前一日是否大盈
    g.week_anchor = None
    g.week_pnl = 0.0                # 本周累计盈亏

    g.late_month_reduced = False    # 本月下旬是否已强制减仓

    run_daily(weekly_reset, time='before_open')
    run_daily(monthly_reduce_check, time='09:30')
    run_daily(build_base, time='09:31')
    run_daily(daily_open_value, time='09:30')
    run_daily(intraday_loss_guard, time='14:30')
    run_daily(daily_decision, time=CHECK_TIME)
    run_daily(eod_summary, time='after_close')


# ======================================================================
def weekly_reset(context):
    today = context.current_dt.date()
    weekday = today.weekday()
    monday = today - _td(weekday)
    if g.week_anchor != monday:
        g.week_anchor = monday
        g.week_pnl = 0.0
        g.late_month_reduced = False
        log.info('[周重置] %s' % monday)


def _td(days):
    import datetime as _dt
    return _dt.timedelta(days=days)


def daily_open_value(context):
    g.day_start_value = context.portfolio.total_value


# ======================================================================
def monthly_reduce_check(context):
    """闸门2：每月下旬强制减仓 50% 一次。"""
    today = context.current_dt
    if today.day < LATE_MONTH_START:
        return
    if g.late_month_reduced:
        return
    cd = get_current_data()
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        pos = context.portfolio.positions.get(sec)
        if pos is None or pos.closeable_amount <= 0:
            continue
        base = g.base_shares.get(sec, pos.total_amount)
        # 只对超过底仓部分（即 T 仓）减仓
        extra = pos.total_amount - int(base * LATE_MONTH_REDUCE)
        if extra < 100:
            continue
        sell = int(extra / 100) * 100
        max_sell = pos.closeable_amount - int(base * MIN_BASE_SHARES_RATIO)
        sell = min(sell, max_sell)
        sell = int(sell / 100) * 100
        if sell < 100:
            continue
        if order(sec, -sell) is not None:
            log.info('[月底减仓-%s] %d股' % (conf['name'], sell))
    g.late_month_reduced = True


# ======================================================================
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
                log.info('[底仓-%s] %d股' % (conf['name'], target))
        g.base_shares[sec] = target
    g.base_built = True


# ======================================================================
def intraday_loss_guard(context):
    """闸门3：单日大亏当日清 T 仓。"""
    if g.day_start_value is None or g.day_start_value <= 0:
        return
    cur = context.portfolio.total_value
    pct = (cur - g.day_start_value) / g.day_start_value
    if pct > BIG_LOSS_PCT:
        return
    log.warn('[盘中大亏-清T] 当日%.2f%%' % (pct * 100))
    cd = get_current_data()
    # 把超过底仓的部分全部清掉
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        pos = context.portfolio.positions.get(sec)
        if pos is None:
            continue
        base = g.base_shares.get(sec, pos.total_amount)
        extra = pos.total_amount - base
        if extra < 100:
            continue
        sell = min(extra, pos.closeable_amount)
        sell = int(sell / 100) * 100
        if sell < 100:
            continue
        if order(sec, -sell) is not None:
            log.info('[盘中清T-%s] %d股' % (conf['name'], sell))


# ======================================================================
def daily_decision(context):
    """每日 14:50 决策入口，依次通过六道闸门。"""
    today = context.current_dt
    weekday = today.weekday()

    # 计算实时仓位调整系数
    multiplier = 1.0

    # 闸门1：星期闸门
    if weekday not in ALLOWED_OPEN_WEEKDAYS:
        # 周四/周五：仅做盈利锁定，不开新仓
        try_profit_lock(context)
        return
    if weekday == FAVORABLE_DAY:
        threshold_factor = FAVORABLE_DAY_BONUS
    else:
        threshold_factor = 1.0

    # 闸门2：月内闸门
    day_of_month = today.day
    if day_of_month >= LATE_MONTH_START:
        log.info('[闸门2-下旬] 禁止开仓 day=%d' % day_of_month)
        return
    if day_of_month >= MID_MONTH_START:
        multiplier *= MID_MONTH_RATIO

    # 闸门3：冷静期
    if g.cool_remaining > 0:
        log.info('[闸门3-冷静] 剩余%d日' % g.cool_remaining)
        return

    # 闸门4：盈利锁定（先做）
    profit_locked = try_profit_lock(context)
    if profit_locked:
        # 已经清了 T 仓，今日不再开
        return

    # 闸门5：摇摆日
    if g.last_day_profit:
        multiplier *= SWING_DAY_HALF_RATIO
    if g.last_day_big_profit:
        multiplier *= NEXT_DAY_HALF_RATIO

    # 闸门6：周累亏
    if g.day_start_value and g.week_pnl / g.day_start_value <= WEEK_LOSS_LIMIT:
        log.warn('[闸门6-周累亏] 冻结本周')
        return

    # 通过所有闸门 -> 执行交易判定
    sell_pct = SELL_TRIGGER_PCT * threshold_factor
    buy_pct = BUY_TRIGGER_PCT * threshold_factor
    final_ratio = T_RATIO * multiplier
    if final_ratio < 0.05:
        log.info('[仓位过小] %.2f 跳过' % final_ratio)
        return

    log.info('[闸门通过] 周%d 月%d日 阈卖=%.3f 阈买=%.3f 量比=%.2f'
             % (weekday, day_of_month, sell_pct, buy_pct, final_ratio))

    # 优先卖（涨幅大）；若无再判断买
    if not _try_sell(context, sell_pct, final_ratio):
        _try_buy(context, buy_pct, final_ratio)


# ======================================================================
def try_profit_lock(context):
    """闸门4：当日盈利 > 1.5% 强制清 T 仓，防止吐回。"""
    if g.day_start_value is None or g.day_start_value <= 0:
        return False
    cur = context.portfolio.total_value
    pct = (cur - g.day_start_value) / g.day_start_value
    if pct < PROFIT_LOCK_PCT:
        return False
    cd = get_current_data()
    locked = False
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        pos = context.portfolio.positions.get(sec)
        if pos is None:
            continue
        base = g.base_shares.get(sec, pos.total_amount)
        extra = pos.total_amount - base
        if extra < 100:
            continue
        sell = min(extra, pos.closeable_amount)
        sell = int(sell / 100) * 100
        if sell < 100:
            continue
        if order(sec, -sell) is not None:
            log.info('[盈利锁定-%s] +%.2f%% 卖%d股'
                     % (conf['name'], pct * 100, sell))
            locked = True
    return locked


# ======================================================================
def _try_sell(context, sell_pct, ratio):
    cd = get_current_data()
    sold = False
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        change = day_change_pct(sec, cd, direction='up')
        if change is None or change < sell_pct:
            continue

        pos = context.portfolio.positions.get(sec)
        if pos is None or pos.closeable_amount <= 0:
            continue
        base = g.base_shares.get(sec, pos.total_amount)
        plan = int(base * ratio / 100) * 100
        keep = int(base * MIN_BASE_SHARES_RATIO)
        max_sell = pos.closeable_amount - keep
        sell = min(plan, max_sell)
        sell = int(sell / 100) * 100
        if sell < 100:
            continue
        price = cd[sec].last_price
        high_limit = cd[sec].high_limit
        if high_limit > 0 and price >= high_limit * 0.999:
            continue
        if order(sec, -sell) is not None:
            log.info('[周内卖-%s] +%.2f%% 价%.3f 量%d'
                     % (conf['name'], change * 100, price, sell))
            sold = True
    return sold


def _try_buy(context, buy_pct, ratio):
    cd = get_current_data()
    mkt = day_change_pct(MARKET_INDEX, cd, direction='down')
    if mkt is not None and mkt <= MARKET_BAN_PCT:
        log.warn('[大盘禁买] %.2f%%' % (mkt * 100))
        return False
    bought = False
    for sec, conf in SECURITIES.items():
        if cd[sec].paused:
            continue
        change = day_change_pct(sec, cd, direction='down')
        if change is None or change > buy_pct:
            continue
        base = g.base_shares.get(sec, 0)
        if base <= 0:
            continue
        plan = int(base * ratio / 100) * 100
        if plan < 100:
            continue
        price = cd[sec].last_price
        low_limit = cd[sec].low_limit
        if low_limit > 0 and price <= low_limit * 1.001:
            continue
        cost = plan * price * 1.005
        cash = context.portfolio.available_cash
        if cash < cost:
            plan = int(cash * 0.95 / price / 100) * 100
        if plan < 100:
            continue
        if order(sec, plan) is not None:
            log.info('[周内买-%s] %.2f%% 价%.3f 量%d'
                     % (conf['name'], change * 100, price, plan))
            bought = True
    return bought


# ======================================================================
def day_change_pct(sec, cd, direction):
    df = attribute_history(sec, 1, '1d', ['close'], skip_paused=True, df=True)
    if df is None or len(df) == 0:
        return None
    prev_close = float(df['close'].iloc[-1])
    if prev_close <= 0:
        return None
    intraday = attribute_history(sec, 240, '1m', ['high', 'low'],
                                 skip_paused=True, df=True)
    if intraday is None or len(intraday) == 0:
        ref = cd[sec].last_price
    elif direction == 'up':
        ref = float(intraday['high'].max())
    else:
        ref = float(intraday['low'].min())
    if ref <= 0:
        return None
    return (ref - prev_close) / prev_close


# ======================================================================
def eod_summary(context):
    """日终结算与状态更新。"""
    if g.day_start_value is None or g.day_start_value <= 0:
        return
    cur = context.portfolio.total_value
    pct = (cur - g.day_start_value) / g.day_start_value
    pnl = cur - g.day_start_value
    g.week_pnl += pnl

    weekday = context.current_dt.weekday()
    weekname = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][weekday]

    # 更新连亏计数器
    if pnl < 0:
        g.consec_loss_days += 1
    else:
        g.consec_loss_days = 0

    # 计算冷静期
    if g.cool_remaining > 0:
        g.cool_remaining -= 1
    if pct <= BIG_LOSS_PCT:
        g.cool_remaining = max(g.cool_remaining, COOL_AFTER_BIG_LOSS)
    if g.consec_loss_days >= 3:
        g.cool_remaining = max(g.cool_remaining, COOL_AFTER_3_LOSS)
    elif g.consec_loss_days >= 2:
        g.cool_remaining = max(g.cool_remaining, COOL_AFTER_2_LOSS)

    # 更新摇摆日标记
    g.last_day_profit = pnl > 0
    g.last_day_big_profit = pct >= BIG_PROFIT_PCT

    log.info('[%s日终] 当日%.2f%%(%.0f) 周累%.0f 连亏%d 冷静%d'
             % (weekname, pct * 100, pnl, g.week_pnl,
                g.consec_loss_days, g.cool_remaining))
