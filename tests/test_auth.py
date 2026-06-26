"""TossAuth 401 자가회복 — request_with_reauth(invalid-token 재인증·재시도) + invalidate. httpx 모킹.

토스가 서버측에서 토큰을 무효화하면(앱 로그인·재발급 등) 로컬 expires_at 은 아직 유효해 token()
이 죽은 토큰을 계속 내준다 → 401 반복. 이 회복 경로가 그걸 끊는다(invalidate → 재발급 → 1회 재시도).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from toss_sleeve.api.auth import TossAuth, _CachedToken, request_with_reauth
from toss_sleeve.api.constants import TossError
from toss_sleeve.config import TossConfig


def _cfg() -> TossConfig:
    return TossConfig(
        client_id="c", client_secret="s", sleeve_id="gd",
        allocated_capital=1_000_000, account_seq="7",
    )


class _FakeAuth:
    """auth_headers + invalidate 만 흉내 — 무효화 때 토큰 버전을 올려 재시도가 fresh 헤더를 받는지 검증."""

    def __init__(self) -> None:
        self.invalidate_calls = 0
        self._v = 0

    async def auth_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer t{self._v}"}

    async def invalidate(self) -> None:
        self.invalidate_calls += 1
        self._v += 1


def _invalid_token() -> httpx.Response:
    return httpx.Response(401, json={"error": {"code": "invalid-token", "message": "유효하지 않은 토큰"}})


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"result": {"ok": True}})


def _queue_send(responses: list[httpx.Response], seen: list[str]) -> Callable[[dict[str, str]], Awaitable[httpx.Response]]:
    async def send(headers: dict[str, str]) -> httpx.Response:
        seen.append(headers["authorization"])
        return responses.pop(0)

    return send


async def test_reauth_retries_on_invalid_token_then_succeeds() -> None:
    auth = _FakeAuth()
    seen: list[str] = []
    send = _queue_send([_invalid_token(), _ok()], seen)
    result = await request_with_reauth(auth, send)  # type: ignore[arg-type]
    assert result == {"ok": True}
    assert auth.invalidate_calls == 1            # 무효화 1회.
    assert seen == ["Bearer t0", "Bearer t1"]    # 재시도는 *fresh* 토큰으로(재발급 반영).


async def test_reauth_propagates_persistent_invalid_token() -> None:
    auth = _FakeAuth()
    seen: list[str] = []
    send = _queue_send([_invalid_token(), _invalid_token()], seen)
    with pytest.raises(TossError):
        await request_with_reauth(auth, send)  # type: ignore[arg-type]
    assert auth.invalidate_calls == 1            # 1회만 무효화·재시도 후 전파(무한 재시도 금지).


async def test_reauth_does_not_retry_non_token_401() -> None:
    # code != invalid-token 인 401(권한·자격증명 문제 등)은 재발급으로 못 고치므로 재인증 안 함.
    auth = _FakeAuth()
    seen: list[str] = []
    send = _queue_send([httpx.Response(401, json={"error": {"code": "unauthorized"}})], seen)
    with pytest.raises(TossError):
        await request_with_reauth(auth, send)  # type: ignore[arg-type]
    assert auth.invalidate_calls == 0            # 무효화 안 함.


async def test_invalidate_clears_memory_and_disk(tmp_path) -> None:  # noqa: ANN001
    auth = TossAuth(_cfg(), cache_path=tmp_path / "tok.json")
    auth._token = _CachedToken(access_token="dead", expires_at=auth._time() + 99_999)
    auth._write_disk_cache(auth._token)
    assert (tmp_path / "tok.json").exists()
    await auth.invalidate()
    assert auth._token is None
    assert auth._invalidated is True
    assert not (tmp_path / "tok.json").exists()  # 디스크도 지워야 다음 token() 이 재발급한다.


async def test_token_skips_disk_cache_when_invalidated(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    # invalidate 가 디스크 unlink 에 *실패*(read-only·잠금)해 죽은 토큰이 디스크에 남아도, _invalidated
    # 면 token() 이 그걸 안 읽고 재발급한다 — unlink 실패에도 401 회복이 보장된다.
    auth = TossAuth(_cfg(), cache_path=tmp_path / "tok.json")
    auth._write_disk_cache(_CachedToken(access_token="dead", expires_at=auth._time() + 99_999))
    auth._invalidated = True  # invalidate 후 unlink 실패로 죽은 디스크 토큰이 남은 상태.

    async def fake_issue() -> str:
        auth._invalidated = False
        return "fresh"

    monkeypatch.setattr(auth, "_issue_token", fake_issue)
    assert await auth.token() == "fresh"  # 디스크의 죽은 토큰을 무시하고 재발급.
