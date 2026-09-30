# -*- coding: utf-8 -*-
import datetime

"""
网格半仓策略 v7（每跌3元加100股、每涨1元减100股 + 双重风控）

策略规则
========
1. 首日 09:31 用总资产的 50% 一次性建仓 → 记 g.last_trade_price = 建仓价
2. 网格交易（每分钟检查）：
   - 当前价 <= last_trade_price - 3 元：买入 100 股，更新 last_trade_price
   - 当前价 >= last_trade_price + 1 元：卖出 100 股，更新 last_trade_price
   - 网格触发可叠加：例如一次跌 9 元就连续买 3 次（300 股）
3. 风险控制 1 - 连续 2 日涨幅 > 5%：
   - 卖掉一半持仓
   - 记录"双涨触发前一日的收盘价"作为补仓目标价 g.refill_target_price
   - 进入 'wait_refill' 态：股价跌回 refill_target_price 时用现金买回当时卖的同等股数
4. 风险控制 2 - 连续 2 日涨停：
   - 全部清仓
   - 进入 'cooldown' 态，30 个自然日内不交易
   - 满 30 天后第一个交易日 09:31 直接重启策略（重新按当时总资产 50% 建仓）
5. 网格、风控1、风控2 优先级：风控2 > 风控1 > 网格

状态机
======
g.mode:
  'init'         - 等待首次建仓
  'normal'       - 正常网格交易
  'wait_refill'  - 双涨后等待回调补仓（仍可继续网格）
  'cooldown'     - 双涨停后冷静期
  'restart_pending' - 冷静期满，下个交易日 09:31 重新建仓

参数
====
SECURITY:                标的代码
INIT_POSITION_RATIO:     首次建仓比例（0.5 = 半仓）
GRID_DOWN_STEP:          下跌每步元数（默认3元）
GRID_UP_STEP:            上涨每步元数（默认1元）
GRID_LOT:                每次加减股数（默认100）
SURGE_2D_PCT:            连两日涨幅触发阈值（默认5%）
LIMIT_UP_PCT:            涨停判定阈值（默认9.8%）
COOLDOWN_DAYS:           涨停后冷静天数（默认30）

T+1 处理
========
- 加仓：可任何时点买入
- 减仓/卖半仓/清仓：必须用 closeable_amount，不足时按可卖部分操作
- 卖完后下单"残余股数"，等次日 closeable_amount 释放后通过 g.pending_sell 兜底
"""

# ============ 参数 ============
SECURITY = '000426.XSHE'
INIT_POSITION_RATIO = 0.50
GRID_DOWN_STEP = 3.0
GRID_UP_STEP = 1.0
GRID_LOT = 100
SURGE_2D_PCT = 0.05            # 连两日各 >5%
LIMIT_UP_PCT = 0.098           # 9.8% 视为涨停（避免精度问题）
COOLDOWN_DAYS = 30
RESTART_RATIO = 0.50           # 冷静期满后重启时建仓比例
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
    g.mode = 'init'
    g.last_trade_price = 0.0           # 网格基准价（上次成交价）
    g.refill_target_price = 0.0        # wait_refill 模式下的回补目标价
    g.refill_shares = 0                # wait_refill 待补回股数
    g.cooldown_until = None            # cooldown 截止日（datetime.date）
    g.pending_sell = 0                 # T+1 兜底卖出残余股数

    run_daily(daily_open, time='09:31')
    run_daily(on_bar, time='every_bar')
    run_daily(eod_check, time='after_close')


# ----------------------------------------------------------------------
def daily_open(context):
    """每日开盘：处理 init / cooldown 解除 / pending_sell。"""
    sec = g.security
    cd = get_current_data()
    if cd[sec].paused:
        return

    today = context.current_dt.date()

    # 1. 冷静期判断
    if g.mode == 'cooldown' and g.cooldown_until is not None and today >= g.cooldown_until:
        log.info('[冷静期解除] 进入 restart_pending')
        g.mode = 'restart_pending'

    # 2. 首次建仓 / 重启建仓
    if g.mode == 'init' or g.mode == 'restart_pending':
        ratio = INIT_POSITION_RATIO if g.mode == 'init' else RESTART_RATIO
        target_value = context.portfolio.total_value * ratio
        price = cd[sec].last_price
        target_shares = int(target_value / price / 100) * 100
        if target_shares < 100:
            log.warn('[建仓失败] 资金不足')
            return
        order_obj = order(sec, target_shares)
        if order_obj is None:
            log.warn('[建仓失败] 下单失败')
            return
        filled = int(order_obj.filled) if order_obj.filled else target_shares
        g.last_trade_price = price
        g.mode = 'normal'
        log.info('[建仓] %s %d股 价格%.3f 模式→normal' %
                 ('首日' if ratio == INIT_POSITION_RATIO else '重启', filled, price))
        return

    # 3. T+1 兜底卖出（前一日 sell_half 等无法全卖出的残余）
    if g.pending_sell > 0:
        pos = context.portfolio.positions.get(sec)
        if pos is not None and pos.closeable_amount > 0:
            sell = min(g.pending_sell, pos.closeable_amount)
            sell = int(sell / 100) * 100
            if sell >= 100:
                order(sec, -sell)
                g.pending_sell -= sell
                log.info('[T+1兜底卖] %d股，残余%d' % (sell, g.pending_sell))


# ----------------------------------------------------------------------
def on_bar(context):
    """每分钟主逻辑：风控2 > 风控1 > 网格。"""
    sec = g.security
    now = context.current_dt
    hhmm = now.strftime('%H:%M')

    # 09:30 开盘前不做任何事；14:55 后停手
    if hhmm < '09:35' or hhmm >= '14:55':
        return

    cd = get_current_data()
    if cd[sec].paused:
        return

    # 冷静期不交易
    if g.mode == 'cooldown':
        return

    bar = attribute_history(sec, 1, '1m', ['close'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])

    # ---- 风控 2：连续 2 日涨停（仅每日早盘检查一次，09:35-09:36）----
    if hhmm == '09:35' and check_two_limit_up(sec):
        clear_all_and_cooldown(context, price, cd)
        return

    # ---- 风控 1：连续 2 日涨幅 > 5%（仅每日早盘检查一次）----
    if hhmm == '09:35' and g.mode == 'normal' and check_two_day_surge(sec):
        sell_half_and_wait_refill(context, price, sec, cd)
        return

    # ---- wait_refill 模式：跌回目标价时回补 ----
    if g.mode == 'wait_refill':
        if price <= g.refill_target_price and g.refill_shares > 0:
            do_refill(context, price, sec)
            # 回补后继续走网格逻辑
        # 即使在 wait_refill 也允许网格继续运行

    # ---- 网格逻辑（normal & wait_refill 都执行）----
    if g.mode in ('normal', 'wait_refill'):
        run_grid(context, price, sec)


# ----------------------------------------------------------------------
def run_grid(context, price, sec):
    """网格加减仓。可一次叠加多步。"""
    if g.last_trade_price <= 0:
        return

    # 下跌加仓：每跌 GRID_DOWN_STEP 元加 GRID_LOT 股
    if price <= g.last_trade_price - GRID_DOWN_STEP:
        steps = int((g.last_trade_price - price) / GRID_DOWN_STEP)
        if steps >= 1:
            buy_shares = steps * GRID_LOT
            cost = buy_shares * price * 1.005
            if context.portfolio.available_cash < cost:
                # 资金不足时按可用现金降量
                buy_shares = int(context.portfolio.available_cash * 0.95
                                 / price / GRID_LOT) * GRID_LOT
            if buy_shares >= 100:
                order(sec, buy_shares)
                old = g.last_trade_price
                g.last_trade_price = price
                log.info('[网格-加] 价%.3f 量%d 步数%d 旧基%.3f→新基%.3f' %
                         (price, buy_shares, steps, old, price))
        return

    # 上涨减仓：每涨 GRID_UP_STEP 元减 GRID_LOT 股
    if price >= g.last_trade_price + GRID_UP_STEP:
        steps = int((price - g.last_trade_price) / GRID_UP_STEP)
        if steps >= 1:
            pos = context.portfolio.positions.get(sec)
            if pos is None or pos.closeable_amount <= 0:
                return
            sell_shares = min(steps * GRID_LOT, pos.closeable_amount)
            sell_shares = int(sell_shares / 100) * 100
            if sell_shares >= 100:
                order(sec, -sell_shares)
                old = g.last_trade_price
                g.last_trade_price = price
                log.info('[网格-减] 价%.3f 量%d 步数%d 旧基%.3f→新基%.3f' %
                         (price, sell_shares, steps, old, price))


# ----------------------------------------------------------------------
def sell_half_and_wait_refill(context, price, sec, cd):
    """风控1：卖半仓并记录回补价。"""
    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.total_amount <= 0:
        return

    target_sell = int(pos.total_amount / 2 / 100) * 100
    if target_sell < 100:
        return
    actual_sell = min(target_sell, pos.closeable_amount)
    actual_sell = int(actual_sell / 100) * 100
    if actual_sell < 100:
        log.warn('[风控1-失败] 可卖<100')
        return

    order(sec, -actual_sell)
    deferred = target_sell - actual_sell
    if deferred > 0:
        g.pending_sell += deferred
        log.info('[风控1-延迟] %d股转明日卖' % deferred)

    # 双涨指令"发出前"的起点价 = 2 日前收盘价
    df = attribute_history(sec, 3, '1d', ['close'], skip_paused=True)
    if df is not None and len(df) >= 3:
        # 索引 -1=昨收，-2=前日收，-3=前前日收（即双涨开始前的"起点"）
        target = float(df['close'].iloc[-3])
    else:
        target = price * 0.95     # 兜底：5% 回调

    g.mode = 'wait_refill'
    g.refill_target_price = target
    g.refill_shares = target_sell    # 期望回补的总股数（不含 pending）
    log.warn('[风控1-卖半仓] 已卖%d 延迟%d 回补目标价%.3f' %
             (actual_sell, deferred, target))


def do_refill(context, price, sec):
    """wait_refill 模式下的回补：用现金买回 refill_shares。"""
    buy_shares = g.refill_shares
    cost = buy_shares * price * 1.005
    available = context.portfolio.available_cash
    if available < cost:
        buy_shares = int(available * 0.95 / price / 100) * 100
    if buy_shares < 100:
        log.warn('[回补-失败] 现金不足')
        return

    order(sec, buy_shares)
    log.info('[风控1-回补] 价%.3f 数量%d 目标价%.3f' %
             (price, buy_shares, g.refill_target_price))
    g.mode = 'normal'
    g.refill_target_price = 0.0
    g.refill_shares = 0
    g.last_trade_price = price       # 回补价作为新网格基准


def clear_all_and_cooldown(context, price, cd):
    """风控2：清仓并进入冷静期。"""
    sec = g.security
    pos = context.portfolio.positions.get(sec)
    if pos is None or pos.total_amount <= 0:
        # 已无仓位，直接进冷静期
        enter_cooldown(context)
        return

    # 涨停日不能卖出（卖单会失败）；记录待次日卖
    low_limit = cd[sec].low_limit
    if low_limit > 0 and price <= low_limit * 1.002:
        log.info('[风控2-跌停跳过] 待次日卖')
        g.pending_sell += pos.closeable_amount
    else:
        actual_sell = min(pos.total_amount, pos.closeable_amount)
        actual_sell = int(actual_sell / 100) * 100
        deferred = pos.total_amount - actual_sell
        if actual_sell >= 100:
            order(sec, -actual_sell)
            log.warn('[风控2-清仓] 卖%d 延迟%d' % (actual_sell, deferred))
        if deferred > 0:
            g.pending_sell += deferred

    enter_cooldown(context)


def enter_cooldown(context):
    """进入冷静期。"""
    today = context.current_dt.date()
    g.mode = 'cooldown'
    g.cooldown_until = today + datetime.timedelta(days=COOLDOWN_DAYS)
    g.last_trade_price = 0.0
    g.refill_target_price = 0.0
    g.refill_shares = 0
    log.warn('[冷静期开始] 至 %s（%d天）' % (g.cooldown_until, COOLDOWN_DAYS))


# ----------------------------------------------------------------------
def check_two_day_surge(sec):
    """前两个交易日各自涨幅 > SURGE_2D_PCT。"""
    df = attribute_history(sec, 4, '1d', ['close'], skip_paused=True)
    if df is None or len(df) < 4:
        return False
    # df.iloc[-1] = 昨日，[-2] = 前日，[-3] = 前前日
    yest = float(df['close'].iloc[-1])
    pre = float(df['close'].iloc[-2])
    pre2 = float(df['close'].iloc[-3])
    if pre2 <= 0 or pre <= 0:
        return False
    up_pre = (pre - pre2) / pre2
    up_yest = (yest - pre) / pre
    return up_pre > SURGE_2D_PCT and up_yest > SURGE_2D_PCT


def check_two_limit_up(sec):
    """前两个交易日均接近涨停（>= LIMIT_UP_PCT）。"""
    df = attribute_history(sec, 4, '1d', ['close'], skip_paused=True)
    if df is None or len(df) < 4:
        return False
    yest = float(df['close'].iloc[-1])
    pre = float(df['close'].iloc[-2])
    pre2 = float(df['close'].iloc[-3])
    if pre2 <= 0 or pre <= 0:
        return False
    return (pre - pre2) / pre2 >= LIMIT_UP_PCT and \
           (yest - pre) / pre >= LIMIT_UP_PCT


# ----------------------------------------------------------------------
def eod_check(context):
    """收盘汇报。"""
    sec = g.security
    pos = context.portfolio.positions.get(sec)
    held = pos.total_amount if pos is not None else 0
    cash = context.portfolio.available_cash
    log.info('[收盘] 模式=%s 持仓=%d 现金=%.0f 基准价=%.3f' %
             (g.mode, held, cash, g.last_trade_price))


# 工具：datetime 模块（聚宽环境内置）
import datetime
