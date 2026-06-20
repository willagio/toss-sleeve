"""시세 조회 파싱 — prices(배치)·orderbook(단건). httpx 모킹.

라이브 폴링 피드(웹소켓 없음)의 시세 소스. /prices 는 symbols 콤마구분 배치(한 콜 다종목),
/orderbook 은 종목당 1콜.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from toss_sleeve.api.constants import ORDERBOOK_PATH, PRICES_PATH, TossError
from toss_sleeve.api.rest import TossRest
from toss_sleeve.config import TossConfig
from toss_sleeve.types import Currency

D = Decimal


def _cfg() -> TossConfig:
    return TossConfig(
        client_id="c", client_secret="s", sleeve_id="gd",
        allocated_capital=1_000_000, account_seq="7",
    )


class _FakeAuth:
    async def auth_headers(self) -> dict[str, str]:
        return {"authorization": "Bearer t"}


class _FakeClient:
    def __init__(self, body: object) -> None:
        self._body = body
        self.gets: list[SimpleNamespace] = []

    async def get(self, path, params=None, headers=None):  # noqa: ANN001
        self.gets.append(SimpleNamespace(path=path, params=params, headers=headers))
        return httpx.Response(200, json=self._body)

    async def aclose(self) -> None:
        pass


def _rest(body: object) -> tuple[TossRest, _FakeClient]:
    client = _FakeClient(body)
    rest = TossRest(_cfg(), _FakeAuth(), client=client)  # type: ignore[arg-type]
    return rest, client


async def test_prices_batch_parses_and_no_account_header() -> None:
    body = {
        "result": {
            "items": [
                {"symbol": "005930", "lastPrice": "71000", "currency": "KRW", "timestamp": "t1"},
                {"symbol": "AAPL", "lastPrice": "212.34", "currency": "USD", "timestamp": "t2"},
            ]
        }
    }
    rest, client = _rest(body)
    quotes = await rest.prices(["005930", "AAPL"])
    assert len(quotes) == 2
    assert quotes[0].symbol == "005930" and quotes[0].last_price == D("71000")
    assert quotes[1].currency == Currency.USD and quotes[1].last_price == D("212.34")
    # 배치: 한 콜, symbols 콤마구분. MARKET_DATA 라 계좌 헤더 없음.
    assert client.gets[0].path == PRICES_PATH
    assert client.gets[0].params == {"symbols": "005930,AAPL"}
    assert "X-Tossinvest-Account" not in client.gets[0].headers


async def test_prices_empty_symbols_no_call() -> None:
    rest, client = _rest({"result": {"items": []}})
    assert await rest.prices([]) == []
    assert len(client.gets) == 0


async def test_prices_chunks_over_200() -> None:
    body = {"result": {"items": [
        {"symbol": "S", "lastPrice": "1", "currency": "KRW", "timestamp": "t"},
    ]}}
    rest, client = _rest(body)
    quotes = await rest.prices([f"S{i}" for i in range(201)])  # 201 → 200 + 1.
    assert len(client.gets) == 2
    assert len(client.gets[0].params["symbols"].split(",")) == 200
    assert len(client.gets[1].params["symbols"].split(",")) == 1
    assert len(quotes) == 2  # 청크당 1 item.


async def test_orderbook_parses_levels() -> None:
    body = {
        "result": {
            "bids": [{"price": "70900", "volume": "10"}, {"price": "70800", "volume": "5"}],
            "asks": [{"price": "71000", "volume": "8"}],
            "currency": "KRW",
            "timestamp": "t",
        }
    }
    rest, client = _rest(body)
    ob = await rest.orderbook("005930")
    assert ob.best_bid() == D("70900") and ob.best_ask() == D("71000")
    assert len(ob.bids) == 2 and ob.asks[0].volume == D("8")
    assert client.gets[0].path == ORDERBOOK_PATH
    assert client.gets[0].params == {"symbol": "005930"}


# --- order_fill 체결값 엄격 파싱(G3-E·F) -------------------------------------


async def test_order_fill_settlement_null_not_held() -> None:
    body = {"result": {
        "side": "SELL", "symbol": "005930", "clientOrderId": "c1", "currency": "KRW",
        "execution": {
            "filledQuantity": "5", "averageFilledPrice": "80000", "commission": "60",
            "tax": "800", "filledAt": "t", "settlementDate": None,
        },
    }}
    rest, _ = _rest(body)
    fill = await rest.order_fill("o1")
    assert fill is not None
    assert fill.settlement_date == ""  # null → "" (리터럴 "None" 아님)
    assert fill.client_order_id == "c1"


async def test_order_fill_refuses_missing_avg_price() -> None:
    body = {"result": {"side": "BUY", "symbol": "005930",
                       "execution": {"filledQuantity": "5"}}}  # averageFilledPrice 누락
    rest, _ = _rest(body)
    with pytest.raises(TossError):
        await rest.order_fill("o1")


async def test_order_fill_none_when_no_progress() -> None:
    body = {"result": {"side": "BUY", "execution": {"filledQuantity": "0"}}}
    rest, _ = _rest(body)
    assert await rest.order_fill("o1") is None


async def test_read_httpx_error_wrapped_as_transport() -> None:
    from toss_sleeve.api.constants import TossTransportError

    class _BoomClient:
        async def get(self, *a, **k):  # noqa: ANN002, ANN003
            raise httpx.ConnectTimeout("boom")

        async def aclose(self) -> None:
            pass

    rest = TossRest(_cfg(), _FakeAuth(), client=_BoomClient())  # type: ignore[arg-type]
    with pytest.raises(TossTransportError):  # raw httpx 누수 금지(폴링 루프 보호).
        await rest.holdings()
