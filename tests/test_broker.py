"""TossBroker 단위 테스트 — httpx 모킹, 네트워크 0.

검증: 발주 본문(quantity/price/orderAmount/timeInForce)·orderId 파싱·record_order, sleeve 하드캡,
매도 보유수량 가드, 시장가 금액매수 vs 수량매수, 에러 분류(422/401/5xx/connect), 멱등(중복 coid·409),
취소, available_cash=ledger, holdings(items) 파싱, place_oco.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from toss_sleeve.api.auth import TossAuth
from toss_sleeve.broker import TossBroker
from toss_sleeve.config import TossConfig
from toss_sleeve.ledger import InMemorySleeveLedger
from toss_sleeve.types import (
    Currency,
    ExitPlan,
    Fill,
    OrderRequest,
    OrderSide,
    OrderType,
    Ticker,
)

TICKER = Ticker("005930")
KRW = Currency.KRW
D = Decimal


def _cfg() -> TossConfig:
    return TossConfig(
        client_id="cid", client_secret="sec", sleeve_id="gold-digger",
        allocated_capital=10_000_000, account_seq="7",
    )


def _resp(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, json=body)


def _ok_order(order_id: str = "O1", coid: str = "") -> httpx.Response:
    return _resp(200, {"result": {"orderId": order_id, "clientOrderId": coid}})


class _FakeClient:
    def __init__(
        self, posts: list[object] | None = None, get_default: httpx.Response | None = None
    ) -> None:
        self._posts = list(posts or [])
        self._get_default = get_default
        self.posts: list[SimpleNamespace] = []
        self.gets: list[SimpleNamespace] = []

    async def post(self, path, json=None, data=None, headers=None, params=None):  # noqa: ANN001
        self.posts.append(SimpleNamespace(path=path, json=json, data=data, headers=headers))
        if not self._posts:
            raise AssertionError(f"예상보다 많은 POST: {path}")
        item = self._posts.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def get(self, path, params=None, headers=None):  # noqa: ANN001
        self.gets.append(SimpleNamespace(path=path, params=params, headers=headers))
        if self._get_default is None:
            raise AssertionError(f"예상치 못한 GET: {path}")
        return self._get_default

    async def aclose(self) -> None:
        pass


class _FakeAuth:
    async def auth_headers(self) -> dict[str, str]:
        return {"authorization": "Bearer t"}


def _broker(
    posts: list[object], *, allocated: int = 10_000_000, get_default: httpx.Response | None = None
) -> tuple[TossBroker, _FakeClient, InMemorySleeveLedger]:
    client = _FakeClient(posts, get_default=get_default)
    ledger = InMemorySleeveLedger(sleeve_id="gold-digger", allocated_capital=allocated)
    broker = TossBroker(_cfg(), _FakeAuth(), ledger=ledger, client=client)  # type: ignore[arg-type]
    return broker, client, ledger


def _buy(qty: int = 3, price: int = 70_000, coid: str = "gd:1") -> OrderRequest:
    return OrderRequest(
        ticker=TICKER, side=OrderSide.BUY, order_type=OrderType.LIMIT, currency=KRW,
        quantity=D(qty), price=D(price), client_order_id=coid,
    )


def _sell(qty: int, price: int = 80_000, coid: str = "gd:s") -> OrderRequest:
    return OrderRequest(
        ticker=TICKER, side=OrderSide.SELL, order_type=OrderType.LIMIT, currency=KRW,
        quantity=D(qty), price=D(price), client_order_id=coid,
    )


def _seed_position(ledger: InMemorySleeveLedger, qty: int) -> None:
    ledger.apply_fill(Fill(
        order_id="seed", client_order_id="seed", symbol=str(TICKER), side=OrderSide.BUY,
        currency=KRW, filled_quantity=D(qty), avg_price=D(60_000), commission=D(0), tax=D(0),
        filled_at="t", settlement_date="2026-06-20",
    ))


# --- TossAuth -------------------------------------------------------------


async def test_token_issues_and_caches(tmp_path: Path) -> None:
    client = _FakeClient([_resp(200, {"access_token": "TK", "expires_in": 86400})])
    auth = TossAuth(_cfg(), client=client, cache_path=tmp_path / "tok.json")  # type: ignore[arg-type]
    assert await auth.token() == "TK"
    assert await auth.token() == "TK"
    assert len(client.posts) == 1
    assert client.posts[0].data["grant_type"] == "client_credentials"


async def test_token_httpx_error_wrapped_as_transport(tmp_path: Path) -> None:
    from toss_sleeve.api.constants import TossTransportError

    class _BoomClient:
        async def post(self, *a, **k):  # noqa: ANN002, ANN003
            raise httpx.ConnectTimeout("boom")

        async def aclose(self) -> None:
            pass

    auth = TossAuth(_cfg(), client=_BoomClient(), cache_path=tmp_path / "t.json")  # type: ignore[arg-type]
    with pytest.raises(TossTransportError):  # 모든 REST 가 auth 경유 → 토큰 전송 실패 누수 금지.
        await auth.token()


# --- place BUY ------------------------------------------------------------


async def test_place_buy_builds_body_reserves_records() -> None:
    broker, client, ledger = _broker([_ok_order("E1", "gd:1")])
    ack = await broker.place(_buy(qty=3, price=70_000, coid="gd:1"))
    assert ack.accepted and ack.order_id == "E1"
    call = client.posts[0]
    assert call.json["symbol"] == "005930"
    assert call.json["side"] == "BUY"
    assert call.json["orderType"] == "LIMIT"
    assert call.json["quantity"] == "3"
    assert call.json["price"] == "70000"
    assert call.json["timeInForce"] == "DAY"
    assert call.json["clientOrderId"] == "gd:1"
    assert call.headers["X-Tossinvest-Account"] == "7"
    assert ledger.available(KRW) == D(10_000_000 - 210_000)
    assert ledger._orders["gd:1"]["order_id"] == "E1"


async def test_hard_cap_rejects_without_placing() -> None:
    broker, client, ledger = _broker([], allocated=100_000)
    ack = await broker.place(_buy(qty=3, price=70_000, coid="gd:1"))
    assert not ack.accepted and "하드캡" in ack.message
    assert len(client.posts) == 0
    assert ledger.available(KRW) == D(100_000)


async def test_buy_without_client_order_id_rejected() -> None:
    broker, client, _ = _broker([])
    ack = await broker.place(_buy(coid=""))
    assert not ack.accepted and "client_order_id" in ack.message
    assert len(client.posts) == 0


async def test_market_amount_buy_reserves() -> None:
    broker, client, ledger = _broker([_ok_order("M1", "gd:m")])
    req = OrderRequest(
        ticker=TICKER, side=OrderSide.BUY, order_type=OrderType.MARKET, currency=KRW,
        order_amount=D(100_000), client_order_id="gd:m",
    )
    ack = await broker.place(req)
    assert ack.accepted
    assert client.posts[0].json["orderAmount"] == "100000"
    assert "price" not in client.posts[0].json
    assert ledger.available(KRW) == D(10_000_000 - 100_000)


async def test_market_quantity_buy_rejected_no_cap() -> None:
    broker, client, _ = _broker([])
    req = OrderRequest(
        ticker=TICKER, side=OrderSide.BUY, order_type=OrderType.MARKET, currency=KRW,
        quantity=D(3), client_order_id="gd:mq",
    )
    ack = await broker.place(req)
    assert not ack.accepted and "order_amount" in ack.message
    assert len(client.posts) == 0


# --- place SELL guard -----------------------------------------------------


async def test_sell_within_holdings_places() -> None:
    broker, client, ledger = _broker([_ok_order("S1", "gd:s")])
    _seed_position(ledger, qty=10)
    ack = await broker.place(_sell(qty=5))
    assert ack.accepted and client.posts[0].json["side"] == "SELL"


async def test_sell_over_holdings_rejected() -> None:
    broker, client, ledger = _broker([])
    _seed_position(ledger, qty=10)
    ack = await broker.place(_sell(qty=20))
    assert not ack.accepted and "보유수량 초과" in ack.message
    assert len(client.posts) == 0  # 발주 안 됨.


async def test_sell_with_no_holdings_rejected() -> None:
    broker, client, _ = _broker([])
    ack = await broker.place(_sell(qty=1))
    assert not ack.accepted and "보유수량 초과" in ack.message
    assert len(client.posts) == 0


async def test_pending_sell_blocks_second_sell() -> None:
    broker, client, ledger = _broker([_ok_order("S1", "s1")])
    _seed_position(ledger, qty=10)
    a1 = await broker.place(_sell(qty=6, coid="s1"))
    assert a1.accepted
    a2 = await broker.place(_sell(qty=6, coid="s2"))  # 미체결 s1 때문에 잔여 4 < 6.
    assert not a2.accepted and "보유수량 초과" in a2.message
    assert len(client.posts) == 1  # 둘째는 발주 안 됨.


async def test_sell_without_client_order_id_rejected() -> None:
    broker, client, ledger = _broker([])
    _seed_position(ledger, qty=10)
    ack = await broker.place(OrderRequest(
        ticker=TICKER, side=OrderSide.SELL, order_type=OrderType.LIMIT, currency=KRW,
        quantity=D(5), price=D(80_000), client_order_id="",
    ))
    assert not ack.accepted and "client_order_id" in ack.message
    assert len(client.posts) == 0  # 기록 불가한 매도는 발주 안 함(가드 우회 차단).


async def test_buy_reservation_released_on_presend_failure() -> None:
    from toss_sleeve.api.constants import TossTransportError

    class _FailAuth:
        async def auth_headers(self) -> dict[str, str]:
            raise httpx.ReadTimeout("token timeout")  # 발주 POST 이전 전송 실패.

    client = _FakeClient([])
    ledger = InMemorySleeveLedger(sleeve_id="gold-digger", allocated_capital=10_000_000)
    broker = TossBroker(_cfg(), _FailAuth(), ledger=ledger, client=client)  # type: ignore[arg-type]
    with pytest.raises(TossTransportError):
        await broker.place(_buy(qty=3, price=70_000, coid="gd:1"))
    assert ledger.available(KRW) == D(10_000_000)  # 예약 환원(미발송 확실).
    assert len(client.posts) == 0


async def test_concurrent_sell_blocked_by_presend_record() -> None:
    import asyncio

    started = asyncio.Event()
    gate = asyncio.Event()

    class _SlowClient:
        def __init__(self) -> None:
            self.posts: list[object] = []

        async def post(self, path, json=None, data=None, headers=None, params=None):  # noqa: ANN001
            self.posts.append(json)
            started.set()  # presend 기록은 이미 끝난 시점.
            await gate.wait()  # 첫 SELL 의 POST 를 await 에 묶어둠.
            return _ok_order("S1", "s1")

        async def get(self, *a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("예상치 못한 GET")

        async def aclose(self) -> None:
            pass

    client = _SlowClient()
    ledger = InMemorySleeveLedger(sleeve_id="gold-digger", allocated_capital=10_000_000)
    _seed_position(ledger, qty=10)
    broker = TossBroker(_cfg(), _FakeAuth(), ledger=ledger, client=client)  # type: ignore[arg-type]

    t1 = asyncio.create_task(broker.place(_sell(qty=6, coid="s1")))
    await started.wait()  # 첫 SELL 이 POST(await) 진입 — 선등록 완료 보장.
    # 첫 SELL 이 아직 미체결(await 중)이라도 둘째 SELL 은 선등록 덕에 가드에서 막혀야 함.
    a2 = await broker.place(_sell(qty=6, coid="s2"))
    assert not a2.accepted and "보유수량 초과" in a2.message
    gate.set()
    a1 = await t1
    assert a1.accepted


async def test_sell_business_reject_clears_presend() -> None:
    broker, client, ledger = _broker([_resp(422, {"error": {"code": "halted"}})])
    _seed_position(ledger, qty=10)
    ack = await broker.place(_sell(qty=6, coid="s1"))
    assert not ack.accepted
    assert ledger.available_to_sell("005930") == D(10)  # 거부된 매도는 가드에서 제거(영구 차단 안 됨).


async def test_pending_sell_cleared_after_fill() -> None:
    broker, client, ledger = _broker([_ok_order("S1", "s1"), _ok_order("S2", "s2")])
    _seed_position(ledger, qty=10)
    await broker.place(_sell(qty=6, coid="s1"))
    ledger.apply_fill(Fill(
        order_id="S1", client_order_id="s1", symbol=str(TICKER), side=OrderSide.SELL,
        currency=KRW, filled_quantity=D(6), avg_price=D(80_000), commission=D(0), tax=D(0),
        filled_at="t", settlement_date="2026-06-20",
    ))
    a2 = await broker.place(_sell(qty=4, coid="s2"))  # s1 체결로 pending 해소 → 남은 4 매도 허용.
    assert a2.accepted


# --- error classification -------------------------------------------------


async def test_business_rejection_releases() -> None:
    broker, _, ledger = _broker([_resp(422, {"error": {"code": "invalid-request"}})])
    ack = await broker.place(_buy(qty=2, price=50_000, coid="gd:2"))
    assert not ack.accepted
    assert ledger.available(KRW) == D(10_000_000)


async def test_auth_error_propagates_and_releases() -> None:
    from toss_sleeve.api.constants import TossError, TossTransportError

    broker, _, ledger = _broker([_resp(401, {"error": {"code": "unauthorized"}})])
    with pytest.raises(TossError) as exc:
        await broker.place(_buy(qty=2, price=50_000, coid="gd:3"))
    assert not isinstance(exc.value, TossTransportError)
    assert ledger.available(KRW) == D(10_000_000)


async def test_server_error_keeps_reservation() -> None:
    from toss_sleeve.api.constants import TossAmbiguousOrderError

    broker, _, ledger = _broker([_resp(503, {"error": {"code": "gw"}})])
    with pytest.raises(TossAmbiguousOrderError):
        await broker.place(_buy(qty=2, price=50_000, coid="gd:4"))
    assert ledger.available(KRW) == D(10_000_000 - 100_000)  # 모호 → 예약 유지.


async def test_connect_error_retries_then_releases() -> None:
    from toss_sleeve.api.constants import TossAmbiguousOrderError, TossTransportError

    broker, client, ledger = _broker(
        [httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.ConnectError("x")]
    )
    with pytest.raises(TossTransportError) as exc:
        await broker.place(_buy(qty=1, price=50_000, coid="gd:5"))
    assert not isinstance(exc.value, TossAmbiguousOrderError)
    assert len(client.posts) == 3
    assert ledger.available(KRW) == D(10_000_000)  # 미발송 → 환원.


async def test_idempotent_same_client_order_id() -> None:
    broker, client, _ = _broker([_ok_order("E1", "gd:6")])
    req = _buy(qty=1, price=50_000, coid="gd:6")
    a1 = await broker.place(req)
    a2 = await broker.place(req)
    assert a1 == a2 and len(client.posts) == 1


# --- cancel / reads -------------------------------------------------------


async def test_cancel_success_and_business_false() -> None:
    broker, _, _ = _broker([_resp(200, {"result": {}})])
    assert await broker.cancel("O1") is True
    broker2, _, _ = _broker([_resp(422, {"error": {"code": "already-filled"}})])
    assert await broker2.cancel("O1") is False


async def test_cancel_releases_reservation() -> None:
    broker, _, ledger = _broker([_ok_order("E1", "gd:1"), _resp(200, {"result": {}})])
    await broker.place(_buy(qty=3, price=70_000, coid="gd:1"))
    assert ledger.available(KRW) == D(10_000_000 - 210_000)  # 예약됨.
    assert await broker.cancel("E1") is True
    assert ledger.available(KRW) == D(10_000_000)  # 취소 → 예약 해제(재시작 후에도 역조회).


async def test_denylist_blocks_buy() -> None:
    cfg = TossConfig(
        client_id="cid", client_secret="sec", sleeve_id="gold-digger",
        allocated_capital=10_000_000, account_seq="7", denylist=frozenset({"005930"}),
    )
    client = _FakeClient([])
    ledger = InMemorySleeveLedger(sleeve_id="gold-digger", allocated_capital=10_000_000)
    broker = TossBroker(cfg, _FakeAuth(), ledger=ledger, client=client)  # type: ignore[arg-type]
    ack = await broker.place(_buy(coid="gd:1"))  # ticker 005930 은 denylist.
    assert not ack.accepted and "denylist" in ack.message
    assert len(client.posts) == 0
    assert ledger.available(KRW) == D(10_000_000)  # 예약도 안 함.


async def test_uncached_409_raises_ambiguous() -> None:
    from toss_sleeve.api.constants import TossAmbiguousOrderError

    broker, _, ledger = _broker([_resp(409, {"error": {"code": "request-in-progress"}})])
    with pytest.raises(TossAmbiguousOrderError):
        await broker.place(_buy(qty=2, price=50_000, coid="gd:9"))
    assert ledger.available(KRW) == D(10_000_000 - 100_000)  # 예약 유지(라이브일 수 있음 → reconcile).


async def test_available_cash_is_ledger() -> None:
    broker, _, ledger = _broker([])
    assert await broker.available_cash(KRW) == ledger.available(KRW)


async def test_positions_parses_holdings_items() -> None:
    holdings = _resp(200, {"result": {"marketValue": "x", "items": [
        {"symbol": "ETHU", "quantity": "110", "averagePurchasePrice": "25.629803",
         "currency": "USD"},
    ]}})
    broker, _, _ = _broker([], get_default=holdings)
    pos = await broker.positions()
    assert len(pos) == 1
    assert pos[0].ticker == "ETHU" and pos[0].quantity == D(110)
    assert pos[0].avg_price == D("25.629803") and pos[0].currency == Currency.USD


async def test_place_oco_entry_only() -> None:
    broker, client, _ = _broker([_ok_order("ENTRY", "gd:7")])
    plan = ExitPlan(stop=D(68_000), take_profit=(D(72_000),), ratios=(1.0,))
    ack = await broker.place_oco(_buy(qty=10, price=70_000, coid="gd:7"), plan)
    assert ack.accepted and len(client.posts) == 1


# --- OrderRequest 경계검증(G5-I) ---------------------------------------------


def test_order_request_rejects_nonpositive_price() -> None:
    with pytest.raises(ValueError, match="price"):
        OrderRequest(
            ticker=TICKER, side=OrderSide.BUY, order_type=OrderType.LIMIT, currency=KRW,
            quantity=D(1), price=D(0), client_order_id="x",
        )


def test_order_request_rejects_nonfinite_quantity() -> None:
    with pytest.raises(ValueError):
        OrderRequest(
            ticker=TICKER, side=OrderSide.BUY, order_type=OrderType.LIMIT, currency=KRW,
            quantity=D("NaN"), price=D(100), client_order_id="x",
        )


def test_order_request_rejects_empty_ticker() -> None:
    with pytest.raises(ValueError, match="ticker"):
        OrderRequest(
            ticker=Ticker(""), side=OrderSide.BUY, order_type=OrderType.LIMIT, currency=KRW,
            quantity=D(1), price=D(100), client_order_id="x",
        )
