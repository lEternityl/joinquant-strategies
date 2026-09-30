# -*- coding: utf-8 -*-
"""
主升浪趋势确认 + 浮动止盈止损策略（v3）
==========================================
策略思路：
1. 大盘环境过滤 — benchmark 在 MA60 之上才允许买入（顺势而为）
2. 主升浪确认后买入，等权分配（最多5只）
3. 浮动止盈止损（三层保护）：
   - 第一层：盈利 0~15%   → 止损线 -20%（硬止损）
   - 第二层：盈利 15~25%  → 止损线上移到 -10%（保护利润）
   - 第三层：盈利 25%+    → 启动移动止损，从最高点回撤 10% 卖出
4. 趋势破位离场：均线多头排列被破坏时也卖出

买入条件（七项全部满足）：
  A. 大盘在 MA60 之上
  B. 均线多头排列 MA5 > MA10 > MA20 > MA60
  C. 收盘价在 MA5 之上
  D. 乖离率 ≤ 12%（收紧，原 15% 还是太容易追高）
  E. 10日涨幅 5%~25%（上限收紧，超过 25% 不追）
  F. 回调≥5% 后重新突破（洗盘更明显才确认）
  G. 放量 + 趋势持续验证

使用方法：直接粘贴到聚宽在线编辑器运行。
"""

from jqdata import *

# =============================================
# 可调参数
# =============================================
UNIVERSE = '000300.XSHG'      # 股票池：000300=沪深300 / 000905=中证500
MAX_POSITIONS = 5             # 最大持仓数量

# --- 买入条件 ---
VOLUME_RATIO = 1.5            # 放量倍数：5日均量 / 60日均量
DEVIATION_MAX = 0.12          # 乖离率上限（收紧到12%，降低追高风险）
MIN_RETURN_10D = 0.05         # 10日最低涨幅 5%
MAX_RETURN_10D = 0.25         # 10日最高涨幅 25%（收紧，超涨不追）
PULLBACK_MIN = 0.05           # 最小回调幅度 5%（收紧，避免把噪声当洗盘）

# --- 浮动止盈止损（三层保护） ---
STOP_LOSS_HARD = -0.20        # 硬止损线（盈利不足15%时使用）
STOP_LOSS_MID = -0.10         # 中间止损线（盈利15%~25%时使用）
PROFIT_TIER1 = 0.15           # 进入中间止损的盈利阈值
PROFIT_TIER2 = 0.25           # 进入移动止损的盈利阈值
TRAILING_STOP = 0.10          # 移动止损：从最高点回撤10%卖出

# --- 大盘过滤 ---
USE_MARKET_FILTER = True      # 是否启用大盘环境过滤
BENCHMARK_MA = 60             # 大盘均线周期（benchmark 低于此均线不买）


def initialize(context):
    set_benchmark(UNIVERSE)
    set_option('use_real_price', True)

    set_order_cost(OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5,
    ), type='stock')
    set_slippage(PriceRelatedSlippage(0.00246))

    # --- 保存参数到 g ---
    g.universe = UNIVERSE
    g.max_positions = MAX_POSITIONS
    g.volume_ratio = VOLUME_RATIO
    g.deviation_max = DEVIATION_MAX
    g.min_return_10d = MIN_RETURN_10D
    g.max_return_10d = MAX_RETURN_10D
    g.pullback_min = PULLBACK_MIN

    g.stop_loss_hard = STOP_LOSS_HARD
    g.stop_loss_mid = STOP_LOSS_MID
    g.profit_tier1 = PROFIT_TIER1
    g.profit_tier2 = PROFIT_TIER2
    g.trailing_stop = TRAILING_STOP

    g.use_market_filter = USE_MARKET_FILTER
    g.benchmark_ma = BENCHMARK_MA

    # --- 记录每只持仓的历史最高价（用于移动止损） ---
    g.high_water = {}

    run_daily(prepare_stock_list, time='before_open')
    run_daily(trade, time='09:31')


def prepare_stock_list(context):
    """盘前准备候选股票池"""
    date = context.current_dt.strftime('%Y-%m-%d')
    g.all_stocks = get_index_stocks(g.universe, date)


def trade(context):
    current_data = get_current_data()

    # 第一步：更新所有持仓的历史最高价
    update_high_water(context, current_data)

    # 第二步：浮动止盈止损检查
    manage_positions(context, current_data)

    # 第三步：大盘环境过滤 + 寻找主升浪买入
    if is_market_uptrend(context):
        buy_main_uptrend_stocks(context, current_data)
    else:
        log.info('大盘环境不佳（benchmark 在 MA%d 之下），暂停买入' % g.benchmark_ma)


# ============================================================
# 卖出逻辑：三层浮动止盈止损 + 趋势破位
# ============================================================

def update_high_water(context, current_data):
    """更新每只持仓的历史最高价"""
    for stock, pos in context.portfolio.positions.items():
        if pos.total_amount == 0:
            continue
        current_price = current_data[stock].last_price
        if stock not in g.high_water or current_price > g.high_water[stock]:
            g.high_water[stock] = current_price

    # 清理已清仓的记录
    for stock in list(g.high_water.keys()):
        if stock not in context.portfolio.positions or \
           context.portfolio.positions[stock].total_amount == 0:
            del g.high_water[stock]


def manage_positions(context, current_data):
    """
    三层浮动保护 + 趋势破位检查。

    盈利区间            止损方式
    ─────────────────────────────────────
    亏损 ~ +15%        硬止损 -20%
    +15% ~ +25%        止损收窄到 -10%
    +25% 以上           移动止损（最高价回撤 10%）

    外加：均线多头排列被破坏 → 离场。
    真正的"按兵不动"是针对赢家——如果主升浪持续，
    MA5 永远不会跌破 MA10，趋势破位永远不会触发。
    但对于买入后趋势就没走出来的股票，快刀斩乱麻。
    """
    for stock in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[stock]
        if pos.total_amount == 0 or pos.closeable_amount == 0:
            continue

        current_price = current_data[stock].last_price
        cost = pos.avg_cost
        if cost <= 0:
            continue

        pnl = (current_price - cost) / cost
        high_water = g.high_water.get(stock, current_price)

        should_sell = False
        reason = ''

        # --- 第三层：移动止损（盈利 25%+） ---
        if pnl >= g.profit_tier2 and high_water > 0:
            drawdown = (current_price - high_water) / high_water
            if drawdown <= -g.trailing_stop:
                should_sell = True
                reason = '移动止损（从高点 %.2f 回撤 %.1f%%）' % (high_water, -drawdown * 100)

        # --- 第二层：止损收窄到 -10%（盈利 15%~25%） ---
        elif pnl >= g.profit_tier1:
            if pnl <= g.stop_loss_mid:
                should_sell = True
                reason = '浮动止损（盈利回吐至 %.1f%%）' % (pnl * 100)

        # --- 第一层：硬止损 -20%（盈利不足 15%） ---
        elif pnl <= g.stop_loss_hard:
            should_sell = True
            reason = '硬止损（亏损 %.1f%%）' % (-pnl * 100)

        # --- 趋势破位：均线多头排列被破坏 ---
        if not should_sell and not is_ma_bullish(stock):
            should_sell = True
            reason = '趋势破位（均线多头排列被破坏）'

        if should_sell:
            order_target(stock, 0)
            log.warn('【卖出】%s —— %s，成本 %.2f，现价 %.2f，盈亏 %.1f%%' % (
                stock, reason, cost, current_price, pnl * 100))
            if stock in g.high_water:
                del g.high_water[stock]


def is_ma_bullish(stock):
    """检查均线是否仍然多头排列（MA5 > MA10 > MA20 > MA60）"""
    df = attribute_history(stock, 70, '1d', ['close'], skip_paused=True, df=True)
    if len(df) < 60:
        return False

    close = df['close']
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]

    return ma5 > ma10 > ma20 > ma60


# ============================================================
# 买入逻辑
# ============================================================

def is_market_uptrend(context):
    """大盘环境判断：benchmark 指数在 MA60 之上"""
    if not g.use_market_filter:
        return True

    df = attribute_history(g.universe, 80, '1d', ['close'], skip_paused=True, df=True)
    if len(df) < g.benchmark_ma:
        return True  # 数据不够时不拦截

    ma = df['close'].rolling(g.benchmark_ma).mean().iloc[-1]
    return df['close'].iloc[-1] > ma


def buy_main_uptrend_stocks(context, current_data):
    """寻找主升浪股票并买入"""
    stocks = g.all_stocks if hasattr(g, 'all_stocks') else get_index_stocks(g.universe)

    current_count = len([
        s for s in context.portfolio.positions
        if context.portfolio.positions[s].total_amount > 0
    ])

    for stock in stocks:
        if current_count >= g.max_positions:
            break
        if stock in context.portfolio.positions and \
           context.portfolio.positions[stock].total_amount > 0:
            continue

        if is_main_uptrend(stock, current_data):
            cash = context.portfolio.available_cash / (g.max_positions - current_count)
            cash = min(cash, context.portfolio.total_value * 0.25)

            if cash < 10000:
                continue

            order_value(stock, cash)
            g.high_water[stock] = current_data[stock].last_price
            log.info('【主升浪确认】买入 %s，金额 %.0f，价格 %.2f' % (
                stock, cash, current_data[stock].last_price))
            current_count += 1


def is_main_uptrend(stock, current_data):
    """
    主升浪判断：七项条件全部满足才算确认。

    v3 改进：
    - 回调幅度从 3% 提高到 5%，过滤假洗盘
    - 乖离率从 15% 收紧到 12%
    - 涨幅上限从 30% 收紧到 25%
    - 要求回调期间量缩（洗盘特征），突破时量增
    """
    # --- 基础过滤 ---
    d = current_data[stock]
    if d.paused:
        return False
    if d.is_st or 'ST' in d.name or '*' in d.name:
        return False
    if d.last_price >= d.high_limit * 0.995:
        return False
    if d.last_price <= d.low_limit * 1.005:
        return False

    # --- 取数据 ---
    df = attribute_history(stock, 90, '1d',
                           ['close', 'high', 'low', 'volume'],
                           skip_paused=True, df=True)
    if len(df) < 60:
        return False

    close = df['close']
    high = df['high']
    volume = df['volume']

    # 均线
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    # -------------------------------------------------------
    # 条件 A：均线多头排列 MA5 > MA10 > MA20 > MA60
    # -------------------------------------------------------
    v5, v10, v20, v60 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    if not (v5 > v10 > v20 > v60):
        return False

    # -------------------------------------------------------
    # 条件 B：收盘价站在 MA5 之上
    # -------------------------------------------------------
    if close.iloc[-1] <= v5:
        return False

    # -------------------------------------------------------
    # 条件 C：乖离率 ≤ 12%（收紧，避免追高）
    # -------------------------------------------------------
    deviation = (close.iloc[-1] - v20) / v20
    if deviation > g.deviation_max:
        return False

    # -------------------------------------------------------
    # 条件 D：10日涨幅 5%~25%
    # -------------------------------------------------------
    ret_10d = close.iloc[-1] / close.iloc[-10] - 1
    if ret_10d < g.min_return_10d or ret_10d > g.max_return_10d:
        return False

    # -------------------------------------------------------
    # 条件 E：回调≥5% 后重新突破（收紧，过滤假洗盘）
    # 要求：
    #   - 20日高点 > 当前价（说明有过回调）
    #   - 20日内最大回撤 ≥ 5%
    #   - 回调阶段的成交量 < 突破阶段的成交量（洗盘量缩）
    # -------------------------------------------------------
    high_20d = high.iloc[-20:].max()
    min_close_20d = close.iloc[-20:].min()
    max_drawdown_20d = (high_20d - min_close_20d) / high_20d

    if max_drawdown_20d < g.pullback_min:
        # 20 天内没有过像样的回调
        return False

    # 找到回调低点的位置
    min_idx = close.iloc[-20:].values.argmin()
    # 回调期的平均成交量 vs 最近 3 天的平均成交量
    vol_pullback = volume.iloc[-20 + min_idx:min(-5, -3 + min_idx) + 1].mean() \
        if -20 + min_idx < -3 else volume.iloc[-20:-3].mean()
    vol_breakout = volume.iloc[-3:].mean()
    # 突破时量应该大于回调时量（放量突破）
    if vol_pullback > 0 and vol_breakout < vol_pullback * 1.2:
        return False

    # -------------------------------------------------------
    # 条件 F：趋势持续性 — 近5天至少4天收盘在 MA5 上方
    # -------------------------------------------------------
    above_ma5 = sum(1 for i in range(-5, 0) if close.iloc[i] > ma5.iloc[i])
    if above_ma5 < 4:
        return False

    # -------------------------------------------------------
    # 条件 G：放量 — 5日均量 > 60日均量 × 1.5
    # -------------------------------------------------------
    avg_vol_5 = volume.iloc[-5:].mean()
    avg_vol_60 = volume.iloc[-60:].mean()
    if avg_vol_60 == 0 or avg_vol_5 / avg_vol_60 < g.volume_ratio:
        return False

    return True
