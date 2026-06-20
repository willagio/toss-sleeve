"""제네릭 슬라이딩 윈도우 레이트리밋 — 토스 카테고리별 TPS 준수.

토스 ORDER 6/s(개장피크 09:00–09:10 3/s)·ASSET 5/s·AUTH 5/s 등 그룹별 한도를 넘지 않게
acquire() 에서 대기시킨다. 시간/대기 함수 주입으로 단위테스트가 실시간 sleep 없이 결정론적.

주의(한 앱키 다중 프로젝트 공유): 이 limiter 는 *프로세스 단위*다. 같은 앱키를 여러 프로세스가
쓰면 cross-process burst 를 못 막는다 → 프로젝트별 앱키 발급을 권장한다(레이트리밋 예산 분리).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable


class RateLimiter:
    """`period` 초 동안 최대 `max_calls` 건 허용."""

    def __init__(
        self,
        max_calls: int,
        period: float = 1.0,
        *,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_calls <= 0:
            raise ValueError(f"max_calls 는 양수여야 함: {max_calls}")
        if period <= 0:
            raise ValueError(f"period 는 양수여야 함: {period}")
        self._max = max_calls
        self._period = period
        self._time = time_func
        self._sleep = sleep_func
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _evict(self, now: float) -> None:
        horizon = now - self._period
        while self._calls and self._calls[0] <= horizon:
            self._calls.popleft()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._time()
            self._evict(now)
            while len(self._calls) >= self._max:
                wait = self._calls[0] + self._period - now
                if wait > 0:
                    await self._sleep(wait)
                now = self._time()
                self._evict(now)
            self._calls.append(now)
