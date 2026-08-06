# meeting-connectors — What's Built, Inputs, Outputs, Protocols

**One-line summary:** a bridge service that puts an AI streaming avatar into a Zoom
meeting as an ordinary participant — it hears the humans, speaks back, and shows
generated video — without modifying the avatar agent itself.

---

## 1. Current status

| Layer | Status |
|---|---|
| Session control API, lifecycle, health/metrics | ✅ Built and verified |
| Zoom RTMS ingest (receiving meeting audio) | ✅ Built and verified against Zoom's real webhook/handshake; **blocked** on Zoom enabling RTMS for our App ID (pending their approval — request filed) |
| Avatar agent leg (send audio out, receive generated video back) | ✅ Built and verified against a mock avatar server |
| Publish leg — put the avatar's audio/video into the actual meeting | ✅ IPC and control flow built and verified against a stub; **not yet** wired to Zoom's real Meeting SDK (separate follow-up milestone — requires a Linux build and SDK entitlement) |

Everything below describes the system as built. Two items are explicitly still stubbed
pending the items above, and are called out where relevant.

---

## 2. How it fits together

```
Zoom meeting participants
        │  (their voices)
        ▼
┌───────────────────────┐        ┌──────────────────────┐
│   Zoom RTMS servers    │──WS──▶ │                      │
└───────────────────────┘        │                      │
                                  │   meeting-connectors  │──WS──▶ Avatar Agent
┌───────────────────────┐        │      (this bridge)     │◀──WS── (generates
│  Zoom webhook (HTTPS)  │──HTTP▶│                      │        speech + video)
└───────────────────────┘        │                      │
                                  └──────────┬───────────┘
                                             │  Unix socket
                                             ▼
                                  ┌──────────────────────┐
                                  │  Publisher sidecar     │──native SDK──▶ Zoom meeting
                                  │  (C++, Zoom Meeting SDK)│              (avatar's audio/video)
                                  └──────────────────────┘
```

Two independent, concurrently-running legs meet inside this bridge:
- **Ingest** — hearing the meeting (Zoom → bridge → avatar agent)
- **Publish** — speaking into the meeting (avatar agent → bridge → Zoom)

They recover independently: audio can keep flowing even if publish is reconnecting, and
vice versa.

---

## 3. Inputs

| # | Input | Direction | Carries |
|---|---|---|---|
| 1 | **Control command** | Operator/system → bridge | "Put an avatar into meeting X" — meeting number, passcode, display name |
| 2 | **Zoom webhook** | Zoom → bridge | Signed notification that a meeting's real-time media stream has started/stopped, with routing details for input 3 |
| 3 | **Live participant audio** | Zoom → bridge | The actual voices in the meeting, once input 2 has arrived |
| 4 | **Avatar's generated response** | Avatar agent → bridge | Synchronized speech + video generated in response to input 3 |
| 5 | **Configuration** | Deploy-time | Zoom app credentials, avatar agent address, media/quality settings |

## 4. Outputs

| # | Output | Direction | Carries |
|---|---|---|---|
| 1 | **Session status** | Bridge → operator/system | Whether the avatar joined, current health, error history |
| 2 | **Service health & metrics** | Bridge → monitoring | Uptime, active sessions, latency/throughput counters |
| 3 | **Participant audio, forwarded** | Bridge → avatar agent | The meeting's live audio, so the avatar knows what to respond to |
| 4 | **Avatar's audio + video, published** | Bridge → meeting (via sidecar) | What the other meeting participants actually hear and see from the avatar |

---

## 5. Protocols — by leg

**Short answer to "WebSocket or WebRTC": WebSocket, not WebRTC — nowhere in this
system do we implement WebRTC directly.** The one leg that carries real-time media
into the meeting (publish) goes through Zoom's own native Meeting SDK, which handles
its own media transport internally as a black box we never touch directly.

| Leg | Protocol | Notes |
|---|---|---|
| Operator control | **HTTP/REST**, JSON | Standard request/response — create/inspect/stop a session |
| Zoom → bridge, routing signal | **HTTPS webhook**, JSON, HMAC-signed | One-shot notification, not a stream |
| Zoom → bridge, live audio | **WebSocket** (Zoom's RTMS protocol) | Two sockets — one for control/handshake, one for the actual audio frames. Zoom's own protocol, not WebRTC. |
| Bridge → avatar agent | **WebSocket** | Raw PCM audio sent out; a fragmented MP4 (fMP4) audio+video stream received back |
| Bridge → publisher process | **Unix domain socket**, custom lightweight binary framing (not HTTP/WS) | Chosen for minimum local latency between the Python process and the native publishing process on the same machine |
| Publisher process → Zoom meeting | **Zoom Meeting SDK** (native library call, not a network protocol we implement) | Handles the actual media transport into the meeting internally; this project only calls its API |

---

## 6. Data formats in flight

- **Audio**: 16-bit linear PCM, mono or stereo depending on config, fixed sample rate matched to the avatar's contract — chosen so no resampling step sits in the real-time path.
- **Video**: raw I420 frames, decoded from the avatar's fMP4 stream, sized/rated per config (e.g. 1280×720 @ 25fps).

---

## 7. What's genuinely left before this is end-to-end live

1. **Zoom RTMS backend enablement** for our App ID — requested, pending Zoom's approval. Nothing further to build on our side for this.
2. **Real Zoom Meeting SDK integration** — the publish leg currently talks to a stub that proves the control flow and IPC are correct, but doesn't yet call Zoom's actual SDK to make the avatar visible in a meeting. That's a distinct, larger piece of work (Linux build, SDK entitlement) tracked separately from what's described above.
