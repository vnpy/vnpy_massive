"""Massive 历史数据服务实现。"""

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote

import requests

from vnpy.trader.constant import Interval
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.database import DB_TZ
from vnpy.trader.object import BarData, HistoryRequest, TickData
from vnpy.trader.setting import SETTINGS


BASE_URL: str = "https://api.massive.com"
INDEX_UNDERLYINGS: frozenset[str] = frozenset({"SPX", "NDX", "RUT", "DJX", "VIX"})
INTERVAL_VT2MASSIVE: dict[Interval, str] = {
    Interval.MINUTE: "minute",
    Interval.HOUR: "hour",
    Interval.DAILY: "day",
}


def _to_massive_ticker(symbol: str) -> str:
    """把 VeighNa symbol 转成 Massive ticker。

    已带 ``O:`` 的代码保持不变。``SPX``、``NDX``、``RUT``、``DJX``、``VIX``
    补 ``I:``。长度大于 10 的代码视作期权，补 ``O:``。
    """
    stripped: str = symbol.strip()
    if stripped.startswith("O:"):
        return stripped
    if stripped in INDEX_UNDERLYINGS:
        return f"I:{stripped}"
    if len(stripped) > 10:
        return f"O:{stripped}"
    return stripped


def _with_api_key(url: str, api_key: str) -> str:
    """保证下一页 URL 带上当前 API 密钥，并替换旧的 apiKey。"""
    encoded: str = quote(api_key, safe="")
    base: str
    query: str
    base, _sep, query = url.partition("?")
    pairs: list[str] = [
        item for item in query.split("&") if item and not item.startswith("apiKey=")
    ]
    pairs.append(f"apiKey={encoded}")
    return base + "?" + "&".join(pairs)


def _as_object(payload: Any) -> dict[str, Any]:
    """要求 JSON 正文是对象。"""
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    raise RuntimeError("Massive 响应不是 JSON 对象")


def _rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """取出 results 里的对象。"""
    raw: Any = data.get("results", [])
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    item: Any
    for item in raw:
        if isinstance(item, dict):
            rows.append(cast(dict[str, Any], item))
    return rows


def _number(row: dict[str, Any], key: str) -> float:
    """读取数值字段。缺失或 null 时按 0。"""
    raw: Any = row.get(key)
    if raw is None:
        return 0.0
    return float(raw)


class MassiveDatafeed(BaseDatafeed):
    """Massive REST 数据服务，查询美股 K 线。"""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        limit: int = 50000,
    ) -> None:
        """从 datafeed.password 读取 API 密钥。密钥在构造时确定。"""
        self.api_key: str = SETTINGS["datafeed.password"]
        self.base_url: str = base_url.rstrip("/")
        self.timeout: float = timeout
        self.max_retries: int = max_retries
        self.base_backoff: float = base_backoff
        self.limit: int = limit
        self.inited: bool = False

    def init(self, output: Callable[[str], Any] = print) -> bool:
        """用交易所列表接口检查密钥。已经初始化过则直接返回 True。"""
        if self.inited:
            return True

        if not self.api_key:
            output("MassiveDatafeed 初始化失败：API 密钥为空，请配置 datafeed.password")
            return False

        exc: Exception
        try:
            self._request(
                f"{self.base_url}/v3/reference/exchanges",
                {"apiKey": self.api_key},
            )
        except Exception as exc:
            output(f"MassiveDatafeed 初始化失败：{exc}")
            return False

        self.inited = True
        return True

    def _request(self, url: str, params: dict[str, str] | None = None) -> Any:
        """GET 一次 URL。429 和 5xx 指数退避，耗尽后抛出 RuntimeError。"""
        attempt: int
        for attempt in range(self.max_retries + 1):
            try:
                resp: Any = requests.get(url, params=params, timeout=self.timeout)
            except requests.RequestException:
                if attempt < self.max_retries:
                    time.sleep(self.base_backoff * (2 ** attempt))
                    continue
                raise

            status: int = int(resp.status_code)
            if status == 200:
                return resp
            if status == 429 or status >= 500:
                if attempt < self.max_retries:
                    time.sleep(self.base_backoff * (2 ** attempt))
                    continue
                raise RuntimeError(f"Max retries exceeded for {url}")

            text: str = str(resp.text)[:200]
            raise RuntimeError(f"HTTP {status}: {text}")

        raise RuntimeError(f"Max retries exceeded for {url}")

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """请求一条路径，并把 JSON 对象作为结果返回。"""
        query: dict[str, str] = dict(params or {})
        query["apiKey"] = self.api_key
        resp: Any = self._request(f"{self.base_url}{path}", query)
        return _as_object(resp.json())

    def _get_all_pages(
        self,
        path: str,
        params: dict[str, str] | None = None,
        output: Callable[[str], Any] = print,
        max_pages: int = 500,
    ) -> list[dict[str, Any]]:
        """分页 GET。某一页失败时返回已经取得的记录，不再请求同一地址。"""
        data: dict[str, Any] = self._get(path, params)
        all_results: list[dict[str, Any]] = _rows(data)
        page: int = 1

        while page < max_pages:
            raw_next: Any = data.get("next_url")
            if not isinstance(raw_next, str) or not raw_next:
                break

            next_url: str = _with_api_key(raw_next, self.api_key)
            try:
                resp: Any = self._request(next_url)
            except (requests.RequestException, RuntimeError) as exc:
                output(f"MassiveDatafeed 分页中断，返回已取得的 K 线：{exc}")
                return all_results

            payload: Any = resp.json()
            if not isinstance(payload, dict):
                output("MassiveDatafeed 分页中断，返回已取得的 K 线：响应不是 JSON 对象")
                return all_results

            data = cast(dict[str, Any], payload)
            all_results.extend(_rows(data))
            page += 1

        return all_results

    def query_bar_history(
        self,
        req: HistoryRequest,
        output: Callable[[str], Any] = print,
    ) -> list[BarData]:
        """查询 K 线。

        指数代码只自动补 ``I:`` 前缀的有 SPX、NDX、RUT、DJX、VIX。
        长度大于 10 的代码按期权补 ``O:``。请求失败时返回空列表。
        """
        if not self.inited:
            ok: bool = self.init(output)
            if not ok:
                return []

        interval: Interval | None = req.interval
        if interval is None or interval not in INTERVAL_VT2MASSIVE:
            label: str = interval.value if interval is not None else "None"
            output(f"MassiveDatafeed 查询K线数据失败：不支持的时间周期{label}")
            return []
        timespan: str = INTERVAL_VT2MASSIVE[interval]

        start: datetime = req.start
        end: datetime = req.end if req.end is not None else datetime.now()
        ticker: str = _to_massive_ticker(req.symbol)
        path: str = (
            f"/v2/aggs/ticker/{ticker}/range/1/{timespan}/"
            f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        )
        params: dict[str, str] = {"limit": str(self.limit), "sort": "asc"}

        try:
            results: list[dict[str, Any]] = self._get_all_pages(path, params, output)
        except (requests.RequestException, RuntimeError) as exc:
            output(f"MassiveDatafeed 查询K线数据失败：{exc}")
            return []

        bars: list[BarData] = []
        row: dict[str, Any]
        for row in results:
            dt: datetime = datetime.fromtimestamp(_number(row, "t") / 1000)
            if not (start <= dt <= end):
                continue

            volume: float = _number(row, "v")
            bar: BarData = BarData(
                symbol=req.symbol,
                exchange=req.exchange,
                datetime=dt.replace(tzinfo=DB_TZ),
                interval=interval,
                volume=volume,
                turnover=_number(row, "vw") * volume,
                open_price=_number(row, "o"),
                high_price=_number(row, "h"),
                low_price=_number(row, "l"),
                close_price=_number(row, "c"),
                gateway_name="MASSIVE",
            )
            bars.append(bar)

        return bars

    def query_tick_history(
        self,
        req: HistoryRequest,
        output: Callable[[str], Any] = print,
    ) -> list[TickData]:
        """查询 Tick 数据。该接口固定返回空列表。"""
        return []
