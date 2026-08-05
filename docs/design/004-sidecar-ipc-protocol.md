# Sidecar IPC Protocol — v1 (FROZEN)

**Status:** 🔒 **FROZEN** as of 2026-08-05, before the publisher is implemented.
**Wire version:** `1`
**Reference implementation:** [`src/connectors/zoom/publisher/protocol.py`](../../src/connectors/zoom/publisher/protocol.py)
**Conformance tests:** `tests/unit/test_sidecar_protocol.py`
**Transport:** Unix domain socket, `SOCK_STREAM`

---

## 1. Why it is frozen now

The Python bridge and the C++ sidecar are separate artifacts in separate languages, built in different milestones (M1 vs M5). If the wire format is settled while the publisher is being written, it gets settled *by* the publisher — and the C++ side becomes the de facto spec, which is the wrong direction of authority for a boundary the Python side has to test against.

Freezing it in M1 gives us an executable reference codec plus conformance tests **before** any native code exists, so the C++ implementation has a fixed target and a test vector to match.

**Change policy:** v1 is closed. Any change that is not purely additive-with-defaults requires a new `version` byte and a new document. Additive changes permitted within v1: new `msg_type` values from the reserved range, new `flags` bits, new **optional** JSON fields in control messages. Receivers must ignore unknown JSON fields and unknown flag bits.

---

## 2. Frame layout

Every message is a fixed 24-byte header followed by a variable payload.

```
 offset  size  field       type     notes
 ──────  ────  ─────────   ──────   ─────────────────────────────────────────
      0     4  magic       u32      0x5A4D4331 — ASCII "ZMC1"
      4     1  version     u8       = 1
      5     1  msg_type    u8       §3
      6     1  flags       u8       §4
      7     1  reserved    u8       = 0, receivers must not validate
      8     4  seq         u32      monotonic per message type, wraps at 2^32
     12     8  pts_us      i64      shared MediaClock, µs; 0 for control
     20     4  length      u32      payload length, ≤ 8 MiB
     24     …  payload              §5
```

**Byte order is big-endian** throughout. Deliberate: network order is unambiguous across a Python `struct` implementation and a C++ one, and removes any question of host endianness on a boundary we cannot easily re-test later. The cost is 24 bytes of byte-swapping per message — irrelevant at ~75 messages/second.

All multi-byte fields are naturally aligned within the header, so a C++ reader may cast a packed struct rather than unpack field by field.

`pts_us` rides the **shared media clock** (doc 003 §5.2). It is what makes A/V sync possible at the SDK boundary — the sidecar must present frames on this timeline, not on arrival order.

**`length` cap: 8 MiB.** A 1080p I420 frame is ~3.1 MB, so the cap is comfortable while still bounding allocation from a corrupt or hostile length field.

---

## 3. Message types

| Value | Name | Direction | Payload |
|---:|---|---|---|
| `0x01` | `VIDEO_I420` | bridge → sidecar | §5.1 |
| `0x02` | `AUDIO_PCM` | bridge → sidecar | §5.2 |
| `0x03` | `CONTROL_JOIN` | bridge → sidecar | JSON, §5.3 |
| `0x04` | `CONTROL_LEAVE` | bridge → sidecar | JSON, §5.4 |
| `0x05` | `HEARTBEAT` | both | JSON, §5.5 |
| `0x06` | `READY` | sidecar → bridge | JSON, §5.6 |
| `0x07` | `ERROR` | sidecar → bridge | JSON, §5.7 |
| `0x08`–`0x7F` | — | — | reserved for future v1 additions |
| `0x80`–`0xFF` | — | — | reserved for vendor/experimental use |

---

## 4. Flags

| Bit | Mask | Name | Meaning |
|---:|---:|---|---|
| 0 | `0x01` | `KEYFRAME` | Video frame is a keyframe. Advisory. |
| 1 | `0x02` | `IDLE` | Frame came from `IdleFrameSource`, not the avatar (doc 003 §1.4). Lets the sidecar log or meter idle publishing distinctly. |
| 2 | `0x04` | `END_OF_STREAM` | Last frame of an utterance. Advisory. |
| 3–7 | — | — | Reserved. Senders set 0; receivers must ignore unknown bits. |

---

## 5. Payloads

### 5.1 `VIDEO_I420`

```
 offset  size  field       notes
      0     2  width       u16, pixels
      2     2  height      u16, pixels
      4     2  stride_y    u16, bytes per Y row
      6     2  stride_u    u16
      8     2  stride_v    u16
     10     2  reserved    u16 = 0
     12     …  planes      Y then U then V, contiguous
```

**Geometry is per-frame, not negotiated once.** 12 bytes at 25 fps is 300 B/s — negligible — and it removes an entire failure class: if the avatar's resolution changes mid-session, a sidecar working from a stale negotiated geometry would render garbage. Strides are explicit for the same reason: packed output is the common case, not a guarantee.

For tightly packed I420: `stride_y == width`, `stride_u == stride_v == width / 2`, and `len(planes) == width * height * 3 / 2`.

### 5.2 `AUDIO_PCM`

```
 offset  size  field            notes
      0     4  sample_rate_hz   u32
      4     1  channels         u8
      5     1  sample_format    u8   1 = S16LE (only value in v1)
      6     2  reserved         u16 = 0
      8     …  pcm              interleaved samples
```

Explicit rather than assumed, for the same reason as video geometry: the publish-side sample rate is a config value until confirmed against the SDK headers in M5 (doc 003 §9 Q4), and a silent mismatch would produce pitch-shifted audio rather than an error.

### 5.3 `CONTROL_JOIN` — JSON

```json
{
  "session_id":     "ses_…",
  "correlation_id": "cor_…",
  "meeting_number": "1234567890",
  "passcode":       "…",
  "display_name":   "Avatar",
  "sdk_jwt":        "eyJ…",
  "video": { "width": 1280, "height": 720, "fps": 25 },
  "audio": { "sample_rate_hz": 32000, "channels": 1 }
}
```

**On identity.** `session_id` and `correlation_id` appear here, at join, and are echoed by the sidecar in `READY`, `ERROR`, and its own logs. They are deliberately **not** repeated on every media frame: one sidecar process serves one meeting (doc 001 §12.3), so the session is unambiguous after join, and adding 72 bytes of UUID to every frame at 75 fps would buy nothing. The requirement that every frame carry the ids holds where it is meaningful — inside the Python domain, via `FrameContext` — and the IPC boundary binds once and stays correlated.

**On credentials.** `sdk_jwt` is short-lived and minted in Python. The sidecar holds no long-lived secret, so secrets live in exactly one process (doc 003 §7.4).

### 5.4 `CONTROL_LEAVE` — JSON

```json
{ "reason": "operator_stop" }
```

### 5.5 `HEARTBEAT` — JSON

```json
{ "sent_at_us": 1234567890123 }
```

Either side may originate. A receiver echoes the same `sent_at_us` so the sender can measure IPC round-trip without a clock agreement.

### 5.6 `READY` — JSON

```json
{
  "session_id":           "ses_…",
  "correlation_id":       "cor_…",
  "sdk_version":          "6.x.y",
  "has_raw_data_license": true,
  "participant_id":       16778240,
  "video": { "width": 1280, "height": 720, "fps": 25 },
  "audio": { "sample_rate_hz": 32000, "channels": 1 }
}
```

Sent once `onStartSend` has fired and the external video source and virtual microphone are registered — i.e. when the sink can genuinely accept frames, not merely when the process is up.

`participant_id` is the bot's own Meeting SDK user id, which `EchoGuard` needs to recognise the avatar's own audio arriving back through RTMS. Whether it shares an id space with RTMS `user_id` is unverified (doc 002 §12.2 B3) — which is exactly why the echo gate exists as a second, identity-independent defence layer.

`has_raw_data_license` reports the `HasRawdataLicense()` probe. `false` must fail the session loudly at join rather than silently producing no video (doc 003 §7.1).

`video`/`audio` report what was actually negotiated after clamping to `support_cap_list`, which may be lower than requested.

### 5.7 `ERROR` — JSON

```json
{
  "session_id":     "ses_…",
  "correlation_id": "cor_…",
  "code":           "JOIN_FAILED",
  "message":        "…",
  "fatal":          true
}
```

`fatal: true` means do not retry — the supervisor moves the session to `FAILED`. `fatal: false` is a transient condition and the reconnect policy applies.

---

## 6. Connection lifecycle

```
bridge connects to UDS
  → CONTROL_JOIN
  ← READY                    (or ERROR, fatal → session FAILED)
  → VIDEO_I420 / AUDIO_PCM   continuously, paced (never stops — §1.4 idle media)
  ↔ HEARTBEAT                every heartbeat_interval_s
  → CONTROL_LEAVE
     socket close
```

**Framing desync is fatal, by design.** A reader encountering a bad magic value must raise rather than scan forward for the next plausible header. A desynced binary stream cannot be re-aligned with confidence, and a heuristic resync would publish garbage video while reporting success. Fail loud, let the supervisor restart the sidecar, and lose a known-bounded interval instead of an unknown one.

**Backpressure.** If the socket write buffer is full, the bridge drops **video** frames and preserves audio: a lost video frame costs one frame of smoothness, a lost audio frame is an audible gap. Drops are counted, never silent (doc 003 §7.1).

---

## 7. Reference test vector

A `CONTROL_JOIN` carrying the 2-byte payload `{}`:

```
5A 4D 43 31   magic  "ZMC1"
01            version 1
03            msg_type CONTROL_JOIN
00            flags
00            reserved
00 00 00 00   seq 0
00 00 00 00 00 00 00 00   pts_us 0
00 00 00 02   length 2
7B 7D         payload "{}"
```

Total 26 bytes. Asserted byte-for-byte in `tests/unit/test_sidecar_protocol.py`, which is what makes this document a specification rather than a description.
