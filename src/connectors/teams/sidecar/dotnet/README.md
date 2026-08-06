# Teams media sidecar (`mc-teams-sidecar`)

The Windows half of the Teams connector. It owns the Microsoft Graph call and the
app-hosted media session, and exposes them to the Python bridge over the wire protocol in
[`docs/design/006-teams-sidecar-ipc-protocol.md`](../../../../../docs/design/006-teams-sidecar-ipc-protocol.md).

It holds **no policy**. When to speak, what to publish, how to recover, how to pace — all
of that is in the bridge. This process moves bytes between a socket and two media sockets.

---

## 1. Why this exists as a separate artifact

Microsoft's *application-hosted media* is the only official way to get frame-level audio
and video into a Teams meeting, and it is available exclusively as a **Windows, x64, .NET
Framework** library: `Microsoft.Graph.Communications.Calls.Media` ships native media-platform
binaries with no Linux, .NET Core, or 32-bit build.

The Python bridge runs in a Linux container. So this is not a design preference — it is the
platform constraint, and it is the single reason the Teams connector is shaped differently
from Zoom's. The alternatives were all rejected as unofficial or worse:

| Alternative | Why not |
|---|---|
| Service-hosted media (`playPrompt`) | No frame-level access and no video send. Cannot carry an avatar. |
| Headless browser joining the meeting | Browser automation — explicitly out of scope, and unsupported by Microsoft. |
| A virtual camera / audio device | Requires a desktop Teams client and a logged-in session. Not a service. |

## 2. Where the boundary sits

```
Linux container                          Windows host
┌────────────────────────┐  TCP + TLS   ┌─────────────────────────────────┐
│  meeting-connectors     │◀────────────▶│  mc-teams-sidecar                │
│  (Python bridge)        │   'TMC1'     │                                  │
│                         │              │  Graph Communications Calls SDK  │──▶ Graph
│  • pipeline, pacing     │              │  Media SDK (app-hosted media)    │◀──  media
│  • avatar client        │              │  • AudioSocket   Sendrecv        │
│  • echo suppression     │              │  • VideoSocket   Sendonly, NV12  │
└────────────────────────┘              │  • /api/calls notifications      │◀── Graph
                                         └─────────────────────────────────┘
```

One connection, both directions. Participant audio flows *up*; the avatar's audio and
video flow *down*. That is not a convenience — the media platform binds receive and send
into one `LocalMediaSession` on one call, so they cannot be separated.

**Graph notifications terminate here, not in the bridge.** The Calling SDK consumes them
(`ProcessNotificationAsync` is what advances call state and delivers the roster), so
relaying them via Python would mean forwarding them straight back out. The practical
consequence is good: `connectors/teams` adds no FastAPI router, and `src/api` is untouched
by this connector.

## 3. Prerequisites

### 3.1 Azure AD app registration

Application permissions, admin-consented — a bot acts as itself, not on behalf of a user:

| Permission | Why |
|---|---|
| `Calls.JoinGroupCall.All` | Join a scheduled meeting |
| `Calls.AccessMedia.All` | App-hosted media. **Without it the join succeeds and no media flows.** |
| `Calls.JoinGroupCallAsGuest.All` | Only if joining meetings outside the tenant |

Register a **bot** with the Teams channel enabled and its calling webhook set to
`https://<service-fqdn>:<media-public-port>/api/calls`.

### 3.2 Host

- Windows Server 2019+ (or Windows 10/11 for development), **x64**
- .NET Framework 4.7.2+
- A **publicly resolvable** DNS name and a **publicly trusted** TLS certificate whose
  subject matches it. Microsoft's service connects inbound and validates it; a self-signed
  certificate fails at `MediaPlatform.Initialize` with a message that does not mention
  certificates.
- Inbound TCP open on the media public port and the notification port.

### 3.3 Certificates

Two, for different jobs:

| Certificate | Used for | Trust requirement |
|---|---|---|
| `--media-cert-thumbprint` | Media platform + Graph notification endpoint | **Publicly trusted**, subject == `--service-fqdn` |
| `--ipc-cert-thumbprint` | The bridge link | Internally issued is fine — the bridge pins it via `MC_TEAMS__SIDECAR_CA_FILE` |

Install both into `LocalMachine\My` and grant the service account read access to the
private keys. Bind the media certificate to the notification port:

```powershell
netsh http add sslcert ipport=0.0.0.0:9441 `
  certhash=<media-thumbprint> `
  appid={00000000-0000-0000-0000-000000000000}
```

## 4. Build and run

```powershell
# Requires MSBuild with the .NET Framework 4.7.2 targeting pack.
msbuild MeetingConnectors.Teams.Sidecar.csproj /p:Configuration=Release /p:Platform=x64

.\bin\x64\Release\mc-teams-sidecar.exe `
  --service-fqdn teams-bot.example.com `
  --media-cert-thumbprint AAAA1111... `
  --ipc-cert-thumbprint BBBB2222... `
  --ipc-listen 10.0.0.7 `
  --ipc-port 8445 `
  --ipc-require-client-cert
```

Every flag also reads from `MC_TEAMS_SIDECAR_<UPPER_SNAKE_CASE>`, so a Windows service
definition can use either. Run `mc-teams-sidecar.exe` with no arguments for the full list.

**One meeting per process.** `MediaPlatform.Initialize` binds native resources to a port
and cannot run twice in one process, so concurrency means more processes on more ports —
again a platform constraint, not a design choice. The sidecar accepts one bridge
connection at a time and refuses a second `CONTROL_JOIN` on a live link.

## 5. What the bridge must be configured with

```dotenv
MC_TEAMS__TENANT_ID=<tenant guid>
MC_TEAMS__CLIENT_ID=<app registration guid>
MC_TEAMS__CLIENT_SECRET=<client secret>
MC_TEAMS__SIDECAR_HOST=teams-bot.example.com
MC_TEAMS__SIDECAR_PORT=8445
MC_TEAMS__SIDECAR_CA_FILE=/etc/mc/teams-sidecar-ca.pem
```

Note what is *not* configured on the Windows side: the credentials and the meeting. Both
arrive per session in `CONTROL_JOIN`, so rotating a secret is a bridge-side config change
and this host stores nothing durable worth stealing.

## 6. Verification status — read this before deploying

Honest accounting of what is and is not proven:

| Component | Status |
|---|---|
| Wire codec (`Wire/WireProtocol.cs`) | Contract is **pinned by a Python conformance test** (`tests/unit/test_teams_sidecar_protocol.py`) that asserts exact bytes. The C# side must match it; run that test's vector against this codec as the first build step. |
| I420 → NV12 conversion | Algorithmically verified against the Python reference in `tests/unit/test_teams_pixel_format.py`. |
| IPC framing, TLS, session state machine | Straightforward logic; the bridge side is covered by `tests/unit/test_teams_link.py` against an in-process fake. |
| **Graph + Media SDK integration** | **Written against Microsoft's documented API surface and NOT yet compiled or run.** No Windows host, Azure tenant, or admin consent was available. Expect to reconcile exact type and property names against the SDK version you restore — `JoinMeetingIdMeetingInfo`, `IUnmixedAudioBuffer.ActiveSpeakerId`, and `Participant.Resource.MediaStreams[].SourceId` are the three most likely to have drifted. |

The bridge side is fully testable today without any of this: `TeamsSessionFactory` accepts
a `client_factory`, and `tests/fakes/teams_sidecar.py` is an in-process sidecar that speaks
the real wire protocol. That is the same de-risking strategy the Zoom connector used — the
whole pipeline was proven against a stub before the C++ SDK build existed.

## 7. Deployment shape

The Zoom sidecar shares a volume with the bridge and talks over a Unix socket. This one
cannot: it is a different machine and a different OS. So it is a separate deployment unit
with its own lifecycle — `docker/docker-compose.yml` does not and cannot contain it. Run it
as a Windows service (NSSM or `sc.exe`) with automatic restart; the bridge's reconnect
policy handles the gap, re-creating the Graph call on the way back up.
