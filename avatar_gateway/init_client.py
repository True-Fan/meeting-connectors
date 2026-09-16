"""Client for the Initialisation control plane (``realtime-product``).

This is the piece that replaces LiveKit's own agent-dispatch-by-name: getting
``agent-worker`` into a room is no longer "ask LiveKit for a named worker," it is
"ask Initialisation for a session, and let Init -> Worker Handling assign a
registered worker pod." Mirrored from
``agent-worker/devtools/local_session.py`` (the reference client for that flow)
rather than imported — this process must not depend on that repo's source tree,
the same reason the avatar protocol constants in ``gateway.py`` are copied, not
imported, from the bridge.

One ``InitClient`` is shared by every ``BridgeSession`` in the process: a single
demo credential is minted once and reused (it carries its own validity window),
rather than once per meeting.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("avatar_gateway.init_client")

WAITING_POLL_ATTEMPTS = 15
WAITING_POLL_INTERVAL_S = 2.0


class InitError(Exception):
    """Init (or Worker Handling behind it) refused or could not complete a session."""


@dataclass(frozen=True)
class InitSession:
    session_id: str
    end_url: str


class InitClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        base_url: str,
        tenant_id: str,
        username: str,
        password: str,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._username = username
        self._password = password
        self._bearer: str | None = None
        self._credential_lock = asyncio.Lock()

    # -- credential ----------------------------------------------------------------

    async def _credential(self, *, force_refresh: bool = False) -> str:
        async with self._credential_lock:
            if self._bearer is not None and not force_refresh:
                return self._bearer

            # Idempotent — a user that already exists from a previous run just 4xxes
            # here, which is fine; only the credential call below has to succeed.
            await self._http.post(
                f"{self._base_url}/v1/users",
                json={
                    "tenant_id": self._tenant_id,
                    "username": self._username,
                    "password": self._password,
                },
            )
            response = await self._http.post(
                f"{self._base_url}/v1/credentials",
                json={
                    "username": self._username,
                    "password": self._password,
                    "validity_minutes": 60,
                },
            )
            if response.status_code >= 400:
                raise InitError(
                    f"POST /v1/credentials -> HTTP {response.status_code}: {response.text}"
                )
            body = response.json()
            token = body.get("access_token")
            if not token:
                raise InitError(f"credentials response carried no access_token: {body}")
            self._bearer = token
            return token

    # -- sessions --------------------------------------------------------------------

    async def create_session(
        self, *, room_name: str, agent_id: str, livekit_url: str, livekit_token: str
    ) -> InitSession:
        """POST a session and ride out `waiting`, same as ``local_session.py`` does.

        `input_type: "audio_stream"` matches what this gateway actually is — it
        streams live audio into the room itself, same as a real customer app would.
        """
        bearer = await self._credential()
        payload = {
            "input_type": "audio_stream",
            "livekit_url": livekit_url,
            "livekit_token": livekit_token,
            "metadata": {"room_name": room_name, "agent_id": agent_id},
        }
        session = await self._post_session(payload, bearer)

        # **The server has reserved a session by this point, and it costs the tenant a
        # slice of its concurrency cap whether or not it ever becomes ready.** So the
        # reservation is tracked from here — the moment it exists — rather than from the
        # point everything below it has succeeded.
        reserved = self._reservation(session)

        try:
            status = session.get("status")
            if status == "waiting":
                logger.info(
                    "session %s is waiting (%s) — polling",
                    session.get("session_id"),
                    session.get("reason"),
                )
                polling_url = session["polling_url"]
                for _ in range(WAITING_POLL_ATTEMPTS):
                    await asyncio.sleep(WAITING_POLL_INTERVAL_S)
                    response = await self._http.get(
                        polling_url, headers={"Authorization": f"Bearer {bearer}"}
                    )
                    if response.status_code < 400:
                        session = response.json()
                        status = session.get("status")
                        if status != "waiting":
                            break

            if status != "ready":
                raise InitError(
                    f"session {session.get('session_id')} is {status!r} "
                    f"({session.get('reason')}) — is agent-worker running and registered "
                    f"with Worker Handling? (curl localhost:8080/readyz)"
                )
        except BaseException:
            # **A reservation nobody ends occupies the cap for good.** A ``ready`` one
            # especially: the cap is a count of ready sessions, so a handful of abandoned
            # handshakes wedges the tenant permanently and every later join comes back as
            # ``waiting (concurrent_session_limit)`` — including the joins that would
            # otherwise have succeeded. Measured exactly that: five ready sessions against
            # a cap of five, every subsequent join blocked, and no recovery short of
            # ending them by hand.
            #
            # Each failure used to leak one, which made a transient shortage of workers
            # permanent. Releasing here is what keeps a failed handshake a failed
            # handshake rather than the end of the tenant's capacity.
            if reserved is not None:
                await self.end_session(reserved, reason="handshake_failed")
            raise

        if reserved is None:  # pragma: no cover - a ready session always carries an id
            raise InitError(f"session is ready but carried no session_id: {session}")
        return reserved

    def _reservation(self, session: dict) -> InitSession | None:
        """The handle needed to release a session, or ``None`` if there is nothing to
        release.

        Separated out because it is wanted *before* the session is known to be usable:
        the failure path needs exactly the same handle the success path returns.
        """
        session_id = session.get("session_id")
        if not session_id:
            return None
        return InitSession(
            session_id=session_id,
            end_url=f"{self._base_url}/v1/sessions/{session_id}/end",
        )

    async def _post_session(self, payload: dict, bearer: str) -> dict:
        response = await self._http.post(
            f"{self._base_url}/v1/tenants/{self._tenant_id}/sessions",
            json=payload,
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if response.status_code == 401:
            # The cached credential expired between meetings — mint one and retry once.
            bearer = await self._credential(force_refresh=True)
            response = await self._http.post(
                f"{self._base_url}/v1/tenants/{self._tenant_id}/sessions",
                json=payload,
                headers={"Authorization": f"Bearer {bearer}"},
            )
        if response.status_code >= 400:
            raise InitError(
                f"POST /v1/tenants/{self._tenant_id}/sessions -> "
                f"HTTP {response.status_code}: {response.text}"
            )
        return response.json()

    async def end_session(self, session: InitSession, *, reason: str = "bridge_disconnected") -> None:
        """Best-effort. A failure here must never block the bridge from tearing down."""
        try:
            bearer = await self._credential()
            response = await self._http.post(
                session.end_url,
                json={"reason": reason},
                headers={"Authorization": f"Bearer {bearer}"},
            )
            if response.status_code >= 400:
                logger.warning(
                    "POST %s -> HTTP %s: %s", session.end_url, response.status_code, response.text
                )
        except Exception as exc:
            logger.warning("failed to end init session %s: %s", session.session_id, exc)
