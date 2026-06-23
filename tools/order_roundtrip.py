"""실 주문 왕복 검증 — 1주 지정가(−10%, 비-marketable) BUY → orderId 확인 → 즉시 취소.

지정가를 현재가 −10%로 깔아 시장보다 한참 아래에 *rest* 시킨다 → 정상적으론 체결 안 되는 실 주문
경로 검증: 실 OAuth·accountSeq·주문 POST 본문·clientOrderId·orderId 파싱·취소·하드캡 예약/환원.
(잔여 위험: 발주~취소 사이 ~1초에 시장이 10% 급락하면 체결될 수 있다 — 장중엔 사실상 불가하나
절대 보장은 아님. 진짜 체결→Execution→정산 검증은 별도 — marketable 1주를 본인이 09:00 이후 의도적으로.)

안전장치: TOSS_CONFIRM_REAL_ORDER=yes 가 *없으면* dry-run(가격·금액 계산만 출력, 발주 안 함).
실제로 쏘려면 그 env 를 명시해야 한다 — 실계좌·실돈이므로 오발 방지.

실행:
  cd /home/will/Repositories/toss-sleeve            # .env 자동 로드(cadence_probe 와 동일)
  PYTHONPATH=src python tools/order_roundtrip.py     # dry-run
  TOSS_CONFIRM_REAL_ORDER=yes PYTHONPATH=src python tools/order_roundtrip.py   # 실제 발주+취소

env:
  TOSS_VERIFY_SYMBOL   기본 035720(카카오, 저가 — 예약금 작게). 1주만 발주.
  TOSS_CLIENT_ID/SECRET/ACCOUNT_SEQ   (.env)
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from toss_sleeve import (
    Currency,
    InMemorySleeveLedger,
    OrderRequest,
    OrderSide,
    OrderType,
    Ticker,
    TossAmbiguousOrderError,
    TossAuth,
    TossBroker,
    TossConfig,
    TossRest,
    snap_down,
)

_KRW = Currency.KRW


def _load_dotenv(path: str = ".env") -> None:
    f = Path(path)
    if not f.is_file():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _config(allocated: int) -> TossConfig:
    cid = os.environ.get("TOSS_CLIENT_ID")
    secret = os.environ.get("TOSS_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 필요(.env)")
    return TossConfig(
        client_id=cid,
        client_secret=secret,
        sleeve_id="order-roundtrip",
        allocated_capital=allocated,
        account_seq=os.environ.get("TOSS_ACCOUNT_SEQ"),
    )


async def _main() -> None:
    _load_dotenv()
    symbol = os.environ.get("TOSS_VERIFY_SYMBOL", "035720")
    confirm = os.environ.get("TOSS_CONFIRM_REAL_ORDER") == "yes"

    # 현재가 조회 → 지정가 = 현재가 × 0.9 (−10%: ±30% 밴드 안, 시장보다 한참 아래라 체결 안 됨).
    probe_cfg = _config(allocated=1)
    auth = TossAuth(probe_cfg)
    rest = TossRest(probe_cfg, auth)
    try:
        quotes = await rest.prices([symbol])
        if not quotes:
            raise SystemExit(f"{symbol} 시세 조회 실패")
        # KRW 전용 — KRX 호가스냅·원화 예약·하드캡이 KRW 전제. USD 종목은 잘못된 통화로
        # 라이브 주문이 나가 하드캡을 우회하므로 거부한다(USD 검증은 별도 도구로).
        if quotes[0].currency is not _KRW:
            raise SystemExit(f"이 검증 도구는 KRW 전용 — {symbol} 은 {quotes[0].currency}. KRW 종목으로 재실행.")
        last = quotes[0].last_price
        raw = int((last * Decimal("0.9")).to_integral_value(rounding=ROUND_DOWN))
        limit = int(snap_down(Decimal(raw), _KRW))  # 호가단위 스냅(토스 400 invalid-request 회피).
        notional = limit  # 1주.
        print(f"symbol={symbol} last={int(last):,} → limit(−10% 호가스냅)={limit:,}  (1주, 예약 {notional:,}원)")

        if not confirm:
            print("\n[dry-run] TOSS_CONFIRM_REAL_ORDER=yes 미설정 — 실제 발주 안 함.")
            print("실제로 쏘려면: TOSS_CONFIRM_REAL_ORDER=yes 로 재실행.")
            return

        # 실제 발주 경로 — allocated_capital 을 1주 + 여유로 잡고 seam 없이 패키지 브로커로 직접.
        cfg = _config(allocated=notional + 10_000)
        ledger = InMemorySleeveLedger(sleeve_id="order-roundtrip", allocated_capital=notional + 10_000)
        broker = TossBroker(cfg, auth, ledger=ledger, rest=rest)
        coid = f"verify-roundtrip-{int(time.time())}"  # 매 실행 고유 — 재실행 시 409 중복 회피.
        req = OrderRequest(
            ticker=Ticker(symbol),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            currency=_KRW,
            quantity=Decimal(1),
            price=Decimal(limit),
            client_order_id=coid,
        )
        print("\n[발주] 1주 지정가 BUY (체결 안 될 가격)…")
        try:
            ack = await broker.place(req)
        except TossAmbiguousOrderError:
            # 전송 후 응답 실패 — 접수 여부 불확정. orderId 가 없어 자동 취소 불가.
            print(f"  ⚠ 모호한 발주 결과(clientOrderId={coid!r}) — 라이브 주문이 떠 있을 수 있다.")
            print("    토스 앱/주문내역에서 위 clientOrderId 를 확인하고 미체결이면 수동 취소할 것.")
            raise
        print(f"  ack: accepted={ack.accepted} order_id={ack.order_id!r} msg={ack.message!r}")
        print(f"  하드캡 예약 후 available={int(ledger.available(_KRW)):,}원")

        if not ack.accepted:
            print("  → 발주 거부(장 닫힘/검증 거부 등). 실 응답·에러 분류는 검증됨.")
            return

        # 접수된 순간부터 라이브 주문 — 이후 무슨 예외가 나도(체결조회 타임아웃 등) 반드시 취소.
        try:
            await asyncio.sleep(1.0)
            fill = await rest.order_fill(ack.order_id)
            print(f"  주문상세 체결: {fill.filled_quantity if fill else 0} (0 이어야 정상 — 체결 안 됨)")
        finally:
            print("[취소]…")
            cancelled = await broker.cancel(ack.order_id)
            print(f"  cancel: {cancelled}")
            print(f"  취소 후 available={int(ledger.available(_KRW)):,}원 (환원되면 원복)")
        print("\n[검증 완료] 실 주문 POST·orderId·체결조회·취소·하드캡 경로 동작 확인(체결 0 = 돈 안 나감).")
    finally:
        await rest.close()
        await auth.close()


if __name__ == "__main__":
    asyncio.run(_main())
