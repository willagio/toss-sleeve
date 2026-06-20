"""SleeveLedger — 공유계좌 sleeve 의 가상현금 원장(포트 + 구현 2종).

TOSS_SHARED_ACCOUNT_PROTOCOL.md: 하나의 토스 계좌를 여러 봇이 공유할 때 각 sleeve 의 주문가능
현금은 계좌 전체 예수금이 아니라 *자기 가상현금*에서 온다(불변식 2 하드캡 / 3 원장이 진실).
계좌가 KRW/USD 현금을 분리 보유하므로 원장도 **통화별 버킷**이다. 산식(통화별):

    available(c) = virtual_cash(c) − Σ reserved(c) − Σ(미정산 매도대금(c), settlement_date > today)
    체결 정산: 매수 virtual_cash −= 체결원금 + 수수료, 매도 += 원금 − 수수료 − 세금
    NAV       = Σ_c (virtual_cash(c) + Σ 보유수량 × 현재가)  ← FX 환산은 소비자(환율 endpoint)

멱등·부분체결: 토스 execution 은 *누적* 스냅샷이라 같은 order_id 가 반복/증가 폴링된다. apply_fill 은
order_id 의 직전 스냅샷 대비 **델타**만 적용한다 — 중복 적용 무해(델타 0), 부분→완전도 정확. 매수
예약은 체결 원금만큼 점진 해제(잔량만 예약 유지). 금액·가격·수량 모두 Decimal(정확).

구현 2종: InMemorySleeveLedger(테스트·결정론), SqliteSleeveLedger(영속, money 는 TEXT 저장).
패키지는 *스키마*만 소유하고 DB 파일/연결은 소비자가 주입한다(공유 인스턴스 아님).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from toss_sleeve.types import BrokerPosition, Currency, Fill, OrderSide, OrderStatus, Ticker


def _iso_today() -> str:
    return date.today().isoformat()


def _signed_cash(fill: Fill) -> Decimal:
    """체결의 누적 현금 효과(부호). 매수 음수(원금+수수료+세금), 매도 양수(원금−수수료−세금)."""
    base = fill.cost_basis()
    if fill.side is OrderSide.BUY:
        return -(base + fill.commission + fill.tax)
    return base - fill.commission - fill.tax


def _can_reserve(available: Decimal, prev_for_coid: Decimal, amount: Decimal) -> bool:
    """동일 coid 재예약은 멱등(set) — 가용에 기존 예약분을 더해 거짓거부를 막는다."""
    return amount <= available + prev_for_coid


def _reservation_after_fill(
    recorded_qty: Decimal, new_filled_qty: Decimal, prev_reserved: Decimal, delta_notional: Decimal
) -> Decimal | None:
    """체결 후 남길 예약액(None=전액해제). 완전체결(누적체결≥주문수량)이면 잔여 전액해제 —
    유리체결·금액주문으로 생기던 유령 잔여예약을 근절. 수량 불명(recorded_qty=0)이면 비례감액만 하고
    종료신호(mark_order_terminal)에서 해제."""
    if recorded_qty > 0 and new_filled_qty >= recorded_qty:
        return None
    left = prev_reserved - delta_notional
    return left if left > 0 else None


def _fill_progress(prev: Fill | None, fill: Fill) -> str:
    """누적 스냅샷 진행 판정. 'noop'(완전 동일) / 'apply'(진행 또는 현금필드 보정) / 'regression'(감소).

    수량이 같아도 수수료/세금/평균가가 늦게 반영·정정되면 현금 영향이 있으므로 'apply'(델타 정산).
    중복으로 건너뛰는 건 현금 영향 필드까지 *전부 동일*할 때뿐."""
    if prev is None:
        return "apply"
    if fill.filled_quantity < prev.filled_quantity:
        return "regression"
    if (
        fill.filled_quantity == prev.filled_quantity
        and fill.commission == prev.commission
        and fill.tax == prev.tax
        and fill.avg_price == prev.avg_price
    ):
        return "noop"
    return "apply"


def _terminal_order_status(recorded_qty: Decimal, new_filled_qty: Decimal) -> OrderStatus:
    """체결 진행에 따른 주문 상태 — 완전체결이면 FILLED, 아니면 PARTIALLY_FILLED."""
    if recorded_qty > 0 and new_filled_qty >= recorded_qty:
        return OrderStatus.FILLED
    return OrderStatus.PARTIALLY_FILLED


# (client_order_id, symbol, side, status, order_id, recorded_qty)
_OrderRow = tuple[str, str, str, str, str, Decimal]
_OPEN_STATUSES = (OrderStatus.PENDING.value, OrderStatus.PARTIALLY_FILLED.value)


def _open_sell_qty(
    orders: Iterable[_OrderRow], filled_by_order_id: dict[str, Decimal], ticker: str,
    exclude_coid: str | None = None,
) -> Decimal:
    """티커의 미체결 SELL 잔량 합 — 체결만 보는 보유수량에서 빼 초과매도를 막는다(공유계좌 잠식 방지).

    exclude_coid 가 주어지면 그 client_order_id 의 미체결 매도는 제외 — 같은 coid 재시도가 *자기*
    예약에 대해 초과매도로 거부되지 않게(멱등, BUY reserve 의 prev 가산과 대칭)."""
    total = Decimal(0)
    for coid, symbol, side, status, order_id, recorded in orders:
        if coid == exclude_coid:
            continue
        if symbol == ticker and side == OrderSide.SELL.value and status in _OPEN_STATUSES:
            remaining = recorded - filled_by_order_id.get(order_id, Decimal(0))
            if remaining > 0:
                total += remaining
    return total


def _position_rows(snapshots: Iterable[Fill]) -> list[BrokerPosition]:
    """주문별 *누적 스냅샷* → 종목별 순포지션. 이동평균원가법 — 매도 시 평단을 비례 차감하고 완전
    청산이면 basis 를 리셋해 재진입이 과거 원가에 오염되지 않게 한다. 시간순(filled_at, order_id) 적용."""
    ordered = sorted(snapshots, key=lambda f: (f.filled_at, f.order_id))
    agg: dict[str, list[Decimal]] = {}  # symbol -> [net_qty, buy_cost, buy_qty]
    cur_of: dict[str, Currency] = {}
    for f in ordered:
        slot = agg.setdefault(f.symbol, [Decimal(0), Decimal(0), Decimal(0)])
        cur_of.setdefault(f.symbol, f.currency)
        if f.side is OrderSide.BUY:
            slot[0] += f.filled_quantity
            slot[1] += f.cost_basis()
            slot[2] += f.filled_quantity
        else:
            avg = slot[1] / slot[2] if slot[2] > 0 else Decimal(0)
            removed = min(f.filled_quantity, slot[2])  # 추적 매수수량 초과분 클램프(음수 basis 방지).
            slot[1] -= avg * removed
            slot[2] -= removed
            slot[0] -= f.filled_quantity
            if slot[0] <= 0 or slot[2] <= 0:  # 완전 청산 → basis 리셋.
                slot[1] = Decimal(0)
                slot[2] = Decimal(0)
    out: list[BrokerPosition] = []
    for symbol, (net, buy_cost, buy_qty) in agg.items():
        if net > 0:
            out.append(
                BrokerPosition(
                    ticker=Ticker(symbol),
                    quantity=net,
                    avg_price=(buy_cost / buy_qty if buy_qty > 0 else Decimal(0)),
                    currency=cur_of[symbol],
                )
            )
    return out


class SleeveLedger(Protocol):
    @property
    def sleeve_id(self) -> str:
        """전역 유일한 sleeve 식별자 — order 귀속·태깅의 1차 키."""
        ...

    def available(self, currency: Currency) -> Decimal:
        """통화별 주문가능 가상현금 = virtual_cash − Σ reserved − Σ 미정산 매도대금(T+2)."""
        ...

    def virtual_cash(self, currency: Currency) -> Decimal:
        """통화별 현재 가상현금(미정산 매도대금 포함, 예약 미차감)."""
        ...

    def reserve(self, client_order_id: str, amount: Decimal, currency: Currency) -> bool:
        """매수 발주분 키잉 예약. 가용 초과면 False(하드캡 거부). client_order_id 필수(귀속)."""
        ...

    def release(self, client_order_id: str) -> None:
        """예약 해제(주문 거부·취소). 없으면 무시(멱등)."""
        ...

    def record_order(
        self,
        *,
        client_order_id: str,
        order_id: str,
        symbol: str,
        side: OrderSide,
        currency: Currency,
        quantity: Decimal,
    ) -> None:
        """client_order_id ↔ order_id 귀속 매핑 기록(1차 진실)."""
        ...

    def apply_fill(self, fill: Fill) -> None:
        """누적 체결 스냅샷을 order_id 델타로 멱등 정산 — 실수수료/세금 반영, 매수 예약 점진 해제."""
        ...

    def record_cashflow(self, amount: Decimal, currency: Currency, *, memo: str = "") -> None:
        """외부 입출금(부호 명시) — 매매손익과 분리. FX 환전은 −KRW/+USD 두 건으로 기록."""
        ...

    def positions(self) -> list[BrokerPosition]:
        """sleeve 보유 포지션(체결 원장에서 유도) — DB-only 진실(불변식 3)."""
        ...

    def position_quantity(self, ticker: str) -> Decimal:
        """해당 티커의 sleeve 순보유 수량(체결 기준). 없으면 0."""
        ...

    def available_to_sell(self, ticker: str) -> Decimal:
        """매도가능 수량 = 보유수량 − 미체결 SELL 잔량. 초과매도(공유계좌 잠식) 방지용."""
        ...

    def reserve_sell(
        self, *, client_order_id: str, order_id: str, symbol: str, currency: Currency,
        quantity: Decimal,
    ) -> bool:
        """매도가능 체크 + 미체결 등록을 *원자적으로*. 초과면 False. 멀티워커 초과매도 방지(BUY reserve 대칭)."""
        ...

    def client_order_id_for(self, order_id: str) -> str | None:
        """order_id → client_order_id 역조회(취소 시 예약 해제용). 재시작 후에도 동작."""
        ...

    def order_status(self, client_order_id: str) -> OrderStatus | None:
        """기록된 주문 상태. 없으면 None."""
        ...

    def mark_order_terminal(self, client_order_id: str, status: OrderStatus) -> None:
        """주문을 종료상태(filled/cancelled/rejected)로 표시하고 잔여 예약을 전액 해제(멱등)."""
        ...


class InMemorySleeveLedger:
    """휘발성 구현 — 테스트·결정론. SqliteSleeveLedger 와 동일 산식."""

    def __init__(
        self,
        *,
        sleeve_id: str,
        allocated_capital: Decimal | int,
        base_currency: Currency = Currency.KRW,
        today_func: Callable[[], str] = _iso_today,
    ) -> None:
        if not sleeve_id:
            raise ValueError("sleeve_id 는 비어있을 수 없음")
        cap = Decimal(allocated_capital)
        if cap < 0:
            raise ValueError(f"allocated_capital 은 음수일 수 없음: {cap}")
        self._sleeve_id = sleeve_id
        self._cash: dict[Currency, Decimal] = {base_currency: cap}
        self._reserved: dict[str, tuple[Currency, Decimal]] = {}
        self._snapshots: dict[str, Fill] = {}  # order_id -> 최신 누적 스냅샷
        self._orders: dict[str, dict[str, object]] = {}
        self._today = today_func

    @property
    def sleeve_id(self) -> str:
        return self._sleeve_id

    def _vc(self, c: Currency) -> Decimal:
        return self._cash.get(c, Decimal(0))

    def _reserved_total(self, c: Currency) -> Decimal:
        return sum((amt for cur, amt in self._reserved.values() if cur == c), Decimal(0))

    def _settlement_pending(self, c: Currency) -> Decimal:
        today = self._today()
        return sum(
            (
                f.cost_basis() - f.commission - f.tax
                for f in self._snapshots.values()
                if f.side is OrderSide.SELL
                and f.currency == c
                and f.settlement_date
                and f.settlement_date > today
            ),
            Decimal(0),
        )

    def available(self, currency: Currency) -> Decimal:
        return (
            self._vc(currency)
            - self._reserved_total(currency)
            - self._settlement_pending(currency)
        )

    def virtual_cash(self, currency: Currency) -> Decimal:
        return self._vc(currency)

    def reserve(self, client_order_id: str, amount: Decimal, currency: Currency) -> bool:
        if not client_order_id:
            raise ValueError("reserve 에는 client_order_id 가 필요함(귀속)")
        if amount < 0:
            raise ValueError(f"reserve amount 는 음수일 수 없음: {amount}")
        prev_cur, prev = self._reserved.get(client_order_id, (currency, Decimal(0)))
        prev_same = prev if prev_cur == currency else Decimal(0)
        if not _can_reserve(self.available(currency), prev_same, amount):
            return False
        self._reserved[client_order_id] = (currency, amount)  # set(멱등), not add.
        return True

    def release(self, client_order_id: str) -> None:
        self._reserved.pop(client_order_id, None)

    def _recorded_qty(self, client_order_id: str) -> Decimal:
        o = self._orders.get(client_order_id)
        q = o.get("quantity") if o else None
        return q if isinstance(q, Decimal) else Decimal(0)

    def client_order_id_for(self, order_id: str) -> str | None:
        return next(
            (coid for coid, o in self._orders.items() if o.get("order_id") == order_id), None
        )

    def order_status(self, client_order_id: str) -> OrderStatus | None:
        o = self._orders.get(client_order_id)
        return OrderStatus(str(o["status"])) if o else None

    def mark_order_terminal(self, client_order_id: str, status: OrderStatus) -> None:
        o = self._orders.get(client_order_id)
        if o is not None:
            o["status"] = status.value
        self.release(client_order_id)

    def record_order(
        self,
        *,
        client_order_id: str,
        order_id: str,
        symbol: str,
        side: OrderSide,
        currency: Currency,
        quantity: Decimal,
    ) -> None:
        # 빈 order_id 재기록(presend 재시도)은 기존 order_id 를 덮지 않는다 — 라이브 주문의 cancel
        # 역조회·reconcile 가 깨지지 않게.
        existing = self._orders.get(client_order_id)
        if not order_id and existing is not None:
            order_id = str(existing.get("order_id", ""))
        self._orders[client_order_id] = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side.value,
            "currency": currency.value,
            "quantity": quantity,
            "status": "pending",
        }

    def apply_fill(self, fill: Fill) -> None:
        prev = self._snapshots.get(fill.order_id)
        progress = _fill_progress(prev, fill)
        if progress == "noop":
            return  # 중복 폴링 — 멱등 no-op.
        if progress == "regression":
            raise ValueError(
                f"누적 체결수량 감소(desync): {fill.order_id} "
                f"{prev.filled_quantity if prev else 0} -> {fill.filled_quantity}"
            )
        delta_cash = _signed_cash(fill) - (_signed_cash(prev) if prev else Decimal(0))
        delta_notional = fill.cost_basis() - (prev.cost_basis() if prev else Decimal(0))
        self._cash[fill.currency] = self._vc(fill.currency) + delta_cash
        if fill.side is OrderSide.BUY and fill.client_order_id in self._reserved:
            cur, remaining = self._reserved[fill.client_order_id]
            left = _reservation_after_fill(
                self._recorded_qty(fill.client_order_id), fill.filled_quantity, remaining,
                delta_notional,
            )
            if left is None:
                self._reserved.pop(fill.client_order_id, None)
            else:
                self._reserved[fill.client_order_id] = (cur, left)
        self._snapshots[fill.order_id] = fill
        o = self._orders.get(fill.client_order_id)
        if o is not None:
            o["status"] = _terminal_order_status(
                self._recorded_qty(fill.client_order_id), fill.filled_quantity
            ).value

    def record_cashflow(self, amount: Decimal, currency: Currency, *, memo: str = "") -> None:
        self._cash[currency] = self._vc(currency) + amount

    def positions(self) -> list[BrokerPosition]:
        return _position_rows(self._snapshots.values())

    def position_quantity(self, ticker: str) -> Decimal:
        return next((p.quantity for p in self.positions() if p.ticker == ticker), Decimal(0))

    def available_to_sell(self, ticker: str) -> Decimal:
        return self._sell_availability(ticker)

    def _sell_availability(self, ticker: str, *, exclude_coid: str | None = None) -> Decimal:
        orders: list[_OrderRow] = []
        for coid, o in self._orders.items():
            q = o.get("quantity")
            qty = q if isinstance(q, Decimal) else Decimal(0)
            orders.append((
                str(coid), str(o.get("symbol", "")), str(o.get("side", "")),
                str(o.get("status", "")), str(o.get("order_id", "")), qty,
            ))
        filled = {oid: f.filled_quantity for oid, f in self._snapshots.items()}
        return self.position_quantity(ticker) - _open_sell_qty(orders, filled, ticker, exclude_coid)

    def reserve_sell(
        self, *, client_order_id: str, order_id: str, symbol: str, currency: Currency,
        quantity: Decimal,
    ) -> bool:
        if not client_order_id:
            raise ValueError("reserve_sell 에는 client_order_id 가 필요함(귀속)")
        # 단일 이벤트루프 내 동기 실행 → 체크+등록이 원자(중간 yield 없음). 자기 예약은 제외(멱등 재시도).
        if quantity > self._sell_availability(symbol, exclude_coid=client_order_id):
            return False
        self.record_order(
            client_order_id=client_order_id, order_id=order_id, symbol=symbol,
            side=OrderSide.SELL, currency=currency, quantity=quantity,
        )
        return True


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sleeve_cash (
    sleeve_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    allocated_capital TEXT NOT NULL,
    virtual_cash TEXT NOT NULL,
    PRIMARY KEY (sleeve_id, currency)
);
CREATE TABLE IF NOT EXISTS sleeve_reservations (
    sleeve_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    PRIMARY KEY (sleeve_id, client_order_id)
);
CREATE TABLE IF NOT EXISTS sleeve_orders (
    sleeve_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    currency TEXT NOT NULL,
    quantity TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (sleeve_id, client_order_id)
);
CREATE TABLE IF NOT EXISTS sleeve_fills (
    sleeve_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    currency TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    avg_price TEXT NOT NULL,
    commission TEXT NOT NULL,
    tax TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    PRIMARY KEY (sleeve_id, order_id)
);
CREATE TABLE IF NOT EXISTS sleeve_cashflows (
    sleeve_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    memo TEXT NOT NULL,
    id INTEGER PRIMARY KEY AUTOINCREMENT
);
"""


class SqliteSleeveLedger:
    """영속 구현 — sqlite3(stdlib). money 는 TEXT(Decimal str) 저장(정확). 집계는 Python Decimal 합산
    (sqlite REAL SUM 부정확 회피). 소비자가 DB 경로/연결을 주입한다(자기 인스턴스)."""

    def __init__(
        self,
        *,
        sleeve_id: str,
        allocated_capital: Decimal | int,
        base_currency: Currency = Currency.KRW,
        path: str | Path = ":memory:",
        connection: sqlite3.Connection | None = None,
        today_func: Callable[[], str] = _iso_today,
    ) -> None:
        if not sleeve_id:
            raise ValueError("sleeve_id 는 비어있을 수 없음")
        cap = Decimal(allocated_capital)
        if cap < 0:
            raise ValueError(f"allocated_capital 은 음수일 수 없음: {cap}")
        self._sleeve_id = sleeve_id
        self._today = today_func
        self._owns_conn = connection is None
        self._conn = connection or sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        # 멀티프로세스 reserve 직렬화(BEGIN IMMEDIATE)에서 SQLITE_BUSY 대신 최대 5초 대기.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO sleeve_cash(sleeve_id, currency, allocated_capital, "
            "virtual_cash) VALUES (?, ?, ?, ?)",
            (sleeve_id, base_currency.value, str(cap), str(cap)),
        )
        self._conn.commit()

    @property
    def sleeve_id(self) -> str:
        return self._sleeve_id

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        """BEGIN IMMEDIATE 트랜잭션 — write 락을 즉시 잡아 read-modify-write 를 멀티프로세스에서도
        직렬화한다(가용/스냅샷을 읽고 조건부로 쓰는 경로의 레이스 차단). 예외 시 ROLLBACK."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _ensure_currency(self, c: Currency) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO sleeve_cash(sleeve_id, currency, allocated_capital, "
            "virtual_cash) VALUES (?, ?, '0', '0')",
            (self._sleeve_id, c.value),
        )

    def _vc(self, c: Currency) -> Decimal:
        row = self._conn.execute(
            "SELECT virtual_cash FROM sleeve_cash WHERE sleeve_id = ? AND currency = ?",
            (self._sleeve_id, c.value),
        ).fetchone()
        return Decimal(row["virtual_cash"]) if row else Decimal(0)

    def _set_vc(self, c: Currency, value: Decimal) -> None:
        self._conn.execute(
            "UPDATE sleeve_cash SET virtual_cash = ? WHERE sleeve_id = ? AND currency = ?",
            (str(value), self._sleeve_id, c.value),
        )

    def _reserved_total(self, c: Currency) -> Decimal:
        rows = self._conn.execute(
            "SELECT amount FROM sleeve_reservations WHERE sleeve_id = ? AND currency = ?",
            (self._sleeve_id, c.value),
        ).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal(0))

    def _settlement_pending(self, c: Currency) -> Decimal:
        rows = self._conn.execute(
            "SELECT avg_price, filled_quantity, commission, tax FROM sleeve_fills "
            "WHERE sleeve_id = ? AND currency = ? AND side = ? AND settlement_date > ?",
            (self._sleeve_id, c.value, OrderSide.SELL.value, self._today()),
        ).fetchall()
        return sum(
            (
                Decimal(r["avg_price"]) * Decimal(r["filled_quantity"])
                - Decimal(r["commission"])
                - Decimal(r["tax"])
                for r in rows
            ),
            Decimal(0),
        )

    def available(self, currency: Currency) -> Decimal:
        return (
            self._vc(currency)
            - self._reserved_total(currency)
            - self._settlement_pending(currency)
        )

    def virtual_cash(self, currency: Currency) -> Decimal:
        return self._vc(currency)

    def reserve(self, client_order_id: str, amount: Decimal, currency: Currency) -> bool:
        if not client_order_id:
            raise ValueError("reserve 에는 client_order_id 가 필요함(귀속)")
        if amount < 0:
            raise ValueError(f"reserve amount 는 음수일 수 없음: {amount}")
        # 하드캡 불변식: 가용 체크와 예약 삽입을 한 IMMEDIATE 트랜잭션으로 묶어 멀티프로세스에서도
        # 직렬화(두 워커가 같은 available 을 읽고 둘 다 삽입하는 레이스 차단).
        with self._immediate():
            row = self._conn.execute(
                "SELECT currency, amount FROM sleeve_reservations "
                "WHERE sleeve_id = ? AND client_order_id = ?",
                (self._sleeve_id, client_order_id),
            ).fetchone()
            prev_same = (
                Decimal(row["amount"]) if row and row["currency"] == currency.value else Decimal(0)
            )
            if not _can_reserve(self.available(currency), prev_same, amount):
                return False
            self._conn.execute(
                "INSERT INTO sleeve_reservations(sleeve_id, client_order_id, currency, amount) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(sleeve_id, client_order_id) DO UPDATE SET "
                "amount = excluded.amount, currency = excluded.currency",  # set(멱등), not add.
                (self._sleeve_id, client_order_id, currency.value, str(amount)),
            )
        return True

    def release(self, client_order_id: str) -> None:
        self._conn.execute(
            "DELETE FROM sleeve_reservations WHERE sleeve_id = ? AND client_order_id = ?",
            (self._sleeve_id, client_order_id),
        )
        self._conn.commit()

    def _write_order(
        self, *, client_order_id: str, order_id: str, symbol: str, side: OrderSide,
        currency: Currency, quantity: Decimal,
    ) -> None:
        # 트랜잭션 미관리(호출측이 _conn/_immediate 로 감쌈). 재기록은 status 를 pending 으로 복원하고
        # 필드 갱신(InMemory 전체교체와 일치) — rejected 로 남으면 재시도 라이브 주문이 가드에서 빠진다.
        self._conn.execute(
            "INSERT INTO sleeve_orders(sleeve_id, client_order_id, order_id, symbol, side, "
            "currency, quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending') "
            # 빈 order_id 재기록은 기존 order_id 보존(라이브 주문 cancel·reconcile 유지).
            "ON CONFLICT(sleeve_id, client_order_id) DO UPDATE SET order_id = "
            "CASE WHEN excluded.order_id = '' THEN sleeve_orders.order_id ELSE excluded.order_id END, "
            "symbol = excluded.symbol, side = excluded.side, currency = excluded.currency, "
            "quantity = excluded.quantity, status = 'pending'",
            (
                self._sleeve_id, client_order_id, order_id, symbol, side.value,
                currency.value, str(quantity),
            ),
        )

    def record_order(
        self,
        *,
        client_order_id: str,
        order_id: str,
        symbol: str,
        side: OrderSide,
        currency: Currency,
        quantity: Decimal,
    ) -> None:
        with self._conn:  # 원자 커밋/롤백.
            self._write_order(
                client_order_id=client_order_id, order_id=order_id, symbol=symbol,
                side=side, currency=currency, quantity=quantity,
            )

    def reserve_sell(
        self, *, client_order_id: str, order_id: str, symbol: str, currency: Currency,
        quantity: Decimal,
    ) -> bool:
        if not client_order_id:
            raise ValueError("reserve_sell 에는 client_order_id 가 필요함(귀속)")
        # 매도가능 체크와 미체결 등록을 한 IMMEDIATE 트랜잭션으로 — 두 워커가 같은 가용을 읽고 둘 다
        # 등록하는 초과매도 레이스를 차단(BUY reserve 대칭). 자기 예약은 제외해 같은 coid 재시도를 멱등 처리.
        with self._immediate():
            if quantity > self._sell_availability(symbol, exclude_coid=client_order_id):
                return False
            self._write_order(
                client_order_id=client_order_id, order_id=order_id, symbol=symbol,
                side=OrderSide.SELL, currency=currency, quantity=quantity,
            )
        return True

    def _snapshot(self, order_id: str) -> Fill | None:
        r = self._conn.execute(
            "SELECT * FROM sleeve_fills WHERE sleeve_id = ? AND order_id = ?",
            (self._sleeve_id, order_id),
        ).fetchone()
        return _row_to_fill(r) if r else None

    def _recorded_qty(self, client_order_id: str) -> Decimal:
        r = self._conn.execute(
            "SELECT quantity FROM sleeve_orders WHERE sleeve_id = ? AND client_order_id = ?",
            (self._sleeve_id, client_order_id),
        ).fetchone()
        return Decimal(r["quantity"]) if r else Decimal(0)

    def client_order_id_for(self, order_id: str) -> str | None:
        r = self._conn.execute(
            "SELECT client_order_id FROM sleeve_orders WHERE sleeve_id = ? AND order_id = ?",
            (self._sleeve_id, order_id),
        ).fetchone()
        return str(r["client_order_id"]) if r else None

    def order_status(self, client_order_id: str) -> OrderStatus | None:
        r = self._conn.execute(
            "SELECT status FROM sleeve_orders WHERE sleeve_id = ? AND client_order_id = ?",
            (self._sleeve_id, client_order_id),
        ).fetchone()
        return OrderStatus(str(r["status"])) if r else None

    def mark_order_terminal(self, client_order_id: str, status: OrderStatus) -> None:
        with self._conn:  # 상태표시 + 예약해제를 원자적으로.
            self._conn.execute(
                "UPDATE sleeve_orders SET status = ? WHERE sleeve_id = ? AND client_order_id = ?",
                (status.value, self._sleeve_id, client_order_id),
            )
            self._conn.execute(
                "DELETE FROM sleeve_reservations WHERE sleeve_id = ? AND client_order_id = ?",
                (self._sleeve_id, client_order_id),
            )

    def apply_fill(self, fill: Fill) -> None:
        # prev 읽기를 write 트랜잭션 안에서 수행 — 멀티프로세스 동시 폴링이 같은 prev 를 보고 같은
        # 델타를 각자 적용하는 이중 정산을 차단(IMMEDIATE 락 후 재조회 시 진행 없으면 멱등 no-op).
        with self._immediate():
            prev = self._snapshot(fill.order_id)
            progress = _fill_progress(prev, fill)
            if progress == "noop":
                return  # 중복 폴링 — 멱등 no-op.
            if progress == "regression":
                raise ValueError(
                    f"누적 체결수량 감소(desync): {fill.order_id} "
                    f"{prev.filled_quantity if prev else 0} -> {fill.filled_quantity}"
                )
            delta_cash = _signed_cash(fill) - (_signed_cash(prev) if prev else Decimal(0))
            delta_notional = fill.cost_basis() - (prev.cost_basis() if prev else Decimal(0))
            self._ensure_currency(fill.currency)
            self._set_vc(fill.currency, self._vc(fill.currency) + delta_cash)
            if fill.side is OrderSide.BUY and fill.client_order_id:
                r = self._conn.execute(
                    "SELECT amount FROM sleeve_reservations "
                    "WHERE sleeve_id = ? AND client_order_id = ?",
                    (self._sleeve_id, fill.client_order_id),
                ).fetchone()
                if r is not None:
                    left = _reservation_after_fill(
                        self._recorded_qty(fill.client_order_id), fill.filled_quantity,
                        Decimal(r["amount"]), delta_notional,
                    )
                    if left is None:
                        self._conn.execute(
                            "DELETE FROM sleeve_reservations WHERE sleeve_id = ? "
                            "AND client_order_id = ?",
                            (self._sleeve_id, fill.client_order_id),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE sleeve_reservations SET amount = ? WHERE sleeve_id = ? "
                            "AND client_order_id = ?",
                            (str(left), self._sleeve_id, fill.client_order_id),
                        )
            self._conn.execute(
                "INSERT INTO sleeve_fills(sleeve_id, order_id, client_order_id, symbol, side, "
                "currency, filled_quantity, avg_price, commission, tax, filled_at, settlement_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sleeve_id, order_id) DO UPDATE SET "
                "filled_quantity=excluded.filled_quantity, avg_price=excluded.avg_price, "
                "commission=excluded.commission, tax=excluded.tax, filled_at=excluded.filled_at, "
                "settlement_date=excluded.settlement_date",
                (
                    self._sleeve_id, fill.order_id, fill.client_order_id, fill.symbol,
                    fill.side.value, fill.currency.value, str(fill.filled_quantity),
                    str(fill.avg_price), str(fill.commission), str(fill.tax),
                    fill.filled_at, fill.settlement_date,
                ),
            )
            if fill.client_order_id:
                self._conn.execute(
                    "UPDATE sleeve_orders SET status = ? WHERE sleeve_id = ? "
                    "AND client_order_id = ?",
                    (
                        _terminal_order_status(
                            self._recorded_qty(fill.client_order_id), fill.filled_quantity
                        ).value,
                        self._sleeve_id,
                        fill.client_order_id,
                    ),
                )

    def record_cashflow(self, amount: Decimal, currency: Currency, *, memo: str = "") -> None:
        with self._immediate():  # read-modify-write 직렬화(동시 입출금 레이스 차단).
            self._ensure_currency(currency)
            self._set_vc(currency, self._vc(currency) + amount)
            self._conn.execute(
                "INSERT INTO sleeve_cashflows(sleeve_id, currency, amount, memo) "
                "VALUES (?, ?, ?, ?)",
                (self._sleeve_id, currency.value, str(amount), memo),
            )

    def _all_snapshots(self) -> list[Fill]:
        rows = self._conn.execute(
            "SELECT * FROM sleeve_fills WHERE sleeve_id = ? ORDER BY order_id",
            (self._sleeve_id,),
        ).fetchall()
        return [_row_to_fill(r) for r in rows]

    def positions(self) -> list[BrokerPosition]:
        return _position_rows(self._all_snapshots())

    def position_quantity(self, ticker: str) -> Decimal:
        return next((p.quantity for p in self.positions() if p.ticker == ticker), Decimal(0))

    def available_to_sell(self, ticker: str) -> Decimal:
        return self._sell_availability(ticker)

    def _sell_availability(self, ticker: str, *, exclude_coid: str | None = None) -> Decimal:
        order_rows = self._conn.execute(
            "SELECT client_order_id, symbol, side, status, order_id, quantity "
            "FROM sleeve_orders WHERE sleeve_id = ?",
            (self._sleeve_id,),
        ).fetchall()
        orders: list[_OrderRow] = [
            (
                r["client_order_id"], r["symbol"], r["side"], r["status"], r["order_id"],
                Decimal(r["quantity"]),
            )
            for r in order_rows
        ]
        fill_rows = self._conn.execute(
            "SELECT order_id, filled_quantity FROM sleeve_fills WHERE sleeve_id = ?",
            (self._sleeve_id,),
        ).fetchall()
        filled = {r["order_id"]: Decimal(r["filled_quantity"]) for r in fill_rows}
        return self.position_quantity(ticker) - _open_sell_qty(orders, filled, ticker, exclude_coid)


def _row_to_fill(r: sqlite3.Row) -> Fill:
    return Fill(
        order_id=r["order_id"],
        client_order_id=r["client_order_id"],
        symbol=r["symbol"],
        side=OrderSide(r["side"]),
        currency=Currency(r["currency"]),
        filled_quantity=Decimal(r["filled_quantity"]),
        avg_price=Decimal(r["avg_price"]),
        commission=Decimal(r["commission"]),
        tax=Decimal(r["tax"]),
        filled_at=r["filled_at"],
        settlement_date=r["settlement_date"],
    )
