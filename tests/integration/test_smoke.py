"""Toss 통합 스모크 — READ-ONLY(주문 절대 발주 안 함). 기본 skip(@pytest.mark.integration).

토스는 production 단일(샌드박스 없음)이라 주문 왕복을 자동화하지 않는다 — 실계좌에 실주문이
나가기 때문. 대신 실 자격증명으로 읽기 3종(토큰→계좌→보유)의 실 응답 형태를 확인해 문서-기반
파싱 가정을 경험적으로 검증한다(IP 허용목록 필요).

실행(환경변수 TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 설정):
  pytest -m integration tests/integration/test_smoke.py -s
"""

from __future__ import annotations

import os

import pytest

from toss_sleeve.api.auth import TossAuth
from toss_sleeve.api.rest import TossRest
from toss_sleeve.config import TossConfig

pytestmark = pytest.mark.integration


async def test_toss_readonly_token_accounts_holdings() -> None:
    client_id = os.environ.get("TOSS_CLIENT_ID")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET")
    if not client_id or not client_secret:
        pytest.skip("TOSS_CLIENT_ID/SECRET 미설정 — 스모크 skip")

    config = TossConfig(
        client_id=client_id,
        client_secret=client_secret,
        sleeve_id="smoke",
        allocated_capital=0,
        account_seq=os.environ.get("TOSS_ACCOUNT_SEQ"),
    )
    auth = TossAuth(config)
    rest = TossRest(config, auth)
    try:
        token = await auth.token()
        assert token  # 토큰 값/prefix 는 출력하지 않는다(CI 로그·터미널 유출 방지).
        print("\n[toss] token issued OK")
        seq = await rest.account_seq()
        print(f"[toss] accountSeq={seq}")
        positions = await rest.holdings()
        print(f"[toss] holdings: {len(positions)} positions")
        for p in positions[:5]:
            print(f"  {p.ticker} qty={p.quantity} avg={p.avg_price}")
    finally:
        await rest.close()
        await auth.close()
