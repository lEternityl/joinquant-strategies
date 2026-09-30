# -*- coding: utf-8 -*-
"""
主升浪趋势确认选股 + 日内做T增强策略
=======================================

融合思路：
  【策略A — 主升浪趋势确认_止损策略】负责中线选股和仓位管理：
    1. 大盘环境过滤 — benchmark 在 MA60 之上才允许买入
    2. 主升浪确认后买入（七项条件），等权分配（最多5只）
    3. 三层浮动止盈止损 + 趋势破位离场

  【策略B — strategy_intraday_t0_grid】负责在A的持仓上做日内T+0：
    1. 对每一只持仓股票独立累积 VWAP
    2. 股价低于 VWAP ≥ 3 格 + 5分钟MACD金叉 → 买入T仓
    3. 股价回升到 VWAP 上方或涨 1 格 → 卖出底仓等量平T
    4. 1.5% 硬止损，14:55 强制清算

架构：
  - before_open  → 重置各只股票的T状态 / 准备候选股票池
  - 09:31        → 策略A主逻辑：卖出检查 + 选股买入
  - every_bar    → 对每只持仓股票独立做T（策略B）
  - 14:55        → 尾盘清算所有做T仓位

使用方法：
  - 回测频率设为【分钟级】
  - 直接粘贴到聚宽在线编辑器运行
"""

from jqdata import *

# =============================================
# 策略A参数 —— 主升浪选股
# =============================================
UNIVERSE = '000300.XSHG'      # 股票池：000300=沪深300 / 000905=中证500
MAX_POSITIONS = 5             # 最大持仓数量

# --- 买入条件 ---
VOLUME_RATIO = 1.5            # 放量倍数：5日均量 / 60日均量
DEVIATION_MAX = 0.12          # 乖离率上限（收紧到12%，降低追高风险）
MIN_RETURN_10D = 0.05         # 10日最低涨幅 5%
MAX_RETURN_10D = 0.25         # 10日最高涨幅 25%（收紧，超涨不追）
PULLBACK_MIN = 0.05           # 最小回调幅度 5%

# --- 三层浮动止盈止损 ---
STOP_LOSS_HARD = -0.20        # 硬止损线（盈利不足15%时使用）
STOP_LOSS_MID = -0.10         # 中间止损线（盈利15%~25%时使用）
PROFIT_TIER1 = 0.15           # 进入中间止损的盈利阈值
PROFIT_TIER2 = 0.25           # 进入移动止损的盈利阈值
TRAILING_STOP = 0.10          # 移动止损：从最高点回撤10%卖出

# --- 大盘过滤 ---
USE_MARKET_FILTER = True      # 是否启用大盘环境过滤
BENCHMARK_MA = 60             # 大盘均线周期

# =============================================
# 策略B参数 —— 日内做T
# =============================================
T_POSITION_RATIO = 0.20       # 单次做T仓位占该股底仓的比例
MAX_T_PER_DAY = 1             # 每日每只股票做T次数上限
T_MIN_CASH = 0.10             # 做T最低保留现金（占总资产），防止现金全被T吃光

GRID_STEP = 0.005             # 网格单位：0.5%
GRID_TRIGGER_CNT = 3          # 低于 VWAP ≥ 3 格触发（即 1.5%）
STOP_LOSS_PCT = 0.015         # 硬止损：浮亏 1.5% 强平T仓

TRADE_START = '0950'          # 避开开盘前 20 分钟
TRADE_END = '1450'            # 避开收盘前 10 分钟
FORCE_CLOSE_TIME = '1455'     # 尾盘强制清算

# MACD 参数（5 分钟）
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# =============================================


def initialize(context):
    set_benchmark(UNIVERSE)
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    set_order_cost(OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5,
    ), type='stock')
    set_slippage(PriceRelatedSlippage(0.00246))

    # --- 策略A的参数 ---
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

    # --- 策略B的参数 ---
    g.t_position_ratio = T_POSITION_RATIO
    g.max_t_per_day = MAX_T_PER_DAY
    g.t_min_cash = T_MIN_CASH
    g.grid_step = GRID_STEP
    g.grid_trigger_cnt = GRID_TRIGGER_CNT
    g.stop_loss_pct = STOP_LOSS_PCT
    g.trade_start = TRADE_START
    g.trade_end = TRADE_END
    g.force_close_time = FORCE_CLOSE_TIME
    g.macd_fast = MACD_FAST
    g.macd_slow = MACD_SLOW
    g.macd_signal = MACD_SIGNAL

    # --- 多标的做T状态：{stock: {'state', 'entry_price', 'amount', 'count', 'vwap_amt', 'vwap_vol'}} ---
    g.t_states = {}

    # --- 移动止损的最高价记录 ---
    g.high_water = {}

    run_daily(before_market, time='before_open')
    run_daily(trade_main, time='09:31')
    run_daily(intraday_t0, time='every_bar')
    run_daily(force_close_all_t, time='14:55')


# ============================================================
# 每日重置
# ============================================================

def before_market(context):
    """盘前：重置做T状态 + 准备候选股票池"""
    # --- 重置所有股票的T状态 ---
    g.t_states = {}

    # --- 准备候选股票池（策略A用） ---
    date = context.current_dt.strftime('%Y-%m-%d')
    g.all_stocks = get_index_stocks(g.universe, date)


# ============================================================
# 策略A —— 中线选股与仓位管理（09:31 执行）
# ============================================================

def trade_main(context):
    """策略A主逻辑：卖出检查 + 选股买入"""
    current_data = get_current_data()

    # 第一步：更新移动止损的最高价
    update_high_water(context, current_data)

    # 第二步：浮动止盈止损 + 趋势破位检查
    manage_positions(context, current_data)

    # 第三步：大盘环境过滤 + 寻找主升浪买入
    if is_market_uptrend(context):
        buy_main_uptrend_stocks(context, current_data)
    else:
        log.info('大盘环境不佳（benchmark 在 MA%d 之下），暂停买入' % g.benchmark_ma)


def update_high_water(context, current_data):
    """更新每只持仓的历史最高价（策略A用）"""
    for stock, pos in context.portfolio.positions.items():
        if pos.total_amount == 0:
            continue
        current_price = current_data[stock].last_price
        if stock not in g.high_water or current_price > g.high_water[stock]:
            g.high_water[stock] = current_price

    for stock in list(g.high_water.keys()):
        if stock not in context.portfolio.positions or \
           context.portfolio.positions[stock].total_amount == 0:
            del g.high_water[stock]


def manage_positions(context, current_data):
    """三层浮动保护 + 趋势破位检查（策略A）"""
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

        # 第三层：移动止损（盈利 25%+）
        if pnl >= g.profit_tier2 and high_water > 0:
            drawdown = (current_price - high_water) / high_water
            if drawdown <= -g.trailing_stop:
                should_sell = True
                reason = '移动止损（从高点 %.2f 回撤 %.1f%%）' % (high_water, -drawdown * 100)

        # 第二层：止损收窄到 -10%（盈利 15%~25%）
        elif pnl >= g.profit_tier1:
            if pnl <= g.stop_loss_mid:
                should_sell = True
                reason = '浮动止损（盈利回吐至 %.1f%%）' % (pnl * 100)

        # 第一层：硬止损 -20%（盈利不足 15%）
        elif pnl <= g.stop_loss_hard:
            should_sell = True
            reason = '硬止损（亏损 %.1f%%）' % (-pnl * 100)

        # 趋势破位：均线多头排列被破坏
        if not should_sell and not is_ma_bullish(stock):
            should_sell = True
            reason = '趋势破位（均线多头排列被破坏）'

        if should_sell:
            order_target(stock, 0)
            log.warn('【卖出】%s —— %s，成本 %.2f，现价 %.2f，盈亏 %.1f%%' % (
                stock, reason, cost, current_price, pnl * 100))
            if stock in g.high_water:
                del g.high_water[stock]
            # 清理该股票的做T状态
            if stock in g.t_states:
                del g.t_states[stock]


def is_ma_bullish(stock):
    """均线多头排列检查（策略A）"""
    df = attribute_history(stock, 70, '1d', ['close'], skip_paused=True, df=True)
    if len(df) < 60:
        return False
    close = df['close']
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    return ma5 > ma10 > ma20 > ma60


def is_market_uptrend(context):
    """大盘环境判断（策略A）"""
    if not g.use_market_filter:
        return True
    df = attribute_history(g.universe, 80, '1d', ['close'], skip_paused=True, df=True)
    if len(df) < g.benchmark_ma:
        return True
    ma = df['close'].rolling(g.benchmark_ma).mean().iloc[-1]
    return df['close'].iloc[-1] > ma


def buy_main_uptrend_stocks(context, current_data):
    """寻找主升浪股票并买入（策略A）"""
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
    主升浪判断：七项条件全部满足才算确认（策略A）。
    """
    d = current_data[stock]
    if d.paused:
        return False
    if d.is_st or 'ST' in d.name or '*' in d.name:
        return False
    if d.last_price >= d.high_limit * 0.995:
        return False
    if d.last_price <= d.low_limit * 1.005:
        return False

    df = attribute_history(stock, 90, '1d',
                           ['close', 'high', 'low', 'volume'],
                           skip_paused=True, df=True)
    if len(df) < 60:
        return False

    close = df['close']
    high = df['high']
    volume = df['volume']

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    # 条件A：均线多头排列
    v5, v10, v20, v60 = ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    if not (v5 > v10 > v20 > v60):
        return False

    # 条件B：收盘价站在 MA5 之上
    if close.iloc[-1] <= v5:
        return False

    # 条件C：乖离率 ≤ 12%
    deviation = (close.iloc[-1] - v20) / v20
    if deviation > g.deviation_max:
        return False

    # 条件D：10日涨幅 5%~25%
    ret_10d = close.iloc[-1] / close.iloc[-10] - 1
    if ret_10d < g.min_return_10d or ret_10d > g.max_return_10d:
        return False

    # 条件E：回调≥5% 后重新突破
    high_20d = high.iloc[-20:].max()
    min_close_20d = close.iloc[-20:].min()
    max_drawdown_20d = (high_20d - min_close_20d) / high_20d
    if max_drawdown_20d < g.pullback_min:
        return False

    min_idx = close.iloc[-20:].values.argmin()
    vol_pullback = volume.iloc[-20 + min_idx:min(-5, -3 + min_idx) + 1].mean() \
        if -20 + min_idx < -3 else volume.iloc[-20:-3].mean()
    vol_breakout = volume.iloc[-3:].mean()
    if vol_pullback > 0 and vol_breakout < vol_pullback * 1.2:
        return False

    # 条件F：近5天至少4天收盘在 MA5 上方
    above_ma5 = sum(1 for i in range(-5, 0) if close.iloc[i] > ma5.iloc[i])
    if above_ma5 < 4:
        return False

    # 条件G：放量
    avg_vol_5 = volume.iloc[-5:].mean()
    avg_vol_60 = volume.iloc[-60:].mean()
    if avg_vol_60 == 0 or avg_vol_5 / avg_vol_60 < g.volume_ratio:
        return False

    return True


# ============================================================
# 策略B —— 日内做T（every_bar 执行）
# ============================================================

def get_t_state(stock):
    """获取某只股票的T状态，不存在则初始化"""
    if stock not in g.t_states:
        g.t_states[stock] = {
            'state': 'flat',         # 'flat' | 'long_t'
            'entry_price': 0.0,
            'amount': 0,
            'count': 0,              # 今日已做T次数
            'vwap_amt': 0.0,         # VWAP累积成交额
            'vwap_vol': 0.0,         # VWAP累积成交量
        }
    return g.t_states[stock]


def intraday_t0(context):
    """分钟级做T主逻辑：对每只持仓股票独立判断（策略B）"""
    now = context.current_dt
    hhmm = now.strftime('%H%M')

    # 时间窗口过滤
    if hhmm < g.trade_start or hhmm >= g.trade_end:
        return

    cd = get_current_data()

    for stock in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[stock]
        if pos.total_amount <= 0:
            continue
        if cd[stock].paused:
            continue

        # 处理单只股票
        intraday_t0_single(context, cd, stock, pos)


def intraday_t0_single(context, cd, stock, pos):
    """对单只股票执行做T判断"""
    ts = get_t_state(stock)

    # --- 1. 取当前分钟bar并累积VWAP ---
    bar = attribute_history(stock, 1, '1m', ['close', 'volume', 'money'], skip_paused=True)
    if bar is None or len(bar) == 0:
        return
    price = float(bar['close'].iloc[-1])
    vol = float(bar['volume'].iloc[-1])
    amt = float(bar['money'].iloc[-1])

    ts['vwap_amt'] += amt
    ts['vwap_vol'] += vol
    if ts['vwap_vol'] <= 0:
        return
    vwap = ts['vwap_amt'] / ts['vwap_vol']
    deviation = (price - vwap) / vwap
    grid_n = deviation / g.grid_step

    # --- 2. 持有T仓位 → 处理平仓 ---
    if ts['state'] == 'long_t':
        pnl = (price - ts['entry_price']) / ts['entry_price']

        # 硬止损
        if pnl <= -g.stop_loss_pct:
            close_t_long(context, cd, stock, price, reason='hard_stop %.2f%%' % (pnl * 100))
            return

        # 止盈：价格回到VWAP上方 或 涨幅≥1格
        if price >= vwap or pnl >= g.grid_step:
            close_t_long(context, cd, stock, price, reason='take_profit %.2f%%' % (pnl * 100))
            return
        return

    # --- 3. 无仓位 → 判断是否开仓做T ---
    if ts['count'] >= g.max_t_per_day:
        return

    # 检查可用现金是否足够（保留一部分现金，防止过度占用）
    min_cash = context.portfolio.total_value * g.t_min_cash
    if context.portfolio.available_cash < min_cash:
        return

    # 5分钟MACD确认
    macd_sig = calc_5min_macd_signal(stock)

    # 正向回转：股价低于VWAP 3格以上 + MACD金叉 → 买入T仓
    if grid_n <= -g.grid_trigger_cnt and macd_sig == 'bull':
        open_t_long(context, cd, stock, price, pos)


def open_t_long(context, cd, stock, price, pos):
    """正向回转开仓：用现金买入T仓（策略B）"""
    ts = get_t_state(stock)

    base_value = pos.value
    t_value = base_value * g.t_position_ratio

    # 保留最低现金
    min_cash = context.portfolio.total_value * g.t_min_cash
    available = max(0, context.portfolio.available_cash - min_cash) * 0.95
    buy_value = min(t_value, available)

    if buy_value < price * 100:
        return

    order_obj = order_value(stock, buy_value)
    if order_obj is None:
        return

    ts['state'] = 'long_t'
    ts['entry_price'] = price
    ts['amount'] = int(buy_value / price / 100) * 100
    ts['count'] += 1
    log.info('[开T多] %s 价格=%.3f 金额=%.0f 当日次数=%d/%d' %
             (stock, price, buy_value, ts['count'], g.max_t_per_day))


def close_t_long(context, cd, stock, price, reason=''):
    """正向回转平仓：卖出等量底仓（策略B）"""
    ts = get_t_state(stock)

    sell_shares = ts['amount']
    if sell_shares <= 0:
        ts['state'] = 'flat'
        return

    sellable = context.portfolio.positions[stock].closeable_amount if stock in context.portfolio.positions else 0
    sell_shares = min(sell_shares, sellable)
    if sell_shares < 100:
        log.warn('[平T多-失败] %s 可卖不足 sellable=%d' % (stock, sellable))
        ts['state'] = 'flat'
        ts['entry_price'] = 0.0
        ts['amount'] = 0
        return

    order(stock, -sell_shares)
    pnl_pct = (price - ts['entry_price']) / ts['entry_price'] * 100
    log.info('[平T多] %s 价格=%.3f 数量=%d 盈亏=%.2f%% 原因=%s' %
             (stock, price, sell_shares, pnl_pct, reason))

    ts['state'] = 'flat'
    ts['entry_price'] = 0.0
    ts['amount'] = 0


def force_close_all_t(context):
    """尾盘 14:55 强制清算所有做T仓位（策略B）"""
    cd = get_current_data()

    for stock in list(g.t_states.keys()):
        ts = g.t_states[stock]
        if ts['state'] != 'long_t':
            continue
        if stock in list(context.portfolio.positions.keys()) and \
           context.portfolio.positions[stock].total_amount > 0:
            if cd[stock].paused:
                continue
            price = cd[stock].last_price
            close_t_long(context, cd, stock, price, reason='force_close_EOD')

    # 取消所有未成交单
    open_orders = get_open_orders()
    for o in open_orders.values():
        cancel_order(o)


# ============================================================
# 工具函数 —— 5分钟MACD信号
# ============================================================

def calc_5min_macd_signal(security):
    """计算5分钟MACD信号：'bull' / 'neutral'（仅做多T）"""
    need = g.macd_slow + g.macd_signal + 5
    df = attribute_history(security, need, '5m', ['close'], skip_paused=True)
    if df is None or len(df) < g.macd_slow + g.macd_signal:
        return 'neutral'

    close = df['close']
    ema_fast = close.ewm(span=g.macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=g.macd_slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=g.macd_signal, adjust=False).mean()
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

    return 'neutral'
