"""호가단위(tick size) 스냅 — 거래소가 주문가를 호가단위 배수로만 받는 제약을 소비자가 맞추는 헬퍼.

토스는 주문가가 호가단위에 안 맞으면 400 invalid-request("주문 가격이 호가 단위에 맞지
않습니다")로 거부한다. 전략의 비율 파생가(target=anchor×배수, stop)가 호가를 벗어나기 쉬우므로
*소비자가 발주 직전* 이 헬퍼로 스냅한다 — 패키지가 자동으로 가격을 바꾸지 않는다(주문 경제성을
호출자 몰래 바꾸지 않기 위함; 브로커는 받은 가격을 그대로 보내고 토스 응답을 충실히 전달).

KR(KRW)은 KRX 호가단위표(2023 개정), US(USD)는 센트(0.01) 단위. snap_down(매수)·snap_up(매도)로
*불리한 쪽으로 안* 스냅한다(매수는 의도가 이하, 매도는 의도가 이상).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from toss_sleeve.types import Currency

# (상한가_미만, 호가단위) 오름차순. 가격 < 상한가 인 첫 구간 채택. 500,000 이상은 _KRX_TOP.
_KRX_TICKS: Final[tuple[tuple[int, int], ...]] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)
_KRX_TOP: Final = 1_000
_USD_TICK: Final = Decimal("0.01")  # 미국주식 센트 단위.


def tick_size(price: Decimal, currency: Currency) -> Decimal:
    """가격·통화의 호가단위. USD 는 0.01 고정, KRW 는 가격대별 KRX 호가단위."""
    if currency is Currency.USD:
        return _USD_TICK
    p = int(price)
    for upper, size in _KRX_TICKS:
        if p < upper:
            return Decimal(size)
    return Decimal(_KRX_TOP)


def snap_down(price: Decimal, currency: Currency) -> Decimal:
    """호가단위로 내림 — 매수 지정가(의도가 이하로만 사 더 비싸게 안 사게)."""
    t = tick_size(price, currency)
    return (price // t) * t


def snap_up(price: Decimal, currency: Currency) -> Decimal:
    """호가단위로 올림 — 매도 지정가(의도가 이상으로만 팔 더 싸게 안 팔게)."""
    t = tick_size(price, currency)
    floored = (price // t) * t
    return floored if floored == price else floored + t
