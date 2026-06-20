"""TossConfig — 패키지 입력. 앱 전체 Settings 에 의존하지 않는다(디커플).

소비자는 자기 Settings(SecretStr 등)에서 평문을 꺼내 이 dataclass 로 주입한다. 패키지는 키를
로깅/echo 하지 않으며, repr 은 시크릿을 노출하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# production 단일 base(토스 샌드박스 없음). 소비자가 override 가능.
DEFAULT_BASE_URL = "https://openapi.tossinvest.com"


@dataclass(frozen=True, slots=True)
class TossConfig:
    client_id: str
    client_secret: str
    sleeve_id: str
    allocated_capital: int
    # 계좌 식별자(accountSeq, X-Tossinvest-Account 헤더). 비면 GET /api/v1/accounts 로 조회.
    account_seq: str | None = None
    base_url: str = DEFAULT_BASE_URL
    # 개인 보유분 보호 denylist(티커 집합). 봇이 매수후보로 고르는 것 차단.
    denylist: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id/client_secret 필수")
        if not self.sleeve_id:
            raise ValueError("sleeve_id 필수(전역 유일)")
        if self.allocated_capital < 0:
            raise ValueError(f"allocated_capital 은 음수일 수 없음: {self.allocated_capital}")

    def __repr__(self) -> str:
        # 시크릿 비노출.
        return (
            f"TossConfig(sleeve_id={self.sleeve_id!r}, account_seq={self.account_seq!r}, "
            f"allocated_capital={self.allocated_capital})"
        )
