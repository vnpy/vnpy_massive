"""Polygon.io历史数据服务实现。"""

from datetime import datetime
from typing import Any, cast
from collections.abc import Iterator, Callable

from polygon import RESTClient
from polygon.rest.aggs import Agg

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData, TickData, HistoryRequest
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.setting import SETTINGS
from vnpy.trader.database import DB_TZ


INTERVAL_VT2POLYGON: dict[Interval, str] = {
    Interval.MINUTE: "minute",
    Interval.HOUR: "hour",
    Interval.DAILY: "day",
}


class PolygonDatafeed(BaseDatafeed):
    """Polygon.io数据服务接口"""

    def __init__(self) -> None:
        """读取API密钥，并标记尚未初始化。"""
        self.api_key: str = SETTINGS["datafeed.password"]

        self.client: RESTClient
        self.inited: bool = False

    def init(self, output: Callable[[str], Any] = print) -> bool:
        """初始化"""
        if self.inited:
            return True

        if not self.api_key:
            output("Polygon.io数据服务初始化失败：API密钥为空！")
            return False

        e: Exception
        try:
            self.client = RESTClient(self.api_key)

            self.client.get_exchanges(asset_class='options')
        except Exception as e:
            output(f"Polygon.io数据服务初始化失败：{e}")
            return False

        self.inited = True
        return True

    def query_bar_history(self, req: HistoryRequest, output: Callable[[str], Any] = print) -> list[BarData]:
        """查询K线数据"""
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return []

        symbol: str = req.symbol
        exchange: Exchange = req.exchange
        interval: Interval = cast(Interval, req.interval)
        start: datetime = req.start
        end: datetime = cast(datetime, req.end)

        polygon_interval: str | None = INTERVAL_VT2POLYGON.get(interval)
        if not polygon_interval:
            output(f"Polygon.io查询K线数据失败：不支持的时间周期{interval.value}")
            return []

        if len(symbol) > 10:
            symbol = "O:" + symbol  # Polygon要求期权代码前加O:前缀

        # polygon客户端的list_aggs方法返回一个处理分页的迭代器
        aggs: Iterator[Agg] = self.client.list_aggs(
            ticker=symbol,
            multiplier=1,
            timespan=polygon_interval,
            from_=start,
            to=end,
            limit=5000      # 每次查5000条
        )

        bars: list[BarData] = []
        agg: Agg
        for agg in aggs:
            # Polygon时间戳是毫秒，转换为datetime
            dt: datetime = datetime.fromtimestamp(agg.timestamp / 1000)

            # list_aggs可能返回超出请求范围的数据，所以需要过滤
            if not (start <= dt <= end):
                continue

            bar: BarData = BarData(
                symbol=req.symbol,
                exchange=exchange,
                datetime=dt.replace(tzinfo=DB_TZ),
                interval=interval,
                volume=agg.volume,
                open_price=agg.open,
                high_price=agg.high,
                low_price=agg.low,
                close_price=agg.close,
                turnover=agg.vwap * agg.volume,
                gateway_name="POLYGON"
            )
            bars.append(bar)

        return bars

    def query_tick_history(self, req: HistoryRequest, output: Callable[[str], Any] = print) -> list[TickData]:
        """查询Tick数据"""
        return []
