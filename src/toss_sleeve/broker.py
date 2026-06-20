"""TossBroker — Toss Open API 발주/취소 + 읽기 위임. 공유계좌 sleeve.

공유계좌 불변식(TOSS_SHARED_ACCOUNT_PROTOCOL.md):
  - available_cash(currency) ← *SleeveLedger.available()* (계좌 전체 buying-power 아님; 불변식 2·3).
  - place(BUY) 직전 ledger.reserve(client_order_id, amount, currency) 로 하드캡 강제 + 귀속.
  - place(SELL) 직전 *보유수량 가드* — sleeve 원장 순보유 수량 초과 매도 거부(불변식: 타 sleeve·개인
    보유분 잠식 방지). 브로커는 sleeve 모르므로 패키지가 막지 않으면 풀에서 남의 주식이 팔린다.
  - positions() 는 toss holdings(계좌 전체)라 reconcile 경로 전용 — 매도 결정은 ledger 가 진실.

place_oco 는 *진입만 발주, exit_plan 은 기록만* — 청산 운전은 소비자(엔진). 멱등: client_order_id 를
토스 clientOrderId 로 전달, 409 충돌은 최초 ack 로 흡수. 에러 분류는 HTTP status 기반.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx

from toss_sleeve.api.auth import TossAuth
from toss_sleeve.api.constants import (
    ORDERS_PATH,
    TossAmbiguousOrderError,
    TossError,
    TossTransportError,
    cancel_path,
    is_business_rejection,
    parse_toss,
)
from toss_sleeve.api.rest import TossRest
from toss_sleeve.config import TossConfig
from toss_sleeve.ledger import SleeveLedger
from toss_sleeve.ratelimit import RateLimiter
from toss_sleeve.types import (
    BrokerPosition,
    Currency,
    ExitPlan,
    OrderAck,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)

_SIDE = {OrderSide.BUY: "BUY", OrderSide.SELL: "SELL"}
_ORDER_TYPE = {OrderType.LIMIT: "LIMIT", OrderType.MARKET: "MARKET"}

# 요청 전 *미발송 확실* httpx 예외 — 재발송해도 중복 발주 불가라 멱등 재시도 안전.
_CONNECT_PHASE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _reject(req: OrderRequest, message: str) -> OrderAck:
    return OrderAck(
        accepted=False, order_id="", client_order_id=req.client_order_id, message=message
    )


class TossBroker:
    """Broker 의 Toss 구현 — 발주/취소 + 읽기 위임(TossRest)."""

    def __init__(
        self,
        config: TossConfig,
        auth: TossAuth,
        *,
        ledger: SleeveLedger,
        client: httpx.AsyncClient | None = None,
        ratelimiter: RateLimiter | None = None,
        rest: TossRest | None = None,
        transport_retries: int = 2,
    ) -> None:
        self._config = config
        self._auth = auth
        self._ledger = ledger
        self._client = client
        self._owns_client = client is None
        # ORDER 그룹 보수적 한도(개장피크 3/s).
        self._ratelimiter = ratelimiter or RateLimiter(max_calls=3, period=1.0)
        self._rest = rest or TossRest(config, auth, client=client, ratelimiter=self._ratelimiter)
        self._transport_retries = max(0, transport_retries)
        self._acks: dict[str, OrderAck] = {}
        self._oco_log: list[tuple[str, ExitPlan]] = []

    def __repr__(self) -> str:
        return f"TossBroker(sleeve={self._ledger.sleeve_id!r})"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._config.base_url)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._rest.close()

    async def __aenter__(self) -> TossBroker:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- low-level write ----------------------------------------------------

    async def _send(
        self,
        make_request: Callable[[], Awaitable[httpx.Response]],
        *,
        ambiguous: bool,
    ) -> httpx.Response:
        """발주/취소 POST 전송. 연결 수립 실패(미발송 확실)는 멱등 재시도, 전송 후 실패는 즉시 표면화
        (발주면 TossAmbiguousOrderError 로 좁힘). 매 시도가 레이트리밋 슬롯을 소비한다."""
        last_exc: BaseException | None = None
        for _ in range(self._transport_retries + 1):
            await self._ratelimiter.acquire()
            try:
                return await make_request()
            except _CONNECT_PHASE_ERRORS as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                cls = TossAmbiguousOrderError if ambiguous else TossTransportError
                raise cls("Toss 발주/취소 전송 실패") from exc
        raise TossTransportError(
            f"Toss 연결 실패 — 멱등 재시도 {self._transport_retries}회 소진"
        ) from last_exc

    async def _post(self, path: str, body: dict[str, object], *, ambiguous: bool) -> object:
        headers = await self._auth.auth_headers()
        headers["X-Tossinvest-Account"] = await self._rest.account_seq()
        client = self._get_client()
        resp = await self._send(
            lambda: client.post(path, json=body, headers=headers), ambiguous=ambiguous
        )
        return parse_toss(resp, ambiguous=ambiguous)

    # --- Broker protocol ----------------------------------------------------

    async def place(self, req: OrderRequest) -> OrderAck:
        """단건 주문. client_order_id 가 있으면 멱등.

        BUY: 발주 직전 sleeve 하드캡(ledger.reserve) — 가상현금 초과면 발주조차 안 함. LIMIT 은
        price×qty, MARKET 금액매수는 order_amount 로 예약(수량기반 MARKET BUY 는 캡 불가→거부).
        SELL: sleeve 보유수량 초과면 거부(공유계좌 잠식 방지). 업무거부(400/422)→accepted=False,
        인증/설정(401/403)·전송장애는 전파.
        """
        if req.client_order_id and req.client_order_id in self._acks:
            return self._acks[req.client_order_id]

        reserved = False
        sell_recorded = False
        if req.side is OrderSide.BUY:
            if str(req.ticker) in self._config.denylist:
                # 개인 보유분 보호 — 봇이 denylist 종목을 매수후보로 고르는 것 차단(발주·예약 안 함).
                return _reject(req, f"denylist 종목 매수 차단(개인 보유분 보호): {req.ticker}")
            if not req.client_order_id:
                return _reject(req, "BUY 에는 client_order_id 필요(귀속·하드캡)")
            amount = self._reserve_amount(req)
            if amount is None:
                return _reject(req, "수량기반 MARKET BUY 는 하드캡 불가 — order_amount 사용")
            if not self._ledger.reserve(req.client_order_id, amount, req.currency):
                return _reject(req, "sleeve 하드캡: 가상현금 부족")
            reserved = True
        else:  # SELL — 매도가능 가드 + 미체결 선등록을 원자적으로(멀티워커 초과매도 방지).
            if not req.client_order_id:
                # coid 없으면 record_order 가 안 돼 미체결 매도가 가드에서 빠진다 → 초과매도 방지 위해 거부.
                return _reject(req, "SELL 에는 client_order_id 필요(미체결 매도 가드·귀속)")
            if req.quantity is None:
                return _reject(req, "SELL 에는 quantity 필요")
            # 체크+선등록을 한 ledger 트랜잭션으로 — POST(await) 이전·크로스프로세스 모두 초과매도 차단.
            if not self._ledger.reserve_sell(
                client_order_id=req.client_order_id, order_id="", symbol=str(req.ticker),
                currency=req.currency, quantity=req.quantity,
            ):
                return _reject(req, f"sleeve 보유수량 초과: 매도 {req.quantity} > 매도가능")
            sell_recorded = True

        body = self._build_body(req)
        try:
            result = await self._post(ORDERS_PATH, body, ambiguous=True)
        except TossError as exc:
            return self._handle_order_error(exc, req, reserved=reserved, sell_recorded=sell_recorded)
        except httpx.HTTPError as exc:
            # 발주 POST 이전(인증헤더·account_seq 조회) 전송 실패 — 주문 미발송 확실 → 정리 후 전파.
            self._unwind(req, reserved=reserved, sell_recorded=sell_recorded)
            raise TossTransportError("발주 전 인증/계좌조회 전송 실패") from exc

        order_id = str(result.get("orderId", "")).strip() if isinstance(result, dict) else ""
        if not order_id:
            self._unwind(req, reserved=reserved, sell_recorded=sell_recorded)
            raise TossError(f"주문 응답에 orderId 없음: {req.ticker!r}")

        if req.client_order_id:
            self._ledger.record_order(
                client_order_id=req.client_order_id,
                order_id=order_id,
                symbol=str(req.ticker),
                side=req.side,
                currency=req.currency,
                quantity=req.quantity or Decimal(0),
            )
        ack = OrderAck(accepted=True, order_id=order_id, client_order_id=req.client_order_id)
        if req.client_order_id:
            self._acks[req.client_order_id] = ack
        return ack

    async def place_oco(self, entry: OrderRequest, exit_plan: ExitPlan) -> OrderAck:
        """진입 주문만 발주하고 exit_plan 은 기록만(청산은 소비자 엔진)."""
        ack = await self.place(entry)
        if ack.accepted:
            self._oco_log.append((ack.order_id, exit_plan))
        return ack

    async def cancel(self, order_id: str) -> bool:
        """주문 취소. 취소 불성립(업무거부)은 False, 인증/설정·전송장애는 전파."""
        try:
            await self._post(cancel_path(order_id), {}, ambiguous=False)
        except TossError as exc:
            if is_business_rejection(exc):
                return False
            raise
        coid = self._ledger.client_order_id_for(order_id)
        if coid:
            self._ledger.mark_order_terminal(coid, OrderStatus.CANCELLED)
        return True

    async def positions(self) -> list[BrokerPosition]:
        """보유 포지션 — TossRest.holdings 위임(계좌 전체; reconcile 경로 전용, 매도 결정은 ledger)."""
        return await self._rest.holdings()

    async def available_cash(self, currency: Currency = Currency.KRW) -> Decimal:
        """통화별 주문가능현금 = **sleeve 가상현금**(SleeveLedger). 계좌 buying-power 아님(불변식 2·3)."""
        return self._ledger.available(currency)

    # --- 내부 ----------------------------------------------------------------

    def _reserve_amount(self, req: OrderRequest) -> Decimal | None:
        """BUY 예약 금액(req.currency). LIMIT=price×qty, MARKET 금액매수=order_amount,
        수량기반 MARKET=하드캡 불가(None)."""
        if req.order_type is OrderType.LIMIT and req.price is not None and req.quantity is not None:
            return req.price * req.quantity
        if req.order_amount is not None:
            return req.order_amount
        return None

    def _build_body(self, req: OrderRequest) -> dict[str, object]:
        body: dict[str, object] = {
            "symbol": str(req.ticker),
            "side": _SIDE[req.side],
            "orderType": _ORDER_TYPE[req.order_type],
            "timeInForce": req.time_in_force.value,
        }
        if req.quantity is not None:
            body["quantity"] = str(req.quantity)
        if req.order_amount is not None:
            body["orderAmount"] = str(req.order_amount)
        if req.order_type is OrderType.LIMIT and req.price is not None:
            body["price"] = str(req.price)
        if req.client_order_id:
            body["clientOrderId"] = req.client_order_id
        return body

    def _handle_order_error(
        self, exc: TossError, req: OrderRequest, *, reserved: bool, sell_recorded: bool
    ) -> OrderAck:
        """발주 예외 분류 — 업무거부/멱등충돌은 ack, 그 외는 예약·선등록 정리 후 전파."""
        if exc.status == httpx.codes.CONFLICT:
            if req.client_order_id and req.client_order_id in self._acks:
                return self._acks[req.client_order_id]
            # orderId 미보유 409(예: 재시작으로 _acks 유실 후 재시도) — 접수됐을 수 있어 예약·선등록을
            # 유지하고, 평범한 거부가 아니라 *모호*로 표면화한다. 거부로 돌리면 소비자가 라이브 주문을
            # 추적 중단하고 예약이 영구 잠긴다 → clientOrderId 로 reconcile 하도록 신호.
            raise TossAmbiguousOrderError(
                "중복 주문(409) — 접수 여부 불확정, clientOrderId 로 reconcile 필요",
                status=exc.status, code=exc.code, msg=exc.msg, request_id=exc.request_id,
            )
        if is_business_rejection(exc):
            self._unwind(req, reserved=reserved, sell_recorded=sell_recorded)  # 미발주 확실 → 정리.
            return _reject(req, str(exc))
        # 모호 착지(전송 후 실패)는 접수됐을 수 있어 유지. 연결 실패·인증/설정은 정리 후 전파.
        if not isinstance(exc, TossAmbiguousOrderError):
            self._unwind(req, reserved=reserved, sell_recorded=sell_recorded)
        raise exc

    def _unwind(self, req: OrderRequest, *, reserved: bool, sell_recorded: bool) -> None:
        """미발주 확실 시 정리 — BUY 예약 환원, SELL 선등록은 REJECTED 로 가드에서 제거."""
        if not req.client_order_id:
            return
        if reserved:
            self._ledger.release(req.client_order_id)
        if sell_recorded:
            self._ledger.mark_order_terminal(req.client_order_id, OrderStatus.REJECTED)
