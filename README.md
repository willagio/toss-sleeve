# toss-sleeve

Toss Securities Open API 브로커 + 공유계좌 **sleeve 가상현금 원장**을 담은 프로젝트-무관 패키지.

여러 자동매매 봇이 **하나의 토스증권 계좌를 공유**하면서 서로(그리고 개인 보유분)의 현금·포지션을
침범하지 않게 하는 프리미티브를 제공한다. 전략·스케줄링·알림 등 상위 로직은 소비자(앱) 책임이고,
이 패키지는 "주문 한 건 정확히 내고, sleeve 의 현금·포지션을 정직하게 추적"하는 것에만 집중한다.

설계 계약(상위 공용 문서): `TOSS_SHARED_ACCOUNT_PROTOCOL.md`.

## 무엇을 제공하나

- `toss_sleeve.TossBroker` — 발주/취소, BUY 전 `ledger.reserve()` 하드캡, SELL 전 `reserve_sell()`
  보유수량 가드, `clientOrderId` 멱등(409 흡수), `available_cash()` = **sleeve 가상현금**(계좌 전체
  예수금 아님).
- `toss_sleeve.SleeveLedger` — 포트(Protocol) + `InMemorySleeveLedger`(테스트·결정론),
  `SqliteSleeveLedger`(영속). 통화별 가상현금·예약·체결정산·T+2 미정산·외부 입출금.
- `toss_sleeve.TossAuth` / `TossRest` — OAuth2 client_credentials 토큰 발급·캐싱, 읽기 REST(계좌·보유·
  주문상세·시세·호가·주문가능현금).
- `toss_sleeve.RateLimiter` — 토스 카테고리별 TPS 슬라이딩 윈도우 제한.
- 자체 값 타입(`OrderRequest`/`OrderAck`/`Fill`/`BrokerPosition`/`Currency`/`PriceQuote`/…). **소비자 core
  타입에 의존하지 않는다** — seam 에서 얇게 변환한다(패키지 독립성). 모든 금액·가격·수량은 `Decimal`.

## 무엇을 제공하지 않나

전략·스크리닝·레짐·스케줄링·파이프라인·알림·대시보드는 **소비자(앱) 책임**이다. 청산(OCO) 실행도
소비자가 운전한다 — `place_oco()` 는 진입만 발주하고 `ExitPlan` 은 기록만 한다(브로커가 독자 발주하면
소비자 청산과 겹쳐 이중 매도가 난다).

## 핵심 보증(불변식)

머니를 다루므로 다음을 *런타임에* 강제한다(전부 테스트로 재현·검증):

- **하드캡** — BUY 는 발주 직전 `reserve()` 로 sleeve 가상현금을 키잉 예약한다. 초과면 발주조차 안 하고
  거부. 수량기반 시장가 매수는 예약액 산정 불가라 거부하고, 금액 매수는 `order_amount` 로 예약한다.
- **초과매도 방지** — SELL 은 `reserve_sell()` 로 *매도가능(보유 − 미체결 SELL 잔량)* 체크와 미체결 선등록을
  **한 트랜잭션**으로 처리한다. 공유계좌에서 남의 sleeve·개인 보유분이 팔리는 것을 막는다.
- **NAV 정직** — 체결 정산은 추정하지 않고 토스 주문상세 execution 의 **실제값**(체결가·수수료·세금)만
  쓴다. 필수 실행값이 누락/비정상이면 `Fill` 생성을 거부하고 표면화한다(0 으로 둔갑 금지).
- **멱등** — `client_order_id` 가 idempotency 키. `reserve()`/`reserve_sell()` 재시도는 누적이 아니라 set,
  `apply_fill()` 은 order_id 누적-스냅샷 델타로 중복 폴링에 무해(같은 수량이라도 늦은 수수료 보정은 반영).
- **원자성** — `SqliteSleeveLedger` 의 read-modify-write(예약·체결·입출금)는 `BEGIN IMMEDIATE` 로 묶여
  멀티프로세스에서도 직렬화된다.
- **에러 계약** — 일시적 전송 실패는 모두 `TossTransportError` 로 정규화된다(소비자가 흡수해 폴링 루프를
  살린다). 발주의 *전송 후* 불확정 실패는 `TossAmbiguousOrderError` 로 좁혀 reconcile 을 유도한다.

## 빠른 사용

```python
from decimal import Decimal
from toss_sleeve import (
    TossConfig, TossAuth, TossBroker, SqliteSleeveLedger,
    OrderRequest, OrderSide, OrderType, Currency, Ticker,
)

config = TossConfig(
    client_id=...,            # 앱 레이어에서 평문 주입(패키지는 키를 로깅/echo 하지 않음)
    client_secret=...,
    sleeve_id="gold-digger",  # 전역 유일
    allocated_capital=10_000_000,
    account_seq=...,          # 비우면 GET /accounts 로 조회
    denylist=frozenset({"005930"}),  # 개인 보유분 보호 — 매수후보 차단
)
ledger = SqliteSleeveLedger(sleeve_id="gold-digger", allocated_capital=10_000_000, path="sleeve.db")
auth = TossAuth(config)
broker = TossBroker(config, auth, ledger=ledger)

async with broker:
    ack = await broker.place(OrderRequest(
        ticker=Ticker("005930"), side=OrderSide.BUY, order_type=OrderType.LIMIT,
        currency=Currency.KRW, quantity=Decimal(10), price=Decimal(70_000),
        client_order_id="gd:001",   # 멱등 키
    ))
    if ack.accepted:
        ...  # 체결 폴링 루프가 order_fill → ledger.apply_fill 로 정산(아래 계약)
```

## 소비자 계약(중요)

이 패키지는 sleeve 의 *진실*을 들고 있을 뿐, 라이브 상태를 스스로 폴링하지 않는다. 소비자가 운전한다:

- **체결 폴링** — 토스는 웹소켓이 없다. 소비자가 `TossRest.order_fill(order_id)` 로 누적 체결 스냅샷을
  받아 `ledger.apply_fill(fill)` 을 호출해 가상현금을 정산한다. 멱등이라 중복 폴링·부분→완전 모두 안전.
- **종료 시 예약 해제** — 주문이 종료되면(완전체결/취소/거부) 예약이 해제돼야 한다. 완전체결은
  `apply_fill` 이 자동 처리하고, `broker.cancel(order_id)` 은 역조회로 해제한다. 그 외 종료(폴링으로 감지한
  REJECTED 등)는 소비자가 `ledger.mark_order_terminal(coid, OrderStatus.REJECTED)` 를 호출한다.
- **모호 착지 reconcile** — `TossAmbiguousOrderError`(전송 후 불확정) 또는 멱등키 미보유 409 를 받으면,
  소비자가 `clientOrderId` 로 토스에 주문 상태를 조회해 대조한다(예약은 그때까지 유지된다).
- **T+2 정산** — 매도 대금은 `settlement_date` 전까지 `available()` 에 들어가지 않는다(미정산 매도대금
  제외). `virtual_cash()` 에는 즉시 포함된다.
- **통화 버킷** — 계좌가 KRW/USD 현금을 분리 보유하므로 원장도 통화별이다. FX 환전은 `record_cashflow`
  −KRW/+USD 두 건으로 기록한다. NAV 의 통화 환산은 소비자(환율 endpoint) 책임.

## 동시성 모델

- 권장 형태는 **sleeve 당 한 프로세스/DB 연결**(단일 writer). 이 형태에서는 in-memory·sqlite 모두 안전.
- 그럼에도 `SqliteSleeveLedger` 의 하드캡·초과매도 가드(`reserve`/`reserve_sell`)와 정산(`apply_fill`)은
  `BEGIN IMMEDIATE` 로 직렬화돼, 같은 DB 파일을 여러 프로세스가 공유하는 경우에도 불변식이 유지된다
  (`busy_timeout` 으로 경합 시 대기). *스키마*만 패키지가 소유하고, DB 파일/연결은 소비자가 주입한다.

## 에러 모델

| 타입 | 의미 | 소비자 처리 |
|------|------|-------------|
| `TossError` | 인증/설정 오류(401/403), 업무 거부(400/422) 등 표면화 대상 | 표면화·중단 |
| `TossTransportError` | 일시적 전송/게이트웨이 실패(타임아웃·연결·5xx·429) | **흡수해 재시도** |
| `TossAmbiguousOrderError` | 발주/취소 *전송 후* 실패 — 토스 접수 여부 불확정 | reconcile |

업무 거부는 `accepted=False` 인 `OrderAck` 로 돌아오고(예외 아님), 전송장애·인증오류는 예외로 전파된다.

## 경계 결정

- **DB 인스턴스는 공유하지 않는다.** 패키지는 스키마(폼)만 소유하고, 각 소비자는 자기 DB 파일/연결을
  주입한다(프로젝트 독립성). money 는 TEXT(Decimal 문자열)로 저장해 부동소수 오차를 차단한다.
- **production 단일 환경**(토스 샌드박스 없음). 시크릿은 앱 레이어에서 관리하고 `TossConfig` 에 평문
  str 로 주입한다 — 패키지는 키를 로깅/echo/repr 하지 않는다.

## 설치(소비자)

```
pip install "toss-sleeve @ git+ssh://…@<tag>"
```

태그 고정 의존을 권장한다.

## 개발

```
uv sync --dev
uv run ruff check src/ tests/
uv run mypy src/                  # strict
uv run pytest -q                  # 단위(네트워크 0)
uv run pytest -m integration -s   # 실 자격증명 read-only 스모크(TOSS_CLIENT_ID/SECRET 필요)
```

라이브 폴링 캐던스 실측은 `tools/cadence_probe.py`(read-only) 참고.
