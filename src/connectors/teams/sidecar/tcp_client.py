"""TCP/TLS client for the Windows media sidecar.

Transport only — it moves framed messages and knows nothing about Graph, calls, or the
media platform, all of which live on the .NET side.

**Why TCP where Zoom uses a Unix socket.** Not a preference: Teams' app-hosted media
runs only on Windows, so the sidecar cannot be a sibling process on a shared volume
(doc 005 §2). The link crosses a host boundary, which makes two things mandatory that
Zoom's UDS gets for free — transport security, because meeting audio and a bearer token
now traverse a network, and ``TCP_NODELAY``, because Nagle would otherwise coalesce
20 ms audio frames into ~40 ms batches and quietly spend a fifth of the latency budget.

Backpressure policy matches Zoom's, for the same reason: **drop video, keep audio.** A
lost video frame costs one frame of smoothness; a lost audio frame is an audible gap.
Drops are counted, never silent.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from src.connectors.teams.exceptions import (
    SidecarFatalError,
    SidecarProtocolError,
    SidecarUnavailableError,
)
from src.connectors.teams.graph.models import SidecarError
from src.connectors.teams.sidecar.protocol import (
    TeamsFrameDecoder,
    TeamsMessage,
    TeamsMessageType,
    encode_json,
)
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_READ_CHUNK = 64 * 1024


def build_ssl_context(
    *,
    ca_file: Path | None = None,
    client_cert_file: Path | None = None,
    client_key_file: Path | None = None,
) -> ssl.SSLContext:
    """Build the client TLS context.

    ``ca_file`` pins the sidecar's issuer, which is the normal deployment: the Windows
    host carries an internally-issued certificate rather than a public one. Omitting it
    falls back to the system trust store.

    A client certificate turns this into mutual TLS, so the sidecar can reject any
    caller that is not this bridge. Without it the sidecar is authenticated but the
    bridge is not — tolerable on a private subnet, and the reason
    ``sidecar_client_cert_file`` exists for anywhere else.

    Raises:
        SidecarFatalError: a configured certificate file is unusable. Fatal rather
            than recoverable — retrying cannot fix a missing file, and a link that
            silently fell back to no client certificate would be worse than a
            refusal.
    """
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=str(ca_file) if ca_file else None
    )
    if client_cert_file is not None:
        try:
            context.load_cert_chain(
                certfile=str(client_cert_file),
                keyfile=str(client_key_file) if client_key_file else None,
            )
        except (OSError, ssl.SSLError) as exc:
            raise SidecarFatalError(
                "TLS_CLIENT_CERT", f"cannot load client certificate: {exc}"
            ) from exc
    return context


class TeamsSidecarClient:
    """Framed message channel to the Teams sidecar over TCP, optionally TLS."""

    __slots__ = (
        "_connected",
        "_decoder",
        "_host",
        "_port",
        "_reader",
        "_ssl",
        "_timeout_s",
        "_writer",
    )

    def __init__(
        self,
        *,
        host: str,
        port: int,
        connect_timeout_s: float = 20.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = connect_timeout_s
        self._ssl = ssl_context
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = TeamsFrameDecoder()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str:
        return f"{self._host}:{self._port}"

    async def connect(self) -> None:
        """Open the connection.

        Raises:
            SidecarUnavailableError: unreachable, refused, or timed out. Recoverable.
            SidecarFatalError: the TLS handshake failed. Not recoverable — a bad
                certificate chain will fail identically on every retry, so burning
                the reconnect budget on it only delays the real diagnosis.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host,
                    self._port,
                    ssl=self._ssl,
                    server_hostname=self._host if self._ssl else None,
                ),
                timeout=self._timeout_s,
            )
        except ssl.SSLCertVerificationError as exc:
            raise SidecarFatalError(
                "TLS_VERIFY", f"cannot verify sidecar certificate: {exc}"
            ) from exc
        except ssl.SSLError as exc:
            raise SidecarFatalError(
                "TLS_HANDSHAKE", f"TLS handshake failed: {exc}"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise SidecarUnavailableError(
                f"cannot connect to Teams sidecar at {self.endpoint}: {exc}"
            ) from exc

        _disable_nagle(writer)
        self._reader = reader
        self._writer = writer
        self._decoder.reset()
        self._connected = True
        logger.info("teams_sidecar.connected", endpoint=self.endpoint, tls=self._ssl is not None)

    async def close(self) -> None:
        """Close the connection. Idempotent."""
        self._connected = False
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, RuntimeError, ssl.SSLError):
            # Already-broken sockets are the normal case here: we are closing because
            # something failed. There is nothing left to salvage or report.
            pass

    async def send_raw(self, payload: bytes, *, drain: bool = True) -> None:
        """Send pre-encoded bytes.

        Args:
            drain: ``False`` skips the flush wait, which is how video frames avoid
                blocking on a saturated link.

        Raises:
            SidecarUnavailableError: the link is closed or the write failed.
        """
        writer = self._writer
        if writer is None:
            raise SidecarUnavailableError("Teams sidecar is not connected")
        try:
            writer.write(payload)
            if drain:
                await writer.drain()
        except (OSError, RuntimeError, ssl.SSLError) as exc:
            self._connected = False
            raise SidecarUnavailableError(f"Teams sidecar write failed: {exc}") from exc

    async def send_json(
        self, msg_type: TeamsMessageType, body: dict[str, Any], *, seq: int = 0
    ) -> None:
        """Send a control message."""
        await self.send_raw(encode_json(msg_type, body, seq=seq))

    def write_buffer_size(self) -> int:
        """Bytes queued in the transport, for backpressure decisions."""
        writer = self._writer
        if writer is None:
            return 0
        try:
            return int(writer.transport.get_write_buffer_size())
        except (AttributeError, NotImplementedError):  # pragma: no cover
            return 0

    async def messages(self) -> AsyncIterator[TeamsMessage]:
        """Yield messages until the link closes.

        Raises:
            SidecarUnavailableError: the read failed.
            SidecarProtocolError: framing desync.
        """
        reader = self._reader
        if reader is None:
            raise SidecarUnavailableError("Teams sidecar is not connected")

        while True:
            try:
                data = await reader.read(_READ_CHUNK)
            except (OSError, RuntimeError, ssl.SSLError) as exc:
                self._connected = False
                raise SidecarUnavailableError(f"Teams sidecar read failed: {exc}") from exc

            if not data:
                self._connected = False
                return  # EOF — the sidecar exited or the call ended

            for message in self._decoder.feed(data):
                yield message

    async def await_message(
        self, expected: TeamsMessageType, *, timeout_s: float
    ) -> TeamsMessage:
        """Wait for one specific message type, handling the traffic that arrives first.

        ``ERROR`` short-circuits: a rejected credential or an unconsented permission
        must fail the join immediately rather than waiting out a 60-second timeout and
        reporting it as one.

        Raises:
            SidecarFatalError: the sidecar reported ``fatal: true``.
            SidecarUnavailableError: timeout, EOF, framing desync, or transport failure.
        """

        async def _wait() -> TeamsMessage:
            async for message in self.messages():
                if message.msg_type is expected:
                    return message
                if message.msg_type is TeamsMessageType.ERROR:
                    error = SidecarError.model_validate(message.json())
                    if error.fatal:
                        raise SidecarFatalError(error.code, error.message)
                    logger.warning(
                        "teams_sidecar.error",
                        code=error.code,
                        message=error.message,
                        fatal=False,
                    )
                elif message.msg_type is TeamsMessageType.HEARTBEAT:
                    await self._echo_heartbeat(message)
            raise SidecarUnavailableError(
                f"Teams sidecar closed before sending {expected.name}"
            )

        try:
            return await asyncio.wait_for(_wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise SidecarUnavailableError(
                f"Teams sidecar did not send {expected.name} within {timeout_s}s"
            ) from exc
        except SidecarProtocolError as exc:
            raise SidecarUnavailableError(f"Teams sidecar framing error: {exc}") from exc

    async def _echo_heartbeat(self, message: TeamsMessage) -> None:
        """Echo the sender's timestamp so it can measure IPC round-trip."""
        body = message.json()
        await self.send_json(
            TeamsMessageType.HEARTBEAT, {"sent_at_us": body.get("sent_at_us", 0)}
        )


def _disable_nagle(writer: asyncio.StreamWriter) -> None:
    """Set ``TCP_NODELAY``.

    asyncio sets this by default for plain TCP but the guarantee does not hold once a
    TLS transport is layered on, and a silently-Nagled media link is an expensive thing
    to debug from latency graphs alone.
    """
    import socket

    sock = writer.transport.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:  # pragma: no cover - platform dependent
        logger.warning("teams_sidecar.nodelay_unavailable")
