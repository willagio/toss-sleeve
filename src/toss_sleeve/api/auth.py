"""Toss 인증 — OAuth2 client_credentials access token 발급·캐싱.

표준 client_credentials grant(POST /oauth2/token, application/x-www-form-urlencoded). 응답
``expires_in``(초)로 TTL 을 잡아 만료 직전 재발급 충돌을 피한다. 자격증명은 TossConfig 에서
평문으로 받으며 로깅/echo 하지 않는다(repr 비노출).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from toss_sleeve.api.constants import TOKEN_PATH, TossError, TossTransportError, parse_toss
from toss_sleeve.config import TossConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# 만료 여유(초). expires_in 에서 이만큼 일찍 만료로 보고 갱신해 경계 충돌을 피한다.
TOKEN_REFRESH_MARGIN_SECONDS = 300

# 토스가 서버측에서 토큰을 무효화했을 때의 401 error.code — request_with_reauth 가 이 코드에만
# 재인증·재시도한다(다른 401 은 재발급으로 못 고치므로 전파). 실측 응답 기준.
INVALID_TOKEN_CODE = "invalid-token"


@dataclass(frozen=True, slots=True)
class _CachedToken:
    access_token: str
    expires_at: float  # epoch seconds


def _key_fingerprint(client_id: str) -> str:
    """client_id 의 비밀 비노출 식별자(sha256 앞 12자리) — 캐시 파일명용. 원문 비노출."""
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:12]


def _default_cache_path(key_fp: str) -> Path:
    import os

    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "toss-sleeve" / f"token-{key_fp}.json"


class TossAuth:
    """Toss OAuth2 client_credentials 토큰 발급·캐싱.

    httpx.AsyncClient 미주입 시 base_url 로 지연 생성(소유 시 close). time_func 주입으로 TTL/캐시
    만료를 단위테스트에서 결정론적으로 검증한다.
    """

    def __init__(
        self,
        config: TossConfig,
        *,
        client: httpx.AsyncClient | None = None,
        cache_path: Path | None = None,
        time_func: "Callable[[], float]" = time.time,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._cache_path = cache_path or _default_cache_path(_key_fingerprint(config.client_id))
        self._time = time_func
        self._token: _CachedToken | None = None
        # invalidate 직후 디스크 캐시 재읽기 방지 플래그 — unlink 가 실패해도(read-only 캐시 등)
        # 다음 token() 이 죽은 디스크 토큰을 다시 읽지 않고 재발급하게 한다. _issue_token 이 해제.
        self._invalidated = False
        # 콜드 스타트 동시 token() 호출의 중복 발급(AUTH 레이트리밋)을 락으로 직렬화.
        self._issue_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"TossAuth(sleeve_id={self._config.sleeve_id!r})"

    @property
    def config(self) -> TossConfig:
        return self._config

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._config.base_url)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> TossAuth:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def auth_headers(self) -> dict[str, str]:
        """공통 인증 헤더(bearer). 계좌범위 헤더(X-Tossinvest-Account)는 호출측이 덧붙인다."""
        token = await self.token()
        return {"authorization": f"Bearer {token}"}

    async def token(self) -> str:
        """유효한 access token 반환. 메모리→디스크→재발급 순으로 캐시 활용."""
        cached = self._token
        if cached is not None and self._time() < cached.expires_at:
            return cached.access_token

        async with self._issue_lock:
            now = self._time()
            if self._token is not None and now < self._token.expires_at:
                return self._token.access_token
            # invalidate 직후엔 디스크 캐시(아직 안 지워졌을 수 있는 죽은 토큰)를 건너뛰고 재발급한다.
            if not self._invalidated:
                disk = self._read_disk_cache()
                if disk is not None and now < disk.expires_at:
                    self._token = disk
                    return disk.access_token
            return await self._issue_token()

    async def _issue_token(self) -> str:
        client = self._get_client()
        try:
            resp = await client.post(
                TOKEN_PATH,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                },
            )
        except httpx.HTTPError as exc:
            # 전송 실패(연결·읽기 타임아웃)도 TossTransportError 로 — 모든 REST 가 auth 를 거치므로 누수 금지.
            raise TossTransportError("토큰 발급 전송 실패") from exc
        # 429/5xx·비-JSON 은 parse_toss 가 TossTransportError 로 분류 → 소비자가 흡수해 재시도 가능.
        data = parse_toss(resp)
        if not isinstance(data, dict) or "access_token" not in data:
            raise TossError("토큰 발급 실패: access_token 없음", status=resp.status_code)
        expires_in = _to_float(data.get("expires_in"), default=86400.0)
        ttl = max(0.0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
        cached = _CachedToken(
            access_token=str(data["access_token"]), expires_at=self._time() + ttl
        )
        self._token = cached
        self._invalidated = False  # fresh 토큰 발급·기록 — 다시 디스크 캐시를 신뢰해도 된다.
        self._write_disk_cache(cached)
        return cached.access_token

    async def invalidate(self) -> None:
        """캐시(메모리+디스크)된 토큰을 버려 다음 token() 이 재발급하게 한다.

        토스가 토큰을 *서버측에서* 무효화하면(앱 로그인·재발급 등) 로컬 expires_at 은 아직 유효해
        token() 이 그 죽은 토큰을 계속 내준다 → 401 무한반복. 401 을 만난 호출측이 이걸 불러 재발급
        경로를 강제한다. 디스크 캐시도 지워야 한다(메모리만 비우면 다음 token() 이 디스크의 같은
        죽은 토큰을 다시 읽는다). _issue_lock 으로 직렬화해 재발급과 경합하지 않는다.
        """
        async with self._issue_lock:
            self._token = None
            # 디스크 unlink 가 실패해도(read-only·잠금) 다음 token() 이 죽은 디스크 토큰을 다시
            # 읽지 않게 플래그를 세운다 — 재발급으로 강제 수렴(_issue_token 이 해제).
            self._invalidated = True
            try:
                self._cache_path.unlink()
            except (FileNotFoundError, OSError):
                pass

    def _read_disk_cache(self) -> _CachedToken | None:
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw)
            return _CachedToken(
                access_token=data["access_token"], expires_at=float(data["expires_at"])
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _write_disk_cache(self, token: _CachedToken) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(
                    {"access_token": token.access_token, "expires_at": token.expires_at}
                ),
                encoding="utf-8",
            )
            try:
                self._cache_path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass  # 디스크 캐시는 최적화 — 실패해도 메모리 캐시로 동작.


def _to_float(value: object, *, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


async def request_with_reauth(
    auth: TossAuth,
    send: Callable[[dict[str, str]], Awaitable[httpx.Response]],
    *,
    ambiguous: bool = False,
) -> object:
    """authed 요청을 보내고 401 이면 토큰을 무효화·재발급해 *1회* 재시도한다.

    send(base_headers)→Response: base_headers 는 auth.auth_headers()(bearer)이고, 계좌 헤더
    (X-Tossinvest-Account) 추가와 httpx 오류→TossTransportError 정규화는 호출측 클로저가 한다(여기선
    인증 재시도만). 401(invalid-token 포함)은 토스 *서버측* 토큰 무효화이므로(앱 로그인·재발급 등 →
    로컬 expires_at 은 아직 유효해 죽은 토큰을 계속 내준다) invalidate 후 fresh 토큰으로 한 번 더
    보낸다 — 401 은 요청이 인증단계에서 *거부*된 것이라 부작용이 없어 재전송이 안전하다(발주 멱등키와
    무관). 재시도도 401 이면(자격증명 자체 문제 등) 전파한다. 전송장애(TossTransportError)·업무거부
    (400/422)는 재인증 대상이 아니라 그대로 전파한다(send 단계 전송장애는 try 밖이라 자연 전파).

    free 함수로 둬 TossRest·TossBroker 가 같은 인증 재시도를 공유하고, 단위테스트는 fake auth(
    auth_headers 만 가진)로 happy-path 를 그대로 돌린다(401 분기에서만 invalidate 를 부른다).
    """
    headers = await auth.auth_headers()
    resp = await send(headers)
    try:
        return parse_toss(resp, ambiguous=ambiguous)
    except TossError as exc:
        # *토큰 무효화* 401(code=invalid-token)만 재인증 대상이다. 다른 401(자격증명·권한 문제 등)은
        # 재발급으로 못 고치고 무한 재시도 위험이라 그대로 전파한다(전송장애·업무거부도 마찬가지).
        if exc.status != httpx.codes.UNAUTHORIZED or exc.code != INVALID_TOKEN_CODE:
            raise
        await auth.invalidate()
        headers = await auth.auth_headers()
        resp = await send(headers)
        return parse_toss(resp, ambiguous=ambiguous)
