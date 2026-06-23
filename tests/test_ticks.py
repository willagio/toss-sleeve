"""호가단위 스냅 — KRW(KRX 밴드)·USD(센트). 거래소 off-tick 거부 회피용 소비자 헬퍼."""

from __future__ import annotations

from decimal import Decimal

from toss_sleeve import Currency, snap_down, snap_up, tick_size

D = Decimal
KRW = Currency.KRW
USD = Currency.USD


def test_krw_tick_size_by_band() -> None:
    assert tick_size(D(1_500), KRW) == D(1)        # <2,000
    assert tick_size(D(70_000), KRW) == D(100)     # 50k–200k
    assert tick_size(D(318_600), KRW) == D(500)    # 200k–500k
    assert tick_size(D(700_000), KRW) == D(1_000)  # 500k+


def test_krw_snap_down_up() -> None:
    # 318,600 (호가 500) → 내림 318,500 / 올림 319,000.
    assert snap_down(D(318_600), KRW) == D(318_500)
    assert snap_up(D(318_600), KRW) == D(319_000)
    # 이미 정렬된 가격은 양쪽 다 그대로.
    assert snap_down(D(70_000), KRW) == D(70_000)
    assert snap_up(D(70_000), KRW) == D(70_000)


def test_krw_band_boundary() -> None:
    # 199,950 (<200k, 호가 100) 올림 → 200,000(200k 구간 시작, 500의 배수라 유효).
    assert snap_up(D(199_950), KRW) == D(200_000)


def test_usd_cents() -> None:
    assert tick_size(D("25.629803"), USD) == D("0.01")
    assert snap_down(D("25.629803"), USD) == D("25.62")
    assert snap_up(D("25.629803"), USD) == D("25.63")
    assert snap_down(D("25.63"), USD) == D("25.63")  # 정렬됨.
