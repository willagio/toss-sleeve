"""자체 값 타입 — 소비자 core 에 의존하지 않는다(패키지 독립성).

머니 모델: 모든 가격·금액·수량은 ``Decimal``(정확). 토스는 소수점 *문자열*("25.629803")로
주므로 ``money(x)`` 로 ``Decimal(str(x))`` 변환해 float 오차를 피한다. 통화는 ``Currency`` 태그 —
계좌가 KRW/USD 현금을 분리 보유하므로(토스 cashBuyingPower 통화별) sleeve 원장도 통화별이다.

소비자(예: gold-digger-alpha)는 자기 core/types·ports.broker 를 두고 토스 seam 에서 얇게 변환한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import NewType

# 종목코드(KR 6자리 "005930", US 심볼 "AAPL"). str 오용 방지 NewType.
Ticker = NewType("Ticker", str)


def money(value: object) -> Decimal:
    """API 응답값(문자열/숫자/None)을 정확한 Decimal 로. None/"" 은 0. float 은 str 경유(오차 차단)."""
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    """주문 유효조건. KR 은 DAY, US 종가/시가 경매는 CLS/OPG(LOC/MOO)."""

    DAY = "DAY"
    CLS = "CLS"
    OPG = "OPG"


class OrderStatus(StrEnum):
    """주문 상태 — 체결 폴링/원장 귀속용. 토스 주문상세 응답을 정규화."""

    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """단건 주문 요청.

    KR/US 단일 표면. 수량 기반(``quantity``)이 기본이고, US 소수점 금액 매수는 ``order_amount``
    (그 통화 금액)로 표현한다 — 둘 중 정확히 하나만 준다. ``price`` 는 LIMIT 에만, ``currency`` 는
    예약·원장 통화 버킷 결정에 쓰인다.
    """

    ticker: Ticker
    side: OrderSide
    order_type: OrderType
    currency: Currency
    quantity: Decimal | None = None
    order_amount: Decimal | None = None  # US 금액·소수점 매수(MARKET BUY 한정)
    price: Decimal | None = None  # LIMIT 일 때만
    time_in_force: TimeInForce = TimeInForce.DAY
    # 멱등성 키 — 재연결·중복 시그널에도 같은 키면 재발주 안 됨. 토스 clientOrderId 로 전달.
    client_order_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.ticker):
            raise ValueError("ticker 는 비어있을 수 없음")
        has_qty = self.quantity is not None
        has_amt = self.order_amount is not None
        if has_qty == has_amt:
            raise ValueError("quantity 와 order_amount 중 정확히 하나가 필요함")
        # is_finite() 를 먼저 보고 단락해 NaN 비교의 InvalidOperation 을 피한다.
        if has_qty and self.quantity is not None and (
            not self.quantity.is_finite() or self.quantity <= 0
        ):
            raise ValueError(f"quantity 는 양수·유한이어야 함: {self.quantity}")
        if has_amt:
            if self.side is not OrderSide.BUY or self.order_type is not OrderType.MARKET:
                raise ValueError("order_amount 는 MARKET BUY 에만 허용됨")
            if self.order_amount is not None and (
                not self.order_amount.is_finite() or self.order_amount <= 0
            ):
                raise ValueError(f"order_amount 는 양수·유한이어야 함: {self.order_amount}")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT 주문에는 price 가 필요함")
        if self.price is not None and (not self.price.is_finite() or self.price <= 0):
            raise ValueError(f"price 는 양수·유한이어야 함: {self.price}")


@dataclass(frozen=True, slots=True)
class OrderAck:
    accepted: bool
    order_id: str
    client_order_id: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    ticker: Ticker
    quantity: Decimal
    avg_price: Decimal
    currency: Currency


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """진입 후 청산 계획 선언. 브로커는 *기록만* 하고(place_oco) 청산 실행은 소비자(엔진)가 운전한다.

    take_profit/ratios 로 부분 익절(30/40/30 등)을 표현. 브로커가 독자 발주하면 소비자 청산과
    겹쳐 이중 매도가 나므로, 본 타입은 선언일 뿐 패키지가 자동 발주하지 않는다.
    """

    stop: Decimal
    take_profit: tuple[Decimal, ...]
    ratios: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.take_profit) != len(self.ratios):
            raise ValueError("take_profit 와 ratios 길이가 다름")
        if abs(sum(self.ratios) - 1.0) > 1e-6:
            raise ValueError(f"ratios 합이 1.0 이 아님: {sum(self.ratios)}")


@dataclass(frozen=True, slots=True)
class Fill:
    """체결 *누적 스냅샷* — 토스 주문상세 execution 의 실제값(추정 금지, NAV 드리프트 방지).

    중요: ``filled_quantity``/``commission``/``tax`` 는 그 주문의 *누적* 총량이다(토스 execution 은
    누적 보고). 같은 order_id 로 여러 번 폴링되면 같은/증가한 스냅샷이 온다 — SleeveLedger 가
    order_id 기준 델타로 멱등 적용한다(중복·부분체결 안전). 금액은 ``currency`` 통화 단위.

    settlement_date 는 ISO 날짜("2026-06-20"). 매도 대금은 이 날짜 전까지 미정산이라 주문가능
    가상현금에 넣지 않는다(T+2). 매수는 체결 즉시 virtual_cash 차감.
    """

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    currency: Currency
    filled_quantity: Decimal
    avg_price: Decimal
    commission: Decimal
    tax: Decimal
    filled_at: str
    settlement_date: str

    def __post_init__(self) -> None:
        # 실행값 무결성 — 누락(0 둔갑)·비정상(NaN/음수)이면 Fill 자체를 만들지 않는다(실행금액 검증).
        # is_finite() 를 먼저 보고 단락해 NaN 비교의 InvalidOperation 을 피한다.
        if not self.filled_quantity.is_finite() or self.filled_quantity <= 0:
            raise ValueError(f"filled_quantity 는 양수여야 함: {self.filled_quantity}")
        if not self.avg_price.is_finite() or self.avg_price <= 0:
            raise ValueError(f"avg_price 는 양수여야 함: {self.avg_price}")
        if not self.commission.is_finite() or self.commission < 0:
            raise ValueError(f"commission 은 음수일 수 없음: {self.commission}")
        if not self.tax.is_finite() or self.tax < 0:
            raise ValueError(f"tax 는 음수일 수 없음: {self.tax}")

    def cost_basis(self) -> Decimal:
        """누적 체결 원금(수수료/세금 제외) = avg_price × filled_quantity."""
        return self.avg_price * self.filled_quantity


@dataclass(frozen=True, slots=True)
class CurrencyCash:
    """통화별 sleeve 현금 스냅샷."""

    currency: Currency
    virtual_cash: Decimal
    available: Decimal


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """/api/v1/prices 단건 — 마지막 체결가(배치 조회 한 항목). bid/ask 는 orderbook 별도."""

    symbol: str
    last_price: Decimal
    currency: Currency
    timestamp: str


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    """/api/v1/orderbook 단건 — 호가 depth(종목당 1콜, 배치 불가)."""

    symbol: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    currency: Currency
    timestamp: str

    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None
