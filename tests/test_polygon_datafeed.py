import sys
import types
from datetime import datetime

import pytest

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData, HistoryRequest


class _Agg:
    def __init__(
        self,
        timestamp: int,
        volume: float,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        vwap: float,
    ) -> None:
        self.timestamp: int = timestamp
        self.volume: float = volume
        self.open: float = open_price
        self.high: float = high_price
        self.low: float = low_price
        self.close: float = close_price
        self.vwap: float = vwap


class _FakeClient:
    def __init__(self, aggs: list[_Agg]) -> None:
        self.aggs: list[_Agg] = aggs
        self.calls: list[dict[str, object]] = []

    def list_aggs(self, **kwargs: object) -> list[_Agg]:
        self.calls.append(dict(kwargs))
        return self.aggs


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def _vendor_module(name: str) -> types.ModuleType:
    module: types.ModuleType = types.ModuleType(name)
    module.__path__ = []
    module.__package__ = name
    sys.modules[name] = module
    if "." in name:
        parent, child = name.rsplit(".", 1)
        setattr(sys.modules[parent], child, module)
    return module


def _install_polygon() -> None:
    _purge("polygon")
    polygon: types.ModuleType = _vendor_module("polygon")
    _vendor_module("polygon.rest")
    aggs: types.ModuleType = _vendor_module("polygon.rest.aggs")

    class RESTClient:
        def __init__(self, api_key: str) -> None:
            raise RuntimeError("polygon RESTClient must not be constructed")

    class Agg:
        pass

    polygon.RESTClient = RESTClient
    aggs.Agg = Agg


_install_polygon()

from vnpy_polygon.polygon_datafeed import DB_TZ, PolygonDatafeed  # noqa: E402


def _clock_ms(clock: datetime) -> int:
    return int(clock.timestamp() * 1000)


def _feed(client: _FakeClient) -> PolygonDatafeed:
    feed: PolygonDatafeed = PolygonDatafeed()
    feed.inited = True
    feed.client = client
    return feed


def _request(symbol: str, exchange: Exchange, interval: Interval) -> HistoryRequest:
    return HistoryRequest(
        symbol=symbol,
        exchange=exchange,
        start=datetime(2024, 1, 2, 9, 30),
        end=datetime(2024, 1, 2, 16, 0),
        interval=interval,
    )


@pytest.mark.parametrize(
    ("interval", "timespan"),
    [
        (Interval.MINUTE, "minute"),
        (Interval.HOUR, "hour"),
        (Interval.DAILY, "day"),
    ],
)
def test_fake_response_becomes_bar(interval: Interval, timespan: str) -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    client: _FakeClient = _FakeClient(
        [
            _Agg(
                timestamp=_clock_ms(clock),
                volume=100.0,
                open_price=10.5,
                high_price=11.0,
                low_price=10.0,
                close_price=10.75,
                vwap=10.75,
            )
        ]
    )
    req: HistoryRequest = _request("AAPL", Exchange.NYSE, interval)
    bars: list[BarData] = _feed(client).query_bar_history(req)

    assert client.calls == [
        {
            "ticker": "AAPL",
            "multiplier": 1,
            "timespan": timespan,
            "from_": req.start,
            "to": req.end,
            "limit": 5000,
        }
    ]
    assert len(bars) == 1
    bar: BarData = bars[0]
    assert bar.symbol == "AAPL"
    assert bar.exchange == Exchange.NYSE
    assert bar.vt_symbol == "AAPL.NYSE"
    assert bar.datetime == clock.replace(tzinfo=DB_TZ)
    assert bar.open_price == 10.5
    assert bar.high_price == 11.0
    assert bar.low_price == 10.0
    assert bar.close_price == 10.75
    assert bar.volume == 100.0


def test_option_symbol_adds_prefix() -> None:
    symbol: str = "AAPL240119C00190000"
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    client: _FakeClient = _FakeClient(
        [
            _Agg(
                timestamp=_clock_ms(clock),
                volume=3.0,
                open_price=1.2,
                high_price=1.4,
                low_price=1.1,
                close_price=1.3,
                vwap=1.25,
            )
        ]
    )
    bars: list[BarData] = _feed(client).query_bar_history(
        _request(symbol, Exchange.NYSE, Interval.MINUTE)
    )

    assert client.calls[0]["ticker"] == "O:" + symbol
    assert len(bars) == 1
    bar: BarData = bars[0]
    assert bar.symbol == symbol
    assert bar.exchange == Exchange.NYSE
    assert bar.vt_symbol == f"{symbol}.NYSE"
    assert bar.datetime == clock.replace(tzinfo=DB_TZ)
    assert bar.open_price == 1.2
    assert bar.high_price == 1.4
    assert bar.low_price == 1.1
    assert bar.close_price == 1.3
    assert bar.volume == 3.0


def test_bar_outside_request_range_is_dropped() -> None:
    inside: datetime = datetime(2024, 1, 2, 10, 0)
    outside: datetime = datetime(2024, 1, 1, 10, 0)
    client: _FakeClient = _FakeClient(
        [
            _Agg(timestamp=_clock_ms(outside), volume=1.0, open_price=1.0, high_price=1.0, low_price=1.0, close_price=1.0, vwap=1.0),
            _Agg(timestamp=_clock_ms(inside), volume=100.0, open_price=10.5, high_price=11.0, low_price=10.0, close_price=10.75, vwap=10.75),
        ]
    )

    bars: list[BarData] = _feed(client).query_bar_history(_request("AAPL", Exchange.NYSE, Interval.MINUTE))

    assert len(bars) == 1
    assert bars[0].symbol == "AAPL"
    assert bars[0].datetime == inside.replace(tzinfo=DB_TZ)
    assert bars[0].open_price == 10.5
    assert bars[0].high_price == 11.0
    assert bars[0].low_price == 10.0
    assert bars[0].close_price == 10.75
    assert bars[0].volume == 100.0


def test_unsupported_interval_does_not_query() -> None:
    client: _FakeClient = _FakeClient([])
    logs: list[str] = []

    assert _feed(client).query_bar_history(_request("AAPL", Exchange.NYSE, Interval.WEEKLY), logs.append) == []
    assert client.calls == []
    assert logs == ["Polygon.io查询K线数据失败：不支持的时间周期w"]
