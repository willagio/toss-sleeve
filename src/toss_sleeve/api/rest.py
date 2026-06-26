"""Toss 읽기 REST — 계좌·보유·주문가능현금·주문상세.

TossAuth(토큰)와 RateLimiter 위의 읽기 게이트웨이. 쓰기(주문/취소)는 broker.py. 계좌범위 호출은
X-Tossinvest-Account: {accountSeq} 헤더 필요 — config 미주입 시 accounts() 로 조회·캐시.

holdings()/order_fill() 은 BrokerPosition/Fill 로 정규화한다(금액 Decimal, 통화 태그). 단 *원장이
진실*이라 소비자 엔진은 holdings 수량/평단을 권위로 쓰지 않는다(orphan 입양 안 함) — reconcile·체결
폴링 경로용.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx

from toss_sleeve.api.auth import TossAuth, request_with_reauth
from toss_sleeve.api.constants import (
    ACCOUNTS_PATH,
    BUYING_POWER_PATH,
    HOLDINGS_PATH,
    ORDERBOOK_PATH,
    PRICES_BATCH_MAX,
    PRICES_PATH,
    TossError,
    TossTransportError,
    order_detail_path,
)
from toss_sleeve.config import TossConfig
from toss_sleeve.ratelimit import RateLimiter
from toss_sleeve.types import (
    BrokerPosition,
    Currency,
    Fill,
    OrderBook,
    OrderBookLevel,
    OrderSide,
    PriceQuote,
    Ticker,
    money,
)


def _currency(value: object, *, default: Currency = Currency.KRW) -> Currency:
    try:
        return Currency(str(value))
    except ValueError:
        return default


def _opt_iso(value: object) -> str:
    """ISO 문자열 필드 — None/"" 는 빈 문자열로(절대 리터럴 "None" 아님). 정산필터 오염 방지."""
    if value is None or value == "":
        return ""
    return str(value)


def _required_money(d: dict[str, object], key: str) -> Decimal | None:
    """필수 실행값 — 부재/null/"" 면 None(0 으로 둔갑 안 함). 호출측이 누락을 에러로 표면화한다."""
    raw = d.get(key)
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _levels(raw: object) -> list[OrderBookLevel]:
    if not isinstance(raw, list):
        return []
    out: list[OrderBookLevel] = []
    for lvl in raw:
        if isinstance(lvl, dict):
            out.append(
                OrderBookLevel(price=money(lvl.get("price")), volume=money(lvl.get("volume")))
            )
    return out


class TossRest:
    """Toss 읽기 REST 클라이언트."""

    def __init__(
        self,
        config: TossConfig,
        auth: TossAuth,
        *,
        client: httpx.AsyncClient | None = None,
        ratelimiter: RateLimiter | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._client = client
        self._owns_client = client is None
        # ASSET 그룹 보수적 한도(5/s).
        self._ratelimiter = ratelimiter or RateLimiter(max_calls=5, period=1.0)
        self._account_seq = config.account_seq

    def __repr__(self) -> str:
        return f"TossRest(sleeve_id={self._config.sleeve_id!r})"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._config.base_url)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> TossRest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _get(
        self, path: str, *, account: bool, params: dict[str, str] | None = None
    ) -> object:
        client = self._get_client()

        async def send(headers: dict[str, str]) -> httpx.Response:
            # 한도 소비는 *매 전송 시도* 마다 — 401 재시도의 두 번째 GET 도 같은 윈도우에서 한도를
            # 소비해야 ASSET TPS 가드를 우회하지 않는다(버스트 429 방지).
            await self._ratelimiter.acquire()
            h = dict(headers)  # 재시도 때 fresh 인증헤더를 받으므로 복사 후 계좌헤더만 덧붙인다.
            if account:
                h["X-Tossinvest-Account"] = await self.account_seq()
            try:
                return await client.get(path, params=params or {}, headers=h)
            except httpx.HTTPError as exc:
                # 전송 실패(연결·읽기 타임아웃)는 소비자가 흡수하도록 TossTransportError 로 정규화.
                raise TossTransportError("Toss 읽기 전송 실패") from exc

        # 401(서버측 토큰 무효화)이면 토큰 재발급 후 1회 재시도(request_with_reauth).
        return await request_with_reauth(self._auth, send)

    async def account_seq(self) -> str:
        """X-Tossinvest-Account 값(accountSeq). 주입값 우선, 없으면 GET /accounts 첫 계좌."""
        if self._account_seq:
            return self._account_seq
        client = self._get_client()

        async def send(headers: dict[str, str]) -> httpx.Response:
            await self._ratelimiter.acquire()  # 매 전송 시도마다 한도 소비(401 재시도 포함).
            try:
                return await client.get(ACCOUNTS_PATH, headers=headers)
            except httpx.HTTPError as exc:
                raise TossTransportError("계좌 조회 전송 실패") from exc

        result = await request_with_reauth(self._auth, send)
        accounts = result if isinstance(result, list) else []
        if not accounts or not isinstance(accounts[0], dict):
            raise TossError("계좌 조회 실패: accounts 비어있음")
        seq = accounts[0].get("accountSeq")
        if seq in (None, ""):
            raise TossError("계좌 조회 실패: accountSeq 없음")
        self._account_seq = str(seq)
        return self._account_seq

    async def holdings(self) -> list[BrokerPosition]:
        """보유 종목 → BrokerPosition(수량 0 제외). 응답 result 는 ``{...,"items":[...]}`` 객체."""
        result = await self._get(HOLDINGS_PATH, account=True)
        items = result.get("items") if isinstance(result, dict) else None
        rows = items if isinstance(items, list) else []
        positions: list[BrokerPosition] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qty = money(row.get("quantity"))
            if qty <= 0:
                continue
            positions.append(
                BrokerPosition(
                    ticker=Ticker(str(row.get("symbol", ""))),
                    quantity=qty,
                    avg_price=money(row.get("averagePurchasePrice")),
                    currency=_currency(row.get("currency")),
                )
            )
        return positions

    async def order_fill(self, order_id: str) -> Fill | None:
        """주문상세의 execution → Fill(누적 체결가·수수료·세금·정산일). 미체결이면 None.

        소비자의 체결 폴링 레이어가 이 값으로 SleeveLedger.apply_fill 을 호출해 가상현금을 정산한다
        (토스 웹소켓 없음 → 폴링이 라이브 체결 인지의 유일 경로). apply_fill 은 order_id 델타로 멱등.
        """
        result = await self._get(order_detail_path(order_id), account=True)
        if not isinstance(result, dict):
            return None
        execution = result.get("execution")
        if not isinstance(execution, dict):
            return None
        filled_qty = money(execution.get("filledQuantity"))
        if filled_qty <= 0:
            return None  # 진행 없음 — 체결 아님.
        # 체결수량이 있는데 평균체결가가 누락/비정상이면 실행금액을 검증할 수 없다 → 표면화(0 둔갑 금지).
        avg_price = _required_money(execution, "averageFilledPrice")
        if avg_price is None or avg_price <= 0:
            raise TossError(
                f"체결수량 있으나 평균체결가 누락/비정상 — 실행금액 검증 불가: order_id={order_id!r}"
            )
        side = OrderSide.SELL if str(result.get("side", "")).upper() == "SELL" else OrderSide.BUY
        return Fill(
            order_id=order_id,
            client_order_id=_opt_iso(result.get("clientOrderId")),
            symbol=_opt_iso(result.get("symbol")),
            side=side,
            currency=_currency(result.get("currency")),
            filled_quantity=filled_qty,
            avg_price=avg_price,
            commission=money(execution.get("commission")),
            tax=money(execution.get("tax")),
            filled_at=_opt_iso(execution.get("filledAt")),
            settlement_date=_opt_iso(execution.get("settlementDate")),
        )

    async def prices(self, symbols: list[str]) -> list[PriceQuote]:
        """배치 시세 — ``GET /prices?symbols=A,B,C`` (최대 200, 콤마구분). 응답 항목당 lastPrice.

        한 콜로 풀 전체 last-price 를 받는다 → 폴링 피드의 주 시세 소스(웹소켓 없음). MARKET_DATA
        그룹이라 계좌 헤더 불필요(account=False).
        """
        if not symbols:
            return []
        out: list[PriceQuote] = []
        # 200 초과 풀은 배치 상한으로 청크 분할(한 요청에 다 넣으면 토스가 거부).
        for start in range(0, len(symbols), PRICES_BATCH_MAX):
            chunk = symbols[start : start + PRICES_BATCH_MAX]
            result = await self._get(
                PRICES_PATH, account=False, params={"symbols": ",".join(chunk)}
            )
            items = result.get("items") if isinstance(result, dict) else result
            rows = items if isinstance(items, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                out.append(
                    PriceQuote(
                        symbol=str(row.get("symbol", "")),
                        last_price=money(row.get("lastPrice")),
                        currency=_currency(row.get("currency")),
                        timestamp=str(row.get("timestamp", "")),
                    )
                )
        return out

    async def orderbook(self, symbol: str) -> OrderBook:
        """호가 depth — ``GET /orderbook?symbol=X`` (단건, 배치 불가). bid/ask depth 가 필요할 때만."""
        result = await self._get(ORDERBOOK_PATH, account=False, params={"symbol": symbol})
        data = result if isinstance(result, dict) else {}
        return OrderBook(
            symbol=symbol,
            bids=tuple(_levels(data.get("bids"))),
            asks=tuple(_levels(data.get("asks"))),
            currency=_currency(data.get("currency")),
            timestamp=str(data.get("timestamp", "")),
        )

    async def buying_power(self, currency: Currency) -> Decimal:
        """토스 계좌 전체 주문가능현금(통화별). *보수적 2차 가드* 전용 — sleeve 가용은 SleeveLedger.

        ``GET /api/v1/buying-power?currency=KRW|USD`` → ``{"currency":...,"cashBuyingPower":"3131"}``.
        """
        result = await self._get(
            BUYING_POWER_PATH, account=True, params={"currency": currency.value}
        )
        if isinstance(result, dict) and "cashBuyingPower" in result:
            return money(result["cashBuyingPower"])
        raise TossError("주문가능현금 파싱 실패(cashBuyingPower 없음)")
