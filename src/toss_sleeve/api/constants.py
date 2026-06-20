"""Toss Open API 엔드포인트 경로 + 에러 모델 파싱.

토스 응답은 성공이면 ``{"result": ...}``, 실패면 ``{"error": {"code","message","requestId"}}`` 로
래핑되며 **업무 거부도 HTTP 4xx(400/422)** 로 온다. 그래서 브로커는 `code 유무`가 아니라 *HTTP
status* 로 업무거부/인증오류/전송장애를 가른다(broker.py). base URL 은 TossConfig.base_url.
"""

from __future__ import annotations

from typing import Final

import httpx

TOKEN_PATH: Final = "/oauth2/token"
ACCOUNTS_PATH: Final = "/api/v1/accounts"
HOLDINGS_PATH: Final = "/api/v1/holdings"
ORDERS_PATH: Final = "/api/v1/orders"
# MARKET_DATA — /prices 는 symbols 콤마구분 *배치*(최대 200), 응답 lastPrice/timestamp/currency.
# /orderbook 은 symbol 단건(bids/asks). 라이브 폴링 피드(웹소켓 없음)의 시세 소스.
PRICES_PATH: Final = "/api/v1/prices"
# /prices 배치 상한(콤마구분 심볼). 초과 풀은 호출측이 청크 분할한다.
PRICES_BATCH_MAX: Final = 200
ORDERBOOK_PATH: Final = "/api/v1/orderbook"
# 주문가능현금 — 정확 경로는 openapi.json 으로 재확인 필요(스펙 일부 truncated). available_cash 는
# sleeve 원장을 쓰므로 이 경로는 *보수적 2차 가드* 용도이고 hot-path 의존이 아니다.
BUYING_POWER_PATH: Final = "/api/v1/buying-power"


def cancel_path(order_id: str) -> str:
    return f"{ORDERS_PATH}/{order_id}/cancel"


def order_detail_path(order_id: str) -> str:
    return f"{ORDERS_PATH}/{order_id}"


# 업무 거부로 해석할 HTTP status(요청은 닿았으나 거부). 멱등 충돌(409)은 브로커가 별도 처리.
_BUSINESS_STATUSES: Final = frozenset({400, 422})


class TossError(RuntimeError):
    """Toss REST/OAuth 경계 오류.

    진단 필드: status(HTTP), code(error.code), msg(error.message), request_id(error.requestId).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        msg: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.msg = msg
        self.request_id = request_id
        super().__init__(_format(message, status, code, msg, request_id))


class TossTransportError(TossError):
    """전송 단계 실패(타임아웃·연결·5xx 게이트웨이·429). 소비자 엔진은 *이 타입만* 흡수해 루프를
    살린다. 인증/설정(401/403)·업무 거부(400/422)는 일반 TossError 로 남겨 표면화한다."""


class TossAmbiguousOrderError(TossTransportError):
    """발주/취소 POST 가 *전송 후* 실패(ReadTimeout·5xx)해 토스 접수 여부가 불확정인 전송 오류.
    진입 경로는 이 타입을 보고 잔고 대조 reconcile 을 한다. 연결 수립 실패(미발송 확실)는 일반
    TossTransportError 로 남긴다."""


def _format(
    message: str,
    status: int | None,
    code: str | None,
    msg: str | None,
    request_id: str | None,
) -> str:
    parts = [
        f"{label}={value}"
        for label, value in (
            ("status", status),
            ("code", code),
            ("msg", msg),
            ("request_id", request_id),
        )
        if value is not None
    ]
    return f"{message} [{' '.join(parts)}]" if parts else message


def _opt_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def json_or_none(resp: httpx.Response) -> object | None:
    try:
        body: object = resp.json()
    except ValueError:
        return None
    return body


def parse_toss(resp: httpx.Response, *, ambiguous: bool = False) -> object:
    """성공이면 ``result`` 페이로드 반환, 아니면 분류된 예외.

    5xx·429·비-JSON → TossTransportError(ambiguous 면 TossAmbiguousOrderError). 그 외 4xx →
    TossError(status·code·msg·request_id) — 브로커가 400/422 업무거부, 409 멱등, 401/403 표면화로 가른다.
    """
    body = json_or_none(resp)
    status = resp.status_code
    if httpx.codes.OK <= status < httpx.codes.MULTIPLE_CHOICES:
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body
    error = body.get("error") if isinstance(body, dict) else None
    code = msg = request_id = None
    if isinstance(error, dict):
        code = _opt_str(error.get("code"))
        msg = _opt_str(error.get("message"))
        request_id = _opt_str(error.get("requestId"))
    if request_id is None:
        request_id = _opt_str(resp.headers.get("x-request-id"))
    if (
        status >= httpx.codes.INTERNAL_SERVER_ERROR
        or status == httpx.codes.TOO_MANY_REQUESTS
        or body is None
    ):
        cls = TossAmbiguousOrderError if ambiguous else TossTransportError
        raise cls(
            "Toss 전송/게이트웨이 오류", status=status, code=code, msg=msg, request_id=request_id
        )
    raise TossError("Toss 응답 오류", status=status, code=code, msg=msg, request_id=request_id)


def is_business_rejection(exc: TossError) -> bool:
    """업무 거부(400/422)인가 — 브로커가 accepted=False 로 돌릴 대상. 전송장애·인증오류는 제외."""
    return not isinstance(exc, TossTransportError) and exc.status in _BUSINESS_STATUSES
