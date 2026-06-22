"""Toss 라이브 폴링 캐던스 실측 — READ-ONLY(주문 없음). 전면 Toss 전환의 viability 게이트.

토스는 웹소켓이 없어 라이브 시세를 REST 폴링으로 받아야 한다. 스캘핑이 도느냐는 "풀 전체 시세를
얼마나 자주 갱신할 수 있나"에 달렸다. 이 스크립트가 실 자격증명으로 그걸 *측정*한다:

  Phase A (last-price 배치): GET /prices?symbols=… 를 레이트리밋 한계로 N초간 반복. /prices 는 한 콜에
    최대 200심볼 배치라, 풀 크기와 무관하게 1콜로 전 종목 last-price 를 받는다 → 종목당 갱신주기 =
    폴 주기. MARKET_DATA 10/s 면 이론상 ~100ms.
  Phase B (호가 depth): GET /orderbook?symbol=X 는 종목당 1콜(배치 불가). N종목을 라운드로빈하면
    종목당 갱신주기 = N / 처리율. depth(bid/ask)가 트리거에 필요하면 이게 병목이다.

리포트: 달성 폴 Hz, 왕복 지연 p50/p95/max, 429 횟수, 종목당 실효 staleness. 이 수치로 DATA_STALL
(stale_after) 임계·종목수·전략 캐던스가 토스에서 성립하는지 판단한다.

실행:
  export TOSS_CLIENT_ID=… TOSS_CLIENT_SECRET=… [TOSS_ACCOUNT_SEQ=…]
  export TOSS_PROBE_SYMBOLS=005930,000660,…   # 미설정 시 기본 KR 대형주
  python tools/cadence_probe.py [--seconds 20] [--mps 10] [--orderbook]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path
from statistics import median

from toss_sleeve.api.auth import TossAuth
from toss_sleeve.api.rest import TossRest
from toss_sleeve.config import TossConfig
from toss_sleeve.ratelimit import RateLimiter

_DEFAULT_SYMBOLS = "005930,000660,035720,051910,006400,035420,028260,068270"


def _load_dotenv(path: str = ".env") -> None:
    """CWD 의 .env 를 읽어 os.environ 에 채운다(stdlib 만, 의존성 0). 이미 설정된 키는 덮지 않는다.

    KEY=VALUE 줄만 처리(주석/빈 줄 무시, 양끝 따옴표 제거). 시크릿은 출력하지 않는다.
    """
    f = Path(path)
    if not f.is_file():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _pct(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = min(len(s) - 1, int(q * len(s)))
    return s[idx]


def _config() -> TossConfig:
    cid = os.environ.get("TOSS_CLIENT_ID")
    secret = os.environ.get("TOSS_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수 필요")
    return TossConfig(
        client_id=cid,
        client_secret=secret,
        sleeve_id="cadence-probe",
        allocated_capital=0,
        account_seq=os.environ.get("TOSS_ACCOUNT_SEQ"),
    )


async def _phase_prices(rest: TossRest, symbols: list[str], seconds: float) -> None:
    print(f"\n=== Phase A: batch /prices ({len(symbols)} symbols, 1 call/poll) ===")
    latencies: list[float] = []
    errors = 0
    polls = 0
    # 서버측 실제 갱신율: 폴 Hz 와 무관하게 timestamp/lastPrice 가 *바뀐* 빈도. 토스가 공시 안 하는
    # 값이라 경험적으로 잰다 — 과다폴링(같은 값 반복 수신) 구간을 드러낸다.
    last_ts: dict[str, str] = {}
    last_px: dict[str, str] = {}
    ts_changes: dict[str, int] = {}
    px_changes: dict[str, int] = {}
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        t0 = time.monotonic()
        try:
            quotes = await rest.prices(symbols)
        except Exception as exc:  # noqa: BLE001 — 측정 중 어떤 실패든 집계만.
            errors += 1
            if errors <= 3:
                print(f"  err: {type(exc).__name__}: {exc}")
        else:
            latencies.append((time.monotonic() - t0) * 1000)
            for q in quotes:
                px = str(q.last_price)
                if q.symbol in last_ts and q.timestamp != last_ts[q.symbol]:
                    ts_changes[q.symbol] = ts_changes.get(q.symbol, 0) + 1
                if q.symbol in last_px and px != last_px[q.symbol]:
                    px_changes[q.symbol] = px_changes.get(q.symbol, 0) + 1
                last_ts[q.symbol] = q.timestamp
                last_px[q.symbol] = px
        polls += 1
    elapsed = time.monotonic() - start
    hz = polls / elapsed if elapsed else 0.0
    print(f"  polls={polls} in {elapsed:.1f}s  →  poll {hz:.1f} Hz (폴 간격 ≈ {1000 / hz if hz else 0:.0f}ms)")
    print(f"  latency ms: p50={median(latencies) if latencies else 0:.0f} "
          f"p95={_pct(latencies, 0.95):.0f} max={max(latencies) if latencies else 0:.0f}  errors={errors}")
    # 서버측 갱신율 — 종목별 timestamp/price 변화 Hz 의 분포. 폴 Hz 보다 *낮으면* 그 차이는 과다폴링.
    ts_hz = [c / elapsed for c in ts_changes.values()] if elapsed else []
    px_hz = [c / elapsed for c in px_changes.values()] if elapsed else []
    if ts_hz:
        print(f"  server timestamp 변화: median {median(ts_hz):.1f} Hz "
              f"(min {min(ts_hz):.1f} / max {max(ts_hz):.1f}) across {len(ts_hz)} symbols")
    if px_hz:
        print(f"  server lastPrice 변화: median {median(px_hz):.1f} Hz "
              f"(min {min(px_hz):.1f} / max {max(px_hz):.1f})")
    if ts_hz and hz:
        yield_pct = 100 * median(ts_hz) / hz
        print(f"  freshness yield ≈ {yield_pct:.0f}% (서버 갱신Hz / 폴Hz — 낮을수록 과다폴링)")


async def _phase_orderbook(rest: TossRest, symbols: list[str], seconds: float) -> None:
    print(f"\n=== Phase B: per-symbol /orderbook (round-robin {len(symbols)}) ===")
    latencies: list[float] = []
    errors = 0
    calls = 0
    start = time.monotonic()
    i = 0
    while time.monotonic() - start < seconds:
        sym = symbols[i % len(symbols)]
        i += 1
        t0 = time.monotonic()
        try:
            await rest.orderbook(sym)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if errors <= 3:
                print(f"  err: {type(exc).__name__}: {exc}")
        else:
            latencies.append((time.monotonic() - t0) * 1000)
        calls += 1
    elapsed = time.monotonic() - start
    rate = calls / elapsed if elapsed else 0.0
    per_symbol = len(symbols) / rate if rate else 0.0
    print(f"  calls={calls} in {elapsed:.1f}s  →  {rate:.1f}/s  "
          f"(종목당 호가 갱신주기 ≈ {per_symbol * 1000:.0f}ms for {len(symbols)} symbols)")
    print(f"  latency ms: p50={median(latencies) if latencies else 0:.0f} "
          f"p95={_pct(latencies, 0.95):.0f} max={max(latencies) if latencies else 0:.0f}")
    print(f"  errors={errors}")


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--mps", type=float, default=10.0, help="MARKET_DATA 초당 호출 상한")
    parser.add_argument("--orderbook", action="store_true", help="Phase B(호가 depth) 도 측정")
    args = parser.parse_args()

    _load_dotenv()  # CWD 의 .env 자동 로드(채워넣고 바로 실행).
    symbols = [s.strip() for s in os.environ.get("TOSS_PROBE_SYMBOLS", _DEFAULT_SYMBOLS).split(",") if s.strip()]
    config = _config()
    auth = TossAuth(config)
    # MARKET_DATA 그룹 한도로 limiter 구성(기본 10/s).
    rest = TossRest(config, auth, ratelimiter=RateLimiter(max_calls=int(args.mps), period=1.0))
    print(f"symbols={symbols}  seconds={args.seconds}  mps={args.mps}")
    try:
        await _phase_prices(rest, symbols, args.seconds)
        if args.orderbook:
            await _phase_orderbook(rest, symbols, args.seconds)
    finally:
        await rest.close()
        await auth.close()


if __name__ == "__main__":
    asyncio.run(_main())
