"""SleeveLedger 산수 — InMemory·Sqlite 동형 + T+2 정산·영속·멀티통화·부분체결 멱등.

검증: 키 예약/해제·하드캡, 체결정산(매수/매도 실수수료·세금), T+2 미정산 분리, 외부 입출금,
포지션 유도, 통화별 버킷 독립, 누적-델타 멱등(중복·부분체결), sqlite 재시작 영속.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from toss_sleeve.ledger import InMemorySleeveLedger, SqliteSleeveLedger
from toss_sleeve.types import Currency, Fill, OrderSide, OrderStatus

TODAY = "2026-06-18"
KRW = Currency.KRW
USD = Currency.USD
D = Decimal


def _mem(cap: int = 1_000_000) -> InMemorySleeveLedger:
    return InMemorySleeveLedger(sleeve_id="s", allocated_capital=cap, today_func=lambda: TODAY)


def _sqlite(path: Path, cap: int = 1_000_000, today: str = TODAY) -> SqliteSleeveLedger:
    return SqliteSleeveLedger(
        sleeve_id="s", allocated_capital=cap, path=path / "ledger.db", today_func=lambda: today
    )


def _buy_fill(
    coid: str, qty: int, price: int, fee: int, *, order_id: str = "o", cur: Currency = KRW
) -> Fill:
    return Fill(
        order_id=order_id, client_order_id=coid, symbol="005930", side=OrderSide.BUY,
        currency=cur, filled_quantity=D(qty), avg_price=D(price), commission=D(fee), tax=D(0),
        filled_at="t", settlement_date="2026-06-20",
    )


def _sell_fill(qty: int, price: int, fee: int, tax: int, settle: str) -> Fill:
    return Fill(
        order_id="o2", client_order_id="", symbol="005930", side=OrderSide.SELL,
        currency=KRW, filled_quantity=D(qty), avg_price=D(price), commission=D(fee), tax=D(tax),
        filled_at="t", settlement_date=settle,
    )


@pytest.fixture(params=["mem", "sqlite"])
def ledger(request, tmp_path: Path):  # noqa: ANN001, ANN201
    return _mem() if request.param == "mem" else _sqlite(tmp_path)


def test_reserve_release_hard_cap(ledger) -> None:  # noqa: ANN001
    assert ledger.reserve("c1", D(600_000), KRW) is True
    assert ledger.available(KRW) == D(400_000)
    assert ledger.reserve("c2", D(600_000), KRW) is False  # 하드캡.
    ledger.release("c1")
    assert ledger.available(KRW) == D(1_000_000)


def test_reserve_requires_client_order_id(ledger) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match="client_order_id"):
        ledger.reserve("", D(100), KRW)


def test_buy_fill_settles_and_releases(ledger) -> None:  # noqa: ANN001
    ledger.reserve("c1", D(700_000), KRW)
    ledger.apply_fill(_buy_fill("c1", qty=10, price=70_000, fee=105))
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 700_000 - 105)
    assert ledger.available(KRW) == D(1_000_000 - 700_000 - 105)  # 예약 0.
    pos = ledger.positions()
    assert len(pos) == 1 and pos[0].quantity == D(10) and pos[0].avg_price == D(70_000)
    assert ledger.position_quantity("005930") == D(10)


def test_sell_fill_t2_unsettled_excluded(ledger) -> None:  # noqa: ANN001
    ledger.apply_fill(_sell_fill(qty=5, price=80_000, fee=60, tax=800, settle="2026-06-20"))
    proceeds = 5 * 80_000 - 60 - 800
    assert ledger.virtual_cash(KRW) == D(1_000_000 + proceeds)
    assert ledger.available(KRW) == D(1_000_000)  # 미정산분 제외(T+2).


def test_sell_fill_settled_after_date(tmp_path: Path) -> None:
    for lg in (
        InMemorySleeveLedger(sleeve_id="s", allocated_capital=1_000_000, today_func=lambda: "2026-06-23"),
        _sqlite(tmp_path, today="2026-06-23"),
    ):
        lg.apply_fill(_sell_fill(qty=5, price=80_000, fee=60, tax=800, settle="2026-06-20"))
        proceeds = 5 * 80_000 - 60 - 800
        assert lg.available(KRW) == D(1_000_000 + proceeds)  # 정산 완료.


def test_cashflow_separates_external(ledger) -> None:  # noqa: ANN001
    ledger.record_cashflow(D(500_000), KRW, memo="deposit")
    assert ledger.available(KRW) == D(1_500_000)
    ledger.record_cashflow(D(-200_000), KRW, memo="withdraw")
    assert ledger.available(KRW) == D(1_300_000)


def test_apply_fill_idempotent_and_partial(ledger) -> None:  # noqa: ANN001
    # 10주 @70000 예약 → 4주 부분체결 → 같은 누적 재적용(멱등) → 10주 완전체결.
    ledger.reserve("c1", D(700_000), KRW)
    ledger.apply_fill(_buy_fill("c1", qty=4, price=70_000, fee=0))
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 280_000)
    assert ledger.available(KRW) == D(300_000)  # 잔량 6주 예약 유지(420k) + 미체결.
    ledger.apply_fill(_buy_fill("c1", qty=4, price=70_000, fee=0))  # 중복 폴링 — no-op.
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 280_000)
    assert ledger.available(KRW) == D(300_000)
    ledger.apply_fill(_buy_fill("c1", qty=10, price=70_000, fee=105))  # 완전체결.
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 700_105)
    assert ledger.available(KRW) == D(1_000_000 - 700_105)  # 예약 0.
    assert ledger.position_quantity("005930") == D(10)


def test_apply_fill_late_fee_correction(ledger) -> None:  # noqa: ANN001
    # 같은 수량으로 수수료가 늦게 반영된 스냅샷 → noop 아니라 현금 보정 적용.
    ledger.reserve("c1", D(700_000), KRW)
    _record(ledger, "c1", 10)
    ledger.apply_fill(_buy_fill("c1", qty=10, price=70_000, fee=0))  # 수수료 미정착.
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 700_000)
    ledger.apply_fill(_buy_fill("c1", qty=10, price=70_000, fee=105))  # 같은 수량·수수료 보정.
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 700_105)


def test_currency_buckets_independent(ledger) -> None:  # noqa: ANN001
    ledger.record_cashflow(D(1000), USD, memo="fx-in")
    assert ledger.available(USD) == D(1000)
    assert ledger.available(KRW) == D(1_000_000)  # KRW 불변.
    assert ledger.reserve("u1", D(600), USD) is True
    assert ledger.available(USD) == D(400)
    assert ledger.reserve("u2", D(600), USD) is False  # USD 하드캡.
    assert ledger.available(KRW) == D(1_000_000)  # KRW 여전히 불변.


def test_us_fractional_price_preserved(ledger) -> None:  # noqa: ANN001
    # USD 소수점 6자리 평단 — 정수 절삭 없이 보존(Decimal).
    ledger.record_cashflow(D(10_000), USD)
    f = Fill(
        order_id="ou", client_order_id="cu", symbol="ETHU", side=OrderSide.BUY,
        currency=USD, filled_quantity=D(110), avg_price=D("25.629803"),
        commission=D("4.31"), tax=D(0), filled_at="t", settlement_date="2026-06-20",
    )
    ledger.apply_fill(f)
    pos = [p for p in ledger.positions() if p.ticker == "ETHU"][0]
    assert pos.avg_price == D("25.629803") and pos.currency == USD


def test_sqlite_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    lg = SqliteSleeveLedger(sleeve_id="s", allocated_capital=1_000_000, path=db, today_func=lambda: TODAY)
    lg.reserve("c1", D(300_000), KRW)
    lg.apply_fill(_buy_fill("c2", qty=1, price=50_000, fee=8, order_id="ob"))
    vc, avail = lg.virtual_cash(KRW), lg.available(KRW)
    lg.close()

    lg2 = SqliteSleeveLedger(sleeve_id="s", allocated_capital=1_000_000, path=db, today_func=lambda: TODAY)
    assert lg2.virtual_cash(KRW) == vc
    assert lg2.available(KRW) == avail  # c1 예약(300k) 보존.
    lg2.release("c1")
    assert lg2.available(KRW) == vc
    lg2.close()


# --- Fill 검증(G3-F): 누락·비정상 실행값 거부 ---------------------------------


def test_fill_rejects_nonpositive() -> None:
    base = dict(
        order_id="o", client_order_id="c", symbol="005930", side=OrderSide.BUY,
        currency=KRW, filled_at="t", settlement_date="2026-06-20",
    )
    with pytest.raises(ValueError, match="filled_quantity"):  # 0 수량
        Fill(**base, filled_quantity=D(0), avg_price=D(100), commission=D(0), tax=D(0))
    with pytest.raises(ValueError, match="avg_price"):  # 누락 가격(0)
        Fill(**base, filled_quantity=D(10), avg_price=D(0), commission=D(0), tax=D(0))
    with pytest.raises(ValueError, match="filled_quantity"):  # 음수 수량(부호 뒤집힘)
        Fill(**base, filled_quantity=D(-1), avg_price=D(100), commission=D(0), tax=D(0))
    with pytest.raises(ValueError, match="commission"):  # 음수 수수료
        Fill(**base, filled_quantity=D(10), avg_price=D(100), commission=D(-1), tax=D(0))


# --- 예약 생애주기(G1): 유령 잔여·멱등·상태 --------------------------------


def _record(ledger, coid: str, qty: int, *, order_id: str = "o") -> None:  # noqa: ANN001
    ledger.record_order(
        client_order_id=coid, order_id=order_id, symbol="005930",
        side=OrderSide.BUY, currency=KRW, quantity=D(qty),
    )


def test_phantom_residue_freed_at_better_price(ledger) -> None:  # noqa: ANN001
    # 10주 @70000 예약 → 유리하게 10주 @69000 완전체결 → 잔여 예약 0(유령 잔여 없음).
    ledger.reserve("c1", D(700_000), KRW)
    _record(ledger, "c1", 10)
    ledger.apply_fill(_buy_fill("c1", qty=10, price=69_000, fee=0))
    assert ledger.position_quantity("005930") == D(10)
    assert ledger.virtual_cash(KRW) == D(1_000_000 - 690_000)
    assert ledger.available(KRW) == ledger.virtual_cash(KRW)  # 예약 0, 잔여 없음


def test_reserve_idempotent_per_coid(ledger) -> None:  # noqa: ANN001
    assert ledger.reserve("c1", D(600_000), KRW) is True
    assert ledger.available(KRW) == D(400_000)
    assert ledger.reserve("c1", D(600_000), KRW) is True  # 재시도 — 멱등(누적 아님).
    assert ledger.available(KRW) == D(400_000)


def test_partial_sets_partially_filled(ledger) -> None:  # noqa: ANN001
    ledger.reserve("c1", D(700_000), KRW)
    _record(ledger, "c1", 10)
    ledger.apply_fill(_buy_fill("c1", qty=4, price=70_000, fee=0))  # 부분.
    assert ledger.order_status("c1") == OrderStatus.PARTIALLY_FILLED
    ledger.apply_fill(_buy_fill("c1", qty=10, price=70_000, fee=0))  # 완전.
    assert ledger.order_status("c1") == OrderStatus.FILLED


def test_mark_order_terminal_releases(ledger) -> None:  # noqa: ANN001
    # 금액주문(수량 0 기록): 비례감액으론 잔여가 남고, terminal 신호로 전액 해제.
    ledger.reserve("c1", D(100_000), KRW)
    _record(ledger, "c1", 0)
    ledger.apply_fill(_buy_fill("c1", qty=1, price=70_000, fee=100))
    assert ledger.available(KRW) < ledger.virtual_cash(KRW)  # 잔여 예약 존재.
    ledger.mark_order_terminal("c1", OrderStatus.FILLED)
    assert ledger.available(KRW) == ledger.virtual_cash(KRW)  # 전액 해제.


def test_rerecord_resets_status(ledger) -> None:  # noqa: ANN001
    # 거부된 주문을 같은 coid 로 재기록 → pending 복원(재시도 라이브 주문이 가드에서 빠지지 않게).
    ledger.record_order(
        client_order_id="s1", order_id="", symbol="005930",
        side=OrderSide.SELL, currency=KRW, quantity=D(6),
    )
    ledger.mark_order_terminal("s1", OrderStatus.REJECTED)
    assert ledger.order_status("s1") == OrderStatus.REJECTED
    ledger.record_order(
        client_order_id="s1", order_id="S1", symbol="005930",
        side=OrderSide.SELL, currency=KRW, quantity=D(6),
    )
    assert ledger.order_status("s1") == OrderStatus.PENDING


def test_client_order_id_for_reverse_lookup(ledger) -> None:  # noqa: ANN001
    _record(ledger, "c1", 10, order_id="E9")
    assert ledger.client_order_id_for("E9") == "c1"
    assert ledger.client_order_id_for("nope") is None


def test_rerecord_empty_order_id_preserves_existing(ledger) -> None:  # noqa: ANN001
    _record(ledger, "c1", 10, order_id="E9")
    # presend 재시도(빈 order_id) → 기존 order_id 보존(역조회 유지).
    ledger.record_order(
        client_order_id="c1", order_id="", symbol="005930",
        side=OrderSide.SELL, currency=KRW, quantity=D(10),
    )
    assert ledger.client_order_id_for("E9") == "c1"


def test_apply_fill_regression_raises(ledger) -> None:  # noqa: ANN001
    ledger.apply_fill(_buy_fill("c1", qty=5, price=70_000, fee=0, order_id="o5"))
    ledger.apply_fill(_buy_fill("c1", qty=5, price=70_000, fee=0, order_id="o5"))  # 동일 — no-op.
    with pytest.raises(ValueError, match="감소"):  # 누적 감소 — desync 표면화.
        ledger.apply_fill(_buy_fill("c1", qty=3, price=70_000, fee=0, order_id="o5"))


def test_available_to_sell_subtracts_open(ledger) -> None:  # noqa: ANN001
    # 10주 보유(매수 체결) + 미체결 SELL 6주 기록 → 매도가능 4.
    ledger.apply_fill(_buy_fill("b1", qty=10, price=70_000, fee=0, order_id="B1"))
    ledger.record_order(
        client_order_id="s1", order_id="S1", symbol="005930",
        side=OrderSide.SELL, currency=KRW, quantity=D(6),
    )
    assert ledger.position_quantity("005930") == D(10)
    assert ledger.available_to_sell("005930") == D(4)


def test_reserve_sell_atomic_check_and_record(ledger) -> None:  # noqa: ANN001
    ledger.apply_fill(_buy_fill("b", qty=10, price=100, fee=0, order_id="B"))
    assert ledger.reserve_sell(
        client_order_id="s1", order_id="", symbol="005930", currency=KRW, quantity=D(6)
    ) is True
    assert ledger.available_to_sell("005930") == D(4)  # 등록돼 즉시 차감.
    assert ledger.reserve_sell(
        client_order_id="s2", order_id="", symbol="005930", currency=KRW, quantity=D(6)
    ) is False  # 잔여 4 < 6 → 거부(체크+등록 원자).


def test_reserve_sell_idempotent_retry(ledger) -> None:  # noqa: ANN001
    ledger.apply_fill(_buy_fill("b", qty=10, price=100, fee=0, order_id="B"))
    assert ledger.reserve_sell(
        client_order_id="s1", order_id="", symbol="005930", currency=KRW, quantity=D(10)
    ) is True
    # 같은 coid 재시도(재시작·ack 유실) — 자기 예약을 자신에게 못 빼므로 여전히 True(멱등).
    assert ledger.reserve_sell(
        client_order_id="s1", order_id="X1", symbol="005930", currency=KRW, quantity=D(10)
    ) is True
    assert ledger.available_to_sell("005930") == D(0)  # 이중계상 아님(여전히 10 예약).


# --- 평단 이동평균(G4): 라운드트립 후 basis -----------------------------------


def _xfill(side: OrderSide, qty: int, price: int, *, oid: str, at: str) -> Fill:
    return Fill(
        order_id=oid, client_order_id="", symbol="005930", side=side, currency=KRW,
        filled_quantity=D(qty), avg_price=D(price), commission=D(0), tax=D(0),
        filled_at=at, settlement_date="2026-06-20",
    )


def test_avg_basis_resets_on_full_close_rebuy(ledger) -> None:  # noqa: ANN001
    ledger.apply_fill(_xfill(OrderSide.BUY, 10, 100, oid="A", at="1"))
    ledger.apply_fill(_xfill(OrderSide.SELL, 10, 110, oid="B", at="2"))  # 완전 청산.
    ledger.apply_fill(_xfill(OrderSide.BUY, 10, 200, oid="C", at="3"))  # 재진입.
    pos = next(p for p in ledger.positions() if p.ticker == "005930")
    assert pos.quantity == D(10) and pos.avg_price == D(200)  # 현행 버그: 150.


def test_partial_sell_keeps_basis(ledger) -> None:  # noqa: ANN001
    ledger.apply_fill(_xfill(OrderSide.BUY, 10, 100, oid="A", at="1"))
    ledger.apply_fill(_xfill(OrderSide.SELL, 4, 110, oid="B", at="2"))
    pos = next(p for p in ledger.positions() if p.ticker == "005930")
    assert pos.quantity == D(6) and pos.avg_price == D(100)


# --- sqlite 원자성(G5-H): 중간 실패 시 롤백 ----------------------------------


def test_sqlite_apply_fill_atomic_on_error(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    import toss_sleeve.ledger as ledger_mod

    lg = _sqlite(tmp_path)
    lg.reserve("c1", D(700_000), KRW)
    lg.record_order(
        client_order_id="c1", order_id="o", symbol="005930",
        side=OrderSide.BUY, currency=KRW, quantity=D(10),
    )

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("injected mid-apply")

    monkeypatch.setattr(ledger_mod, "_terminal_order_status", boom)  # 마지막 statement 직전 폭발.
    with pytest.raises(RuntimeError):
        lg.apply_fill(_buy_fill("c1", qty=10, price=70_000, fee=0))
    monkeypatch.undo()

    # 원자성: 현금·예약 변경이 부분 적용되지 않고 전부 롤백.
    assert lg.virtual_cash(KRW) == D(1_000_000)
    assert lg.available(KRW) == D(1_000_000 - 700_000)  # 예약 700k 유지(삭제 롤백).
    lg.close()
