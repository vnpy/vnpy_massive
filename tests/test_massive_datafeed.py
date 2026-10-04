from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
import requests

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ
from vnpy.trader.object import BarData, HistoryRequest
from vnpy.trader.setting import SETTINGS

from vnpy_massive.massive_datafeed import MassiveDatafeed


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code: int = status_code
        self.text: str = text
        self._payload: dict[str, Any] = {} if payload is None else payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _Http:
    def __init__(self, responses: list[_Response], error: Exception | None = None) -> None:
        self.responses: list[_Response] = list(responses)
        self.error: Exception | None = error
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


@contextmanager
def _http(responses: list[_Response], error: Exception | None = None) -> Iterator[_Http]:
    client: _Http = _Http(responses, error)
    with patch("vnpy_massive.massive_datafeed.requests.get", client.get):
        yield client


def _clock_ms(clock: datetime) -> int:
    return int(clock.timestamp() * 1000)


def _bar(clock: datetime, **fields: float) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "t": _clock_ms(clock),
        "v": 100.0,
        "o": 10.5,
        "h": 11.0,
        "l": 10.0,
        "c": 10.75,
        "vw": 10.75,
    }
    payload.update(fields)
    return payload


def _feed(*, ready: bool = True, password: str = "test-key", max_retries: int = 0) -> MassiveDatafeed:
    SETTINGS["datafeed.password"] = password
    feed: MassiveDatafeed = MassiveDatafeed(max_retries=max_retries, base_backoff=0.0)
    feed.inited = ready
    return feed


_END: datetime = datetime(2024, 1, 2, 16, 0)


def _request(
    symbol: str,
    exchange: Exchange,
    interval: Interval | None,
    end: datetime | None = _END,
) -> HistoryRequest:
    return HistoryRequest(
        symbol=symbol,
        exchange=exchange,
        start=datetime(2024, 1, 2, 9, 30),
        end=end,
        interval=interval,
    )


def _agg_url(ticker: str, timespan: str) -> str:
    return f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/{timespan}/2024-01-02/2024-01-02"


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
    http: _Http
    with _http([_Response(200, {"results": [_bar(clock)]})]) as http:
        bars: list[BarData] = _feed().query_bar_history(_request("AAPL", Exchange.NYSE, interval))

    assert http.calls == [
        {
            "url": _agg_url("AAPL", timespan),
            "params": {"limit": "50000", "sort": "asc", "apiKey": "test-key"},
            "timeout": 30.0,
        }
    ]
    assert len(bars) == 1
    bar: BarData = bars[0]
    assert bar.symbol == "AAPL"
    assert bar.exchange == Exchange.NYSE
    assert bar.vt_symbol == "AAPL.NYSE"
    assert bar.datetime == clock.replace(tzinfo=DB_TZ)
    assert bar.interval == interval
    assert bar.open_price == 10.5
    assert bar.high_price == 11.0
    assert bar.low_price == 10.0
    assert bar.close_price == 10.75
    assert bar.volume == 100.0
    assert bar.turnover == 1075.0
    assert bar.gateway_name == "MASSIVE"


def test_option_symbol_adds_prefix() -> None:
    symbol: str = "AAPL240119C00190000"
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    http: _Http
    with _http([_Response(200, {"results": [_bar(clock, v=3.0, o=1.2, h=1.4, l=1.1, c=1.3, vw=1.25)]})]) as http:
        bars: list[BarData] = _feed().query_bar_history(_request(symbol, Exchange.NYSE, Interval.MINUTE))

    assert http.calls[0]["url"] == _agg_url("O:" + symbol, "minute")
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
    assert bar.turnover == 3.75
    assert bar.gateway_name == "MASSIVE"


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [
        ("ABCDEFGHIJ", "ABCDEFGHIJ"),
        ("ABCDEFGHIJK", "O:ABCDEFGHIJK"),
        ("SPX", "I:SPX"),
        ("O:SPX", "O:SPX"),
        ("  AAPL  ", "AAPL"),
    ],
)
def test_ticker_mapping(symbol: str, ticker: str) -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    http: _Http
    with _http([_Response(200, {"results": [_bar(clock)]})]) as http:
        bars: list[BarData] = _feed().query_bar_history(_request(symbol, Exchange.NYSE, Interval.MINUTE))

    assert http.calls[0]["url"] == _agg_url(ticker, "minute")
    assert len(bars) == 1
    assert bars[0].symbol == symbol


def test_missing_vwap_turnover_is_zero() -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    payload: dict[str, Any] = _bar(clock)
    del payload["vw"]
    bars: list[BarData]
    with _http([_Response(200, {"results": [payload]})]):
        bars = _feed().query_bar_history(_request("AAPL", Exchange.NYSE, Interval.MINUTE))

    assert len(bars) == 1
    assert bars[0].volume == 100.0
    assert bars[0].turnover == 0.0


def test_bar_outside_request_range_is_dropped() -> None:
    inside: datetime = datetime(2024, 1, 2, 10, 0)
    outside: datetime = datetime(2024, 1, 1, 10, 0)
    bars: list[BarData]
    with _http([_Response(200, {"results": [_bar(outside, v=1.0, o=1.0, h=1.0, l=1.0, c=1.0, vw=1.0), _bar(inside)]})]):
        bars = _feed().query_bar_history(_request("AAPL", Exchange.NYSE, Interval.MINUTE))

    assert len(bars) == 1
    assert bars[0].symbol == "AAPL"
    assert bars[0].datetime == inside.replace(tzinfo=DB_TZ)
    assert bars[0].open_price == 10.5
    assert bars[0].high_price == 11.0
    assert bars[0].low_price == 10.0
    assert bars[0].close_price == 10.75
    assert bars[0].volume == 100.0


def test_unsupported_interval_does_not_query() -> None:
    logs: list[str] = []
    http: _Http
    with _http([]) as http:
        assert _feed().query_bar_history(_request("AAPL", Exchange.NYSE, Interval.WEEKLY), logs.append) == []

    assert http.calls == []
    assert logs == ["MassiveDatafeed 查询K线数据失败：不支持的时间周期w"]


def test_query_tick_history_is_empty() -> None:
    http: _Http
    with _http([]) as http:
        assert _feed().query_tick_history(_request("AAPL", Exchange.NYSE, Interval.TICK)) == []
    assert http.calls == []


def test_init_rejects_empty_key() -> None:
    logs: list[str] = []
    http: _Http
    with _http([]) as http:
        feed: MassiveDatafeed = _feed(ready=False, password="")
        assert feed.init(logs.append) is False

    assert feed.inited is False
    assert http.calls == []
    assert logs == ["MassiveDatafeed 初始化失败：API 密钥为空，请配置 datafeed.password"]


def test_init_rejects_http_error() -> None:
    logs: list[str] = []
    with _http([_Response(401, text="denied")]):
        feed: MassiveDatafeed = _feed(ready=False)
        assert feed.init(logs.append) is False

    assert feed.inited is False
    assert logs == ["MassiveDatafeed 初始化失败：HTTP 401: denied"]


def test_init_rejects_network_error() -> None:
    logs: list[str] = []
    with _http([], error=requests.ConnectionError("offline")):
        feed: MassiveDatafeed = _feed(ready=False)
        assert feed.init(logs.append) is False

    assert feed.inited is False
    assert logs == ["MassiveDatafeed 初始化失败：offline"]


def test_init_marks_ready() -> None:
    http: _Http
    with _http([_Response(200, {"results": []})]) as http:
        feed: MassiveDatafeed = _feed(ready=False)
        assert feed.init() is True
        assert feed.inited is True
        assert http.calls[0]["url"] == "https://api.massive.com/v3/reference/exchanges"
        assert http.calls[0]["params"] == {"apiKey": "test-key"}
        assert feed.init() is True
        assert len(http.calls) == 1


def test_query_inits_when_needed() -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    http: _Http
    with _http([
        _Response(200, {"results": []}),
        _Response(200, {"results": [_bar(clock)]}),
    ]) as http:
        feed: MassiveDatafeed = _feed(ready=False)
        bars: list[BarData] = feed.query_bar_history(_request("AAPL", Exchange.NYSE, Interval.MINUTE))

    assert feed.inited is True
    assert len(http.calls) == 2
    assert http.calls[0]["url"] == "https://api.massive.com/v3/reference/exchanges"
    assert http.calls[1]["url"] == _agg_url("AAPL", "minute")
    assert len(bars) == 1
    assert bars[0].gateway_name == "MASSIVE"


def test_second_page_is_followed() -> None:
    first: datetime = datetime(2024, 1, 2, 10, 0)
    second: datetime = datetime(2024, 1, 2, 10, 1)
    next_url: str = _agg_url("AAPL", "minute") + "?cursor=abc"
    http: _Http
    with _http([
        _Response(200, {"results": [_bar(first)], "next_url": next_url}),
        _Response(200, {"results": [_bar(second, c=10.8)]}),
    ]) as http:
        bars: list[BarData] = _feed().query_bar_history(_request("AAPL", Exchange.NYSE, Interval.MINUTE))

    assert http.calls[1]["url"] == next_url + "&apiKey=test-key"
    assert http.calls[1]["params"] is None
    assert [bar.datetime for bar in bars] == [
        first.replace(tzinfo=DB_TZ),
        second.replace(tzinfo=DB_TZ),
    ]
    assert bars[1].close_price == 10.8


def test_second_page_replaces_existing_api_key() -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    next_url: str = _agg_url("AAPL", "minute") + "?cursor=abc&apiKey=old-key"
    http: _Http
    with _http([
        _Response(200, {"results": [_bar(clock)], "next_url": next_url}),
        _Response(200, {"results": []}),
    ]) as http:
        _feed().query_bar_history(_request("AAPL", Exchange.NYSE, Interval.MINUTE))

    assert http.calls[1]["url"] == _agg_url("AAPL", "minute") + "?cursor=abc&apiKey=test-key"


def test_second_page_http_error_returns_partial() -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    logs: list[str] = []
    http: _Http
    with _http([
        _Response(200, {"results": [_bar(clock)], "next_url": _agg_url("AAPL", "minute") + "?cursor=abc"}),
        _Response(400, text="bad cursor"),
    ]) as http:
        bars: list[BarData] = _feed().query_bar_history(
            _request("AAPL", Exchange.NYSE, Interval.MINUTE),
            logs.append,
        )

    assert len(http.calls) == 2
    assert len(bars) == 1
    assert bars[0].datetime == clock.replace(tzinfo=DB_TZ)
    assert logs == ["MassiveDatafeed 分页中断，返回已取得的 K 线：HTTP 400: bad cursor"]


def test_query_http_error_returns_empty() -> None:
    logs: list[str] = []
    with _http([_Response(403, text="forbidden")]):
        bars: list[BarData] = _feed().query_bar_history(
            _request("AAPL", Exchange.NYSE, Interval.MINUTE),
            logs.append,
        )

    assert bars == []
    assert logs == ["MassiveDatafeed 查询K线数据失败：HTTP 403: forbidden"]


def test_server_error_is_retried() -> None:
    clock: datetime = datetime(2024, 1, 2, 10, 0)
    http: _Http
    with _http([
        _Response(500, text="unavailable"),
        _Response(200, {"results": [_bar(clock)]}),
    ]) as http:
        bars: list[BarData] = _feed(max_retries=1).query_bar_history(
            _request("AAPL", Exchange.NYSE, Interval.MINUTE)
        )

    assert len(http.calls) == 2
    assert len(bars) == 1
    assert bars[0].close_price == 10.75


def test_retries_exhausted_returns_empty() -> None:
    logs: list[str] = []
    with _http([_Response(429, text="slow down")]):
        bars: list[BarData] = _feed().query_bar_history(
            _request("AAPL", Exchange.NYSE, Interval.MINUTE),
            logs.append,
        )

    assert bars == []
    assert len(logs) == 1
    assert logs[0].startswith("MassiveDatafeed 查询K线数据失败：Max retries exceeded for ")
