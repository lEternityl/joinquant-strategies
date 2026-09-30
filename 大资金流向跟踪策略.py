# -*- coding: utf-8 -*-
"""
大资金流向跟踪策略
==================
基于聚宽 get_money_flow 接口，跟踪主力资金（超大单 + 大单）净流向，
买入主力持续净流入的股票，卖出主力资金流出的股票，跟随大资金方向操作。

核心指标：
  - net_amount_main（主力净额，万元）= 超大单净额 + 大单净额
  - net_pct_main（主力净占比，%）= 主力净额 / 成交额
  - 正值 = 主力净买入（资金流入），负值 = 主力净卖出（资金流出）

调仓频率：每日
股票池：  沪深300（可改为中证500等其他指数）
风险控制：单票仓位上限、主力净流出卖出、避开涨跌停和ST
"""

from jqdata import *

# ============================================================
# 策略参数（可根据需要调整）
# ============================================================
UNIVERSE = '000300.XSHG'      # 股票池索引：沪深300
HOLD_NUM = 10                 # 持仓数量
LOOKBACK = 10                 # 资金流向回看天数
MIN_MARKET_CAP = 50           # 最低市值过滤（亿元），排除壳股
MAX_SINGLE_POSITION = 0.15   # 单票仓位上限
SELL_THRESHOLD = -2.0        # 主力净占比低于此阈值强制卖出（%）


def initialize(context):
    """策略初始化"""
    # ---- 必选设置 ----
    set_benchmark(UNIVERSE)
    set_option('use_real_price', True)
    set_option('order_volume_ratio', 0.25)

    # ---- 交易费用 ----
    set_order_cost(OrderCost(
        open_tax=0,
        close_tax=0.001,               # 卖出印花税 0.1%
        open_commission=0.0003,        # 买入佣金万3
        close_commission=0.0003,       # 卖出佣金万3
        close_today_commission=0,
        min_commission=5,              # 最低佣金5元
    ), type='stock')

    # ---- 滑点 ----
    set_slippage(PriceRelatedSlippage(0.00246))  # 买卖各1跳，合计约0.246%

    # ---- 调度 ----
    run_daily(prepare_stock_pool, time='09:00')   # 盘前选股
    run_daily(trade, time='09:31')                 # 开盘后交易

    # ---- 全局变量 ----
    g.candidate_stocks = []  # 候选股票池（盘前更新）


def prepare_stock_pool(context):
    """盘前准备：获取并过滤候选股票池"""
    date = context.current_dt.strftime('%Y-%m-%d')
    all_stocks = get_index_stocks(UNIVERSE, date)
    g.candidate_stocks = filter_stocks(all_stocks)
    log.info('{} 候选股票池: {} 只（过滤后）'.format(
        date, len(g.candidate_stocks)))


def trade(context):
    """主交易逻辑"""
    stocks = g.candidate_stocks
    if len(stocks) < HOLD_NUM:
        log.warn('候选股票不足 {} 只，实际 {} 只，跳过调仓'.format(
            HOLD_NUM, len(stocks)))
        return

    # ---- 1. 计算主力资金流向得分 ----
    scores = calc_money_flow_score(stocks)
    if scores is None or len(scores) < HOLD_NUM:
        log.warn('有效资金流向数据不足，跳过调仓')
        return

    # ---- 2. 检查现有持仓是否需要强制卖出 ----
    current_data = get_current_data()
    for s in list(context.portfolio.positions.keys()):
        # 停牌 / 跌停的不强制卖，卖不掉
        if current_data[s].paused:
            continue
        if current_data[s].day_open == current_data[s].low_limit:
            continue
        # 不在评分列表中的（数据缺失），先保留
        if s not in scores:
            continue
        # 强制卖出条件：主力净占比低于阈值
        if scores[s] < SELL_THRESHOLD:
            log.info('主力资金持续流出，强制卖出 {}（得分 {:.2f}）'.format(
                current_data[s].name, scores[s]))
            order_target(s, 0)

    # ---- 3. 选取得分最高的 HOLD_NUM 只作为目标池 ----
    target = scores.nlargest(HOLD_NUM)
    target_stocks = target.index.tolist()

    # ---- 4. 卖出不在目标池的持仓 ----
    for s in list(context.portfolio.positions.keys()):
        if s not in target_stocks:
            if current_data[s].paused:
                continue
            if current_data[s].day_open == current_data[s].low_limit:
                continue
            order_target(s, 0)

    # ---- 5. 等权重买入目标池 ----
    weight = min(1.0 / HOLD_NUM, MAX_SINGLE_POSITION)
    target_value = context.portfolio.total_value * weight

    for s in target_stocks:
        # 涨停不追
        if current_data[s].day_open == current_data[s].high_limit:
            continue
        order_target_value(s, target_value)

    # ---- 6. 日志输出 ----
    log.info('今日持仓目标 ({} 只)：'.format(len(target_stocks)))
    for s, score in target.items():
        log.info('  {} {}：得分 {:.2f}'.format(
            current_data[s].name, s, score))


def calc_money_flow_score(stocks):
    """
    计算主力资金流向综合得分。

    评分维度（权重）：
      - 主力净占比均值（50%）：主力资金净买入占成交额的比例
      - 近期趋势变化（30%）：主力资金是加速流入还是减速
      - 累计净额得分（20%）：累计净买入金额的标准化得分

    返回：
      pd.Series，index=股票代码，value=综合得分（越高越好）
    """
    import pandas as pd
    import numpy as np

    # 分批获取数据（避免单次查询股票过多）
    all_data = []
    batch_size = 50
    for i in range(0, len(stocks), batch_size):
        batch = stocks[i:i + batch_size]
        try:
            df = get_money_flow(
                batch,
                count=LOOKBACK,
                fields=['date', 'sec_code', 'net_amount_main', 'net_pct_main']
            )
            if df is not None and len(df) > 0:
                all_data.append(df)
        except Exception as e:
            log.warn('获取资金流向失败 batch {}: {}'.format(i, e))

    if not all_data:
        return None

    df_all = pd.concat(all_data)

    scores = {}
    for s in stocks:
        s_data = df_all[df_all['sec_code'] == s]
        if len(s_data) < max(LOOKBACK // 2, 3):
            # 数据太少，跳过
            continue

        pct = s_data['net_pct_main']
        amount = s_data['net_amount_main']

        # ---- 得分1: 主力净占比均值（权重 50%） ----
        avg_pct = pct.mean()

        # ---- 得分2: 近期趋势（权重 30%） ----
        # 比较最近 3 天和前 N-3 天的均值差，判断资金流向方向
        split = min(3, len(s_data) - 1)
        if len(s_data) > split:
            recent = pct.tail(split).mean()
            earlier = pct.head(len(s_data) - split).mean()
            trend = recent - earlier
        else:
            trend = 0

        # ---- 得分3: 累计净额得分（权重 20%） ----
        # 主力净额单位是万元，数量级差异大，取对数平滑
        cum_amount = amount.sum()
        if cum_amount > 0:
            amount_score = np.log1p(cum_amount) * 0.1
        elif cum_amount < 0:
            amount_score = -np.log1p(abs(cum_amount)) * 0.1
        else:
            amount_score = 0

        # ---- 综合得分 ----
        score = avg_pct * 0.5 + trend * 0.3 + amount_score * 0.2

        scores[s] = score

    return pd.Series(scores)


def filter_stocks(stocks):
    """
    过滤候选股票：
      - 排除停牌
      - 排除 ST / *ST
      - 排除开盘涨停或跌停（无法买入/卖出）
    """
    current = get_current_data()
    result = []
    for s in stocks:
        d = current[s]
        if d.paused:
            continue
        if d.is_st:
            continue
        if 'ST' in d.name or '*' in d.name:
            continue
        if d.day_open == d.high_limit:
            continue
        if d.day_open == d.low_limit:
            continue
        result.append(s)
    return result
