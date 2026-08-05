"""Zoom webhook endpoint.

Lives in the connector, not in ``api/``, because signature verification, payload shape,
and event names are all Zoom specifics. ``api/app.py`` mounts this router from the DI
container and so never imports a connector — the rule is enforced by
``tests/architecture/test_layering.py``.

Handles three events:

* ``endpoint.url_validation`` — Zoom's endpoint challenge
* ``meeting.rtms_started``    — bind ingest, or park it for a session yet to be created
* ``meeting.rtms_stopped``    — tear the session down
"""

from __future__ import annotations

from typing import Any

import orjson
from fastapi import APIRouter, Request, Response, status

from src.connectors.zoom.auth.webhook_verifier import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookVerifier,
)
from src.connectors.zoom.exceptions import WebhookVerificationError
from src.connectors.zoom.rtms.enums import (
    WEBHOOK_EVENT_RTMS_STARTED,
    WEBHOOK_EVENT_RTMS_STOPPED,
    WEBHOOK_EVENT_URL_VALIDATION,
)
from src.connectors.zoom.rtms.models import (
    RtmsStartedEvent,
    RtmsStoppedEvent,
    UrlValidationEvent,
)
from src.infrastructure.logging import get_logger
from src.services.meeting.service import MeetingService
from src.services.session.registry import PendingRtmsBinding

logger = get_logger(__name__)


def build_router(*, verifier: WebhookVerifier, meeting_service: MeetingService) -> APIRouter:
    """Build the Zoom webhook router.

    A factory rather than a module-level router so dependencies are injected instead of
    resolved from globals.
    """
    router = APIRouter(tags=["zoom-webhook"])

    @router.post("/", status_code=status.HTTP_200_OK, summary="Zoom webhook receiver")
    async def receive(request: Request) -> Response:
        # The RAW body is required: re-serialising parsed JSON changes whitespace and
        # key order, and the HMAC would never match.
        body = await request.body()

        try:
            payload: dict[str, Any] = orjson.loads(body)
        except orjson.JSONDecodeError:
            logger.warning("zoom.webhook.malformed_json")
            return _json_response({"error": "malformed json"}, status.HTTP_400_BAD_REQUEST)

        event = str(payload.get("event", ""))

        # The validation challenge is signed like any other event, so verify first and
        # uniformly — an unverified endpoint would let anyone complete Zoom's challenge.
        try:
            verifier.verify(
                body=body,
                signature=request.headers.get(SIGNATURE_HEADER),
                timestamp=request.headers.get(TIMESTAMP_HEADER),
            )
        except WebhookVerificationError as exc:
            # Never echo the received signature: a probe must learn nothing.
            logger.warning("zoom.webhook.rejected", zoom_event=event, reason=str(exc))
            return _json_response(
                {"error": "signature verification failed"}, status.HTTP_401_UNAUTHORIZED
            )

        if event == WEBHOOK_EVENT_URL_VALIDATION:
            validation = UrlValidationEvent.model_validate(payload)
            reply = verifier.build_url_validation_reply(validation.payload.plain_token)
            logger.info("zoom.webhook.url_validated")
            return _json_response(reply.as_dict(), status.HTTP_200_OK)

        if event == WEBHOOK_EVENT_RTMS_STARTED:
            return await _handle_started(payload, meeting_service)

        if event == WEBHOOK_EVENT_RTMS_STOPPED:
            return await _handle_stopped(payload, meeting_service)

        logger.info("zoom.webhook.ignored", zoom_event=event)
        return _json_response({"status": "ignored"}, status.HTTP_200_OK)

    return router


async def _handle_started(payload: dict[str, Any], service: MeetingService) -> Response:
    try:
        started = RtmsStartedEvent.model_validate(payload)
        signaling_url = started.payload.signaling_url()
    except ValueError as exc:
        logger.warning("zoom.webhook.rtms_started_malformed", error=str(exc))
        return _json_response({"error": "malformed payload"}, status.HTTP_400_BAD_REQUEST)

    binding = PendingRtmsBinding(
        meeting_uuid=started.payload.meeting_uuid,
        rtms_stream_id=started.payload.rtms_stream_id,
        signaling_url=signaling_url,
    )
    session = await service.bind_rtms(binding)

    if session is None:
        # Webhook won the race. Parked until a session is created (doc 003 §3.1).
        logger.info("zoom.webhook.rtms_parked", meeting_uuid=binding.meeting_uuid)
        return _json_response({"status": "parked"}, status.HTTP_202_ACCEPTED)

    return _json_response(
        {"status": "bound", "session_id": session.session_id}, status.HTTP_200_OK
    )


async def _handle_stopped(payload: dict[str, Any], service: MeetingService) -> Response:
    try:
        stopped = RtmsStoppedEvent.model_validate(payload)
    except ValueError as exc:
        logger.warning("zoom.webhook.rtms_stopped_malformed", error=str(exc))
        return _json_response({"error": "malformed payload"}, status.HTTP_400_BAD_REQUEST)

    session = await service.handle_rtms_stopped(stopped.payload.meeting_uuid)
    if session is None:
        logger.info("zoom.webhook.rtms_stopped_unknown", meeting_uuid=stopped.payload.meeting_uuid)
        return _json_response({"status": "unknown"}, status.HTTP_200_OK)

    return _json_response(
        {"status": "stopped", "session_id": session.session_id}, status.HTTP_200_OK
    )


def _json_response(body: dict[str, Any], status_code: int) -> Response:
    return Response(
        content=orjson.dumps(body), media_type="application/json", status_code=status_code
    )
