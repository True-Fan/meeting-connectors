"""Unix domain socket client for the publisher sidecar.

Speaks the frozen wire protocol from ``protocol.py`` (spec:
``docs/design/004-sidecar-ipc-protocol.md``). Transport only — no Zoom SDK knowledge,
which all lives on the C++ side.

Backpressure policy, per spec §6: if the socket cannot absorb writes, **drop video and
keep audio.** A lost video frame costs one frame of smoothness; a lost audio frame is
an audible gap. Drops are counted, never silent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from src.connectors.zoom.exceptions import SidecarFatalError, SidecarUnavailableError
from src.connectors.zoom.publisher.protocol import (
    SidecarFrameDecoder,
    SidecarMessage,
    SidecarMessageType,
    SidecarProtocolError,
    encode_json,
)
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_READ_CHUNK = 64 * 1024


class SidecarUdsClient:
    """Framed message channel to the sidecar over a Unix domain socket."""

    __slots__ = ("_connected", "_decoder", "_path", "_reader", "_timeout_s", "_writer")

    def __init__(self, *, uds_path: Path, connect_timeout_s: float = 15.0) -> None:
        self._path = uds_path
        self._timeout_s = connect_timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = SidecarFrameDecoder()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        """Open the socket.

        Raises:
            SidecarUnavailableError: the socket is absent or refused. Recoverable.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._path)), timeout=self._timeout_s
            )
        except (OSError, TimeoutError) as exc:
            raise SidecarUnavailableError(
                f"cannot connect to sidecar at {self._path}: {exc}"
            ) from exc

        self._reader = reader
        self._writer = writer
        self._decoder.reset()
        self._connected = True
        logger.info("sidecar.connected", path=str(self._path))

    async def close(self) -> None:
        """Close the socket. Idempotent."""
        self._connected = False
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, RuntimeError):
            pass

    async def send_raw(self, payload: bytes, *, drain: bool = True) -> None:
        """Send pre-encoded bytes.

        Args:
            drain: ``False`` skips the flush wait, which is how video frames avoid
                blocking on a saturated socket.

        Raises:
            SidecarUnavailableError: the socket failed.
        """
        writer = self._writer
        if writer is None:
            raise SidecarUnavailableError("sidecar is not connected")
        try:
            writer.write(payload)
            if drain:
                await writer.drain()
        except (OSError, RuntimeError) as exc:
            self._connected = False
            raise SidecarUnavailableError(f"sidecar write failed: {exc}") from exc

    async def send_json(
        self, msg_type: SidecarMessageType, body: dict[str, Any], *, seq: int = 0
    ) -> None:
        """Send a control message."""
        await self.send_raw(encode_json(msg_type, body, seq=seq))

    def write_buffer_size(self) -> int:
        """Bytes queued in the transport, for backpressure decisions."""
        writer = self._writer
        if writer is None:
            return 0
        transport = writer.transport
        try:
            return int(transport.get_write_buffer_size())
        except (AttributeError, NotImplementedError):  # pragma: no cover
            return 0

    async def messages(self) -> AsyncIterator[SidecarMessage]:
        """Yield messages from the sidecar until the socket closes.

        Raises:
            SidecarProtocolError: framing desync. Fatal by design — a desynced binary
                stream cannot be realigned with confidence (spec §6).
        """
        reader = self._reader
        if reader is None:
            raise SidecarUnavailableError("sidecar is not connected")

        while True:
            try:
                data = await reader.read(_READ_CHUNK)
            except (OSError, RuntimeError) as exc:
                self._connected = False
                raise SidecarUnavailableError(f"sidecar read failed: {exc}") from exc

            if not data:
                self._connected = False
                return  # EOF — sidecar exited

            for message in self._decoder.feed(data):
                yield message

    async def await_message(
        self, expected: SidecarMessageType, *, timeout_s: float
    ) -> SidecarMessage:
        """Wait for one specific message type.

        ``ERROR`` short-circuits: a fatal error must fail the join immediately rather
        than waiting out the timeout.

        Raises:
            SidecarFatalError: the sidecar reported ``fatal: true``.
            SidecarUnavailableError: timeout, EOF, or a transport failure.
        """

        async def _wait() -> SidecarMessage:
            async for message in self.messages():
                if message.msg_type is expected:
                    return message
                if message.msg_type is SidecarMessageType.ERROR:
                    body = message.json()
                    code = str(body.get("code", "UNKNOWN"))
                    detail = str(body.get("message", ""))
                    if bool(body.get("fatal", False)):
                        raise SidecarFatalError(code, detail)
                    logger.warning("sidecar.error", code=code, message=detail, fatal=False)
                elif message.msg_type is SidecarMessageType.HEARTBEAT:
                    await self._echo_heartbeat(message)
            raise SidecarUnavailableError(f"sidecar closed before sending {expected.name}")

        try:
            return await asyncio.wait_for(_wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise SidecarUnavailableError(
                f"sidecar did not send {expected.name} within {timeout_s}s"
            ) from exc
        except SidecarProtocolError as exc:
            raise SidecarUnavailableError(f"sidecar framing error: {exc}") from exc

    async def _echo_heartbeat(self, message: SidecarMessage) -> None:
        """Echo the sender's timestamp so it can measure IPC round-trip (spec §5.5)."""
        body = message.json()
        await self.send_json(
            SidecarMessageType.HEARTBEAT, {"sent_at_us": body.get("sent_at_us", 0)}
        )
