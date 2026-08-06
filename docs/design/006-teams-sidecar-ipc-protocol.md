# Teams Sidecar IPC Protocol — Wire Version 1

**Status:** Implemented. Python codec is the reference; the .NET codec must match byte for byte.
**Reference implementation:** `src/connectors/teams/sidecar/protocol.py`
**Counterpart:** `src/connectors/teams/sidecar/dotnet/Wire/WireProtocol.cs`
**Conformance vector:** `tests/unit/test_teams_sidecar_protocol.py` — **this is the contract**
**Architecture:** [005](./005-teams-connector-architecture.md)
**Date:** 2026-08-06

---

## 1. Why this is not Zoom's protocol

Doc 004 is a **frozen** wire spec already deployed against a C++ binary in production. It is
shaped precisely for its job: unidirectional media, over a Unix domain socket, on one host.
Teams differs in every one of those dimensions:

| | Zoom (doc 004) | Teams (this doc) |
|---|---|---|
| Media direction | Bridge → sidecar only | **Bidirectional** — participant audio arrives *up* this link |
| Transport | Unix domain socket, same container | TCP + TLS, **across a host boundary** |
| Join credential | Zoom SDK JWT + meeting number | Azure AD client credentials + Graph join descriptor |
| Audio attribution | On the separate RTMS leg | **On this link** — unmixed buffers carry a media source id |
| Video format | I420 straight through | I420 in, converted to NV12 in the sidecar |
| Roster | Not carried | `ROSTER` message |

Sharing one codec would mean unfreezing a production contract to add fields only Teams uses,
coupling Zoom's release cycle to Teams'. The cost of keeping them separate is a few hundred
lines of similar-looking framing; the benefit is that a Teams change cannot break Zoom.
`tests/architecture/test_layering.py` asserts neither codec is imported by the other
connector.

The two use **different magic numbers** (`TMC1` vs `ZMC1`) so that pointing a bridge at the
wrong sidecar fails on the first frame with a named error rather than decoding plausible
garbage.

## 2. Change policy

Additive changes only within version 1: new message types, new optional JSON keys. Anything
that moves a byte or changes a field's meaning requires a new `WIRE_VERSION` and a new
document. A decoder rejects an unknown version rather than guessing.

Both ends must ignore JSON keys they do not recognise.

---

## 3. Frame layout

Every frame is a 24-byte header followed by `payload_len` bytes. **All integers are
big-endian.** .NET is little-endian natively, which is why `WireProtocol.cs` shuffles bytes
explicitly instead of using `BitConverter` — the conformance test exists largely to catch
that mistake.

```
 offset  size  field         type    notes
 ------  ----  ------------  ------  --------------------------------------------
 0       4     magic         uint32  0x544D4331 — ASCII 'TMC1'
 4       1     version       uint8   1
 5       1     type          uint8   §4
 6       1     flags         uint8   §5, advisory only
 7       1     reserved      uint8   must be 0; ignored on receipt
 8       4     seq           uint32  per-type, wraps at 2^32
 12      8     pts_us        int64   microseconds, sender's clock (§7)
 20      4     payload_len   uint32  ≤ 8388608 (8 MiB)
 24      …     payload
```

`MAX_PAYLOAD_BYTES` is 8 MiB: a 1080p I420 frame is ~3.1 MB, so this leaves headroom while
stopping a corrupt length field from causing a gigabyte allocation.

---

## 4. Message types

| Value | Name | Direction | Payload |
|---|---|---|---|
| `0x01` | `VIDEO_I420` | bridge → sidecar | video sub-header + packed I420 planes |
| `0x02` | `AUDIO_PCM` | **both** | audio sub-header + PCM |
| `0x03` | `CONTROL_JOIN` | bridge → sidecar | JSON (§6.1) |
| `0x04` | `CONTROL_LEAVE` | bridge → sidecar | JSON `{"reason": str}` |
| `0x05` | `HEARTBEAT` | **both** | JSON `{"sent_at_us": int}` |
| `0x06` | `READY` | sidecar → bridge | JSON (§6.2) |
| `0x07` | `ERROR` | sidecar → bridge | JSON (§6.3) |
| `0x08` | `ROSTER` | sidecar → bridge | JSON (§6.4) |
| `0x09` | `CALL_STATE` | sidecar → bridge | JSON (§6.5) |

`AUDIO_PCM` is one type in both directions because the payload is identical and the direction
is implied by which end received it.

## 5. Flags

Advisory. **Never load-bearing for decoding** — a decoder that needs a flag to parse a frame
has a design bug.

| Bit | Name | Meaning |
|---|---|---|
| `0x01` | `KEYFRAME` | Video frame is independently displayable. Always true for raw frames. |
| `0x02` | `UNMIXED` | Sidecar → bridge audio came from an unmixed buffer, so `source_msi` is meaningful. |
| `0x04` | `SILENCE` | Payload is digital silence. Diagnostics only — it is still sent, because the media platform needs a continuous cadence. |

---

## 6. Control payloads

JSON, UTF-8, no whitespace, **keys sorted**. Determinism is what lets the conformance vector
be a literal. `null` values are omitted rather than serialised: the sidecar switches on key
*presence* for the optional passcode, and an explicit `null` would be forwarded to Graph as
an empty passcode rather than as "no passcode".

### 6.1 `CONTROL_JOIN` (bridge → sidecar)

```json
{
  "sessionId": "ses_...",
  "correlationId": "cor_...",
  "join": {
    "mode": "meeting_id",
    "tenantId": "<guid>",
    "displayName": "AI Avatar",
    "joinMeetingId": "123456789012",
    "passcode": "abc123"
  },
  "auth": {
    "tenantId": "<guid>",
    "clientId": "<guid>",
    "clientSecret": "<secret>"
  },
  "audio": { "sampleRateHz": 16000, "channels": 1, "unmixed": true },
  "video": { "width": 1280, "height": 720, "fps": 30 }
}
```

`join.mode` is `"meeting_id"` or `"chat_info"`. The `chat_info` route replaces
`joinMeetingId`/`passcode` with:

```json
  "chatInfo": { "threadId": "19:meeting_...@thread.v2", "messageId": "0" },
  "organizer": { "id": "<guid>", "tenantId": "<guid>" }
```

Field names are camelCase because they pass through to Graph unrenamed.

**Credentials travel per join.** The Windows host is provisioned with infrastructure only, so
rotating a client secret needs no Windows deployment and a compromised host yields no durable
credential (doc 005 §5.2).

### 6.2 `READY` (sidecar → bridge)

Sent once the Graph call is established and media is negotiated.

```json
{
  "callId": "...",
  "wireVersion": 1,
  "audioSampleRateHz": 16000,
  "audioChannels": 1,
  "unmixedAudio": true,
  "videoWidth": 1280,
  "videoHeight": 720,
  "videoFps": 30,
  "selfMsi": 4242,
  "sdkVersion": "1.2.0.0"
}
```

The bridge **verifies the negotiated values against what it requested** and fails the session
on a mismatch. A silent rate mismatch produces pitch-shifted speech and a geometry mismatch
produces a garbled frame — both read as avatar bugs and are expensive to trace across a host
boundary. A downgrade of `unmixedAudio` is *not* fatal: it logs loudly and `EchoGuard` falls
back to its speaking gate.

`selfMsi` is optional; when absent the bot's own identity arrives later via `ROSTER`.

### 6.3 `ERROR` (sidecar → bridge)

```json
{ "code": "GRAPH_403", "message": "...", "fatal": true }
```

`fatal` is the field that matters. `true` means retrying cannot help — a rejected credential,
missing `Calls.AccessMedia.All` consent, an uninitialisable media platform — and the bridge
fails the session immediately rather than spending ten reconnect attempts to arrive at the
same place. `false` degrades the leg and lets backoff run.

### 6.4 `ROSTER` (sidecar → bridge)

```json
{ "participants": [ { "msi": 4242, "displayName": "AI Avatar", "isSelf": true } ] }
```

`msi` is the media source id — the identifier unmixed audio buffers are tagged with, and
therefore the only one a frame can be matched against. `isSelf` marks the bot's own entry and
is how `EchoGuard` learns the identity to filter.

### 6.5 `CALL_STATE` (sidecar → bridge)

```json
{ "state": 2, "reason": null }
```

`1` establishing · `2` established · `3` terminating · `4` terminated. `TERMINATED` degrades
the link rather than failing it: the meeting may simply have ended.

---

## 7. Timestamps

`pts_us` is on the **sender's** clock and the two ends' clocks are never mixed.

Inbound audio is re-stamped by `ingest/mapping.py` onto the bridge's `MediaClock` on receipt.
Two machines' clock offset must not leak into A/V sync, and the bridge's single-clock
invariant is what the pacer depends on. The wire value is kept for latency attribution only.

The sidecar likewise stamps outgoing media with `DateTime.UtcNow.Ticks`, which is what the
media platform expects, and does not reuse the bridge's `pts_us`.

---

## 8. Media sub-headers

### 8.1 `AUDIO_PCM` — 12 bytes, then PCM

```
 offset  size  field           notes
 0       4     sample_rate_hz  uint32
 4       1     channels        uint8   1 for both directions today
 5       1     sample_format   uint8   1 = S16LE (the only value)
 6       2     frame_ms        uint16  presentation duration, derived from the payload
 8       4     source_msi      uint32  0 = mixed / not attributable
```

`source_msi` is 0 for bridge → sidecar audio: it has exactly one source, us.

### 8.2 `VIDEO_I420` — 12 bytes, then planes

```
 offset  size  field      notes
 0       2     width      uint16
 2       2     height     uint16
 4       2     stride_y   uint16  == width for packed planes
 6       2     stride_uv  uint16  == width / 2
 8       2     fps        uint16
 10      2     reserved   uint16  must be 0
```

Strides are explicit so the sidecar never has to assume packing.

**The bridge sends I420; the sidecar converts to NV12.** The sidecar must copy every frame
into unmanaged memory for the media platform anyway, so the chroma interleave rides along on
a copy already being made — where doing it in Python would add a per-frame ~1.4 MB shuffle to
the bridge's event loop. Specified and tested in `tests/unit/test_teams_pixel_format.py`.

---

## 9. Framing and error handling

A decoder accumulates bytes and emits whole messages. TCP splits wherever it likes; the
decoder must not care.

**Framing errors are fatal for the connection and are never resynchronised.** A desynced
binary stream cannot be realigned with confidence, and guessing would surface as corrupt
audio in a live meeting rather than as an error. Both ends tear the connection down and
rebuild; the bridge then performs a full rejoin, because a media session cannot be
re-attached to a call whose signalling has gone.

Rejected outright:

- magic ≠ `0x544D4331`
- version ≠ 1
- `payload_len` > 8 MiB
- unknown message type

## 10. Backpressure

Same policy as Zoom's, for the same reason: **drop video, keep audio.** A lost video frame
costs one frame of smoothness; a lost audio frame is an audible gap.

| Condition | Action |
|---|---|
| Transport write buffer > 4 MiB | Drop the video frame, count it, mark the leg degraded |
| Audio | Always drained; never dropped for backpressure |
| Link down | Drop and count, absorb the error — the shared pacer must not be torn down mid-reconnect |

Drops are counted, never silent. A silent drop is how a latency bug becomes unfalsifiable.

## 11. Heartbeats

Either end may send `HEARTBEAT`; the receiver echoes `sent_at_us` verbatim. That makes the
round trip across the host boundary measurable from either side, which matters more here than
for Zoom's local socket.

## 12. Concurrency

The sidecar serves **one bridge connection at a time** and refuses a second `CONTROL_JOIN` on
a live link. One meeting per process is a platform constraint —
`MediaPlatform.Initialize` binds native resources to a port and cannot run twice in one
process — not a design preference.

Writes on the sidecar side are lock-serialised: media frames arrive on the media platform's
callback threads while control messages come from the accept loop, and interleaved writes
would corrupt framing. The lock is never held across an await.
