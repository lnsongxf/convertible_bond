import numpy as np
import os
import pandas as pd
from datetime import date, timedelta
from collections.abc import Callable


# Code to run on joinquant
# 初始化函数，设定基准等等
def initialize(context):
    # 设定沪深300作为基准
    set_benchmark('000300.XSHG')
    # 开启动态复权模式(真实价格)
    set_option('use_real_price', True)
    # 输出内容到日志 log.info()
    log.info('初始函数开始运行且全局只运行一次')
    # 过滤掉order系列API产生的比error级别低的log
    # log.set_level('order', 'error')

    ### 股票相关设定 ###
    # 股票类每笔交易时的手续费是：买入时佣金万分之三，卖出时佣金万分之三加千分之一印花税, 每笔交易佣金最低扣5块钱
    set_order_cost(OrderCost(close_tax=0.001,
                             open_commission=0.0003,
                             close_commission=0.0003,
                             min_commission=5),
                   type='stock')

    g.top = 20

    ## 运行函数（reference_security为运行时间的参考标的；传入的标的只做种类区分，因此传入'000300.XSHG'或'510300.XSHG'是一样的）
    # 开盘前运行
    # run_daily(before_market_open, time='before_open', reference_security='000300.XSHG')
    # 开盘时运行
    run_daily(market_open, time='open', reference_security='000300.XSHG')
    # 收盘后运行
    # run_daily(after_market_close, time='after_close', reference_security='000300.XSHG')


## 开盘时运行函数
def market_open(context):
    if context.current_dt.weekday() != 4:
        return
    log.info('market_open: Today is Friday, adjust holdings...')
    # 给微信发送消息（添加模拟交易，并绑定微信生效）
    # send_message('今天调仓')

    df_date, df_basic_info, df_latest_bond_price, df_latest_stock_price, df_convert_price_adjust = fetch_jqdata(
    )
    log.info('Using latest jqdata from date: %s' %
             df_date.strftime('%Y-%m-%d'))
    df = massage_jqdata(df_basic_info, df_latest_bond_price,
                        df_latest_stock_price, df_convert_price_adjust)
    candidates = execute_strategy(df, double_low, {
        'weight_bond_price': 0.5,
        'weight_convert_premium_rate': 0.5,
        'top': g.top,
    })
    log.info('Candidates:\n%s' % candidates[[
        'code', 'short_name', 'bond_price', 'convert_premium_rate',
        'double_low'
    ]])
    orders = generate_orders(set(),
                             set(g.candidates.reset_index().code.tolist()))
    execute_orders(orders)


def execute_orders(orders: dict[str, set[str]]):
    for code in orders['sell']:
        security = g.candidates.loc[code]
        log.info('Selling %s %s' % (code, security.short_name))
        order_target(code, 0)

    for code in orders['hold']:
        security = g.candidates.loc[code]
        log.info('Holding %s %s' % (code, security.short_name))
        order_target_value(code, g.portfolio.total_value / g.top)

    for code in orders['buy']:
        security = g.candidates.loc[code]
        log.info('Buying %s %s' % (code, security.short_name))
        order_target_value(code, g.portfolio.total_value / g.top)


# To use this locally, need to call auth() first
def fetch_jqdata(
) -> tuple[date, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    yesterday = date.today() - timedelta(days=1)
    txn_day = get_trade_days(end_date=yesterday, count=1)[0]
    df_basic_info = bond.run_query(query(bond.CONBOND_BASIC_INFO))
    # Filter non-conbond, e.g. exchange bond
    df_basic_info = df_basic_info[df_basic_info.bond_type_id == 703013]
    # Keep active bonds only
    df_basic_info = df_basic_info[df_basic_info.list_status_id == 301001]
    df_latest_bond_price = bond.run_query(
        query(bond.CONBOND_DAILY_PRICE).filter(
            bond.CONBOND_DAILY_PRICE.date == txn_day))
    df_latest_stock_price = get_price(df_basic_info.company_code.tolist(),
                                      start_date=txn_day,
                                      end_date=txn_day,
                                      frequency='daily')
    df_convert_price_adjust = bond.run_query(
        query(bond.CONBOND_CONVERT_PRICE_ADJUST))
    return txn_day, df_basic_info, df_latest_bond_price, df_latest_stock_price, df_convert_price_adjust


def massage_jqdata(df_basic_info: pd.DataFrame,
                   df_latest_bond_price: pd.DataFrame,
                   df_latest_stock_price: pd.DataFrame,
                   df_convert_price_adjust: pd.DataFrame) -> pd.DataFrame:
    # Data cleaning
    df_basic_info = df_basic_info[[
        'code', 'short_name', 'company_code', 'convert_price'
    ]]
    df_latest_bond_price = df_latest_bond_price[[
        'code', 'close'
    ]].rename(columns={'close': 'bond_price'})
    df_latest_stock_price = df_latest_stock_price[[
        'code', 'close'
    ]].rename(columns={'close': 'stock_price'})
    df_convert_price_adjust = df_convert_price_adjust[[
        'code', 'new_convert_price'
    ]].groupby('code').min()

    # Join basic_info with latest_bond_price to get close price from last transaction day
    # Schema: code, short_name, company_code, convert_price, bond_price
    df = df_basic_info.set_index('code').join(
        df_latest_bond_price.set_index('code')).reset_index()
    # Keep only bonds that are listed and also can be traded
    # Some bonds are still listed, but is not traded (e.g. 2021-08-26, 123029)
    df = df[df.bond_price > 0]

    # Join with convert_price_adjust to get latest convert price
    # code in convert_price_latest is str, while code in df is int64
    df['code'] = df.code.astype(str)
    # Schema: code, short_name, company_code, convert_price, bond_price, new_convert_price
    df = df.set_index('code').join(df_convert_price_adjust)

    # Join with latest_stock_price to get latest stock price
    # Schema: code, short_name, company_code, convert_price, bond_price, new_convert_price, stock_price
    df = df.reset_index().set_index('company_code').join(
        df_latest_stock_price.set_index('code'))

    # Calculate convert_premium_rate
    # Schema: code, short_name, company_code, convert_price, bond_price, new_convert_price, stock_price, convert_premium_rate
    df['convert_premium_rate'] = df.bond_price / (100 / df.new_convert_price *
                                                  df.stock_price) - 1
    return df


# config: Expect to have two keys: weight_bond_price and weight_convert_premium_rate
def double_low(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    dl_df = df
    if not 'weight_bond_price' in config:
        raise 'Bad config: weight_bond_price not found'
    if not 'weight_convert_premium_rate' in config:
        raise 'Bad config: weight_convert_premium_rate not found'
    if not 'top' in config:
        raise 'Bad config: top not found'
    weight_bond_price = config['weight_bond_price']
    weight_convert_premium_rate = config['weight_convert_premium_rate']
    top = config['top']
    dl_df[
        'double_low'] = df.bond_price * weight_bond_price + df.convert_premium_rate * 100 * weight_convert_premium_rate
    return dl_df.nsmallest(top, 'double_low')


def generate_orders(holdings: set[str],
                    candidates: set[str]) -> dict[str, set[str]]:
    orders = {}
    orders['buy'] = candidates - holdings
    orders['sell'] = holdings - candidates
    orders['hold'] = holdings & candidates
    return orders


def execute_strategy(df: pd.DataFrame, strategy: Callable[[pd.DataFrame],
                                                          pd.DataFrame],
                     config: dict) -> pd.DataFrame:
    return strategy(df, config)


def fetch_cache(
    cache_dir: str
) -> tuple[date, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    df_basic_info = pd.read_excel(os.path.join(cache_dir, 'basic_info.xlsx'))
    df_latest_bond_price = pd.read_excel(
        os.path.join(cache_dir, 'latest_bond_price.xlsx'))
    df_latest_stock_price = pd.read_excel(
        os.path.join(cache_dir, 'latest_stock_price.xlsx'))
    df_convert_price_adjust = pd.read_excel(
        os.path.join(cache_dir, 'convert_price_adjust.xlsx'))
    return df_latest_bond_price.date[
        0], df_basic_info, df_latest_bond_price, df_latest_stock_price, df_convert_price_adjust