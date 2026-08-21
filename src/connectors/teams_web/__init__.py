"""Microsoft Teams, joined with a browser instead of Graph app-hosted media.

**Why this exists alongside ``connectors/teams``.** That connector is the better one
wherever it can run — Graph hands over per-participant audio with a source id, one session
carries both directions, and no part of it depends on markup Microsoft can rename. What it
needs is an Azure AD app registration with admin-consented ``Calls.JoinGroupCall.All`` and
``Calls.AccessMedia.All`` **in the tenant that owns the meeting**, plus a Windows host
running the .NET media SDK (doc 005 §1 and §2). A deployment whose avatar joins meetings other
people booked can obtain neither: the tenant is not its own, and consent is not something a
guest can arrange.

This connector needs nothing from the tenant. It drives Chromium to the ordinary Teams web
client and joins the way a person without a Teams account joins — the meeting link, a name
typed into the pre-join form, and a wait in the lobby. That is the same trade
``connectors/zoom_web`` makes against ``connectors/zoom``, and it is made here for a
stronger reason: Zoom's alternative is a licensed SDK download, Teams' is a tenant
administrator's signature.

**The two halves, and what each rests on.**

* **publish** — a synthetic ``MediaStreamTrack``. PCM arrives over a loopback WebSocket, an
  ``AudioWorklet`` turns it into a real track, and a patched ``getUserMedia`` hands that
  track to the page. The same mechanism the Google Meet connector publishes through, and it
  needs no audio device on the host.
* **ingest** — the page's own playout graph, tapped. One mixed stream, no attribution, which
  is why who-is-talking is read separately off the DOM.

**One ingest mode, where ``zoom_web`` has two.** That connector can fall back to RTMS,
Zoom's own real-time media stream, wherever an account will serve it. Teams has no
equivalent a guest can reach — its event and media streams live behind exactly the Graph
entitlement the other connector needs — so there is nothing to select between, and no
``ingest_mode`` setting here. Everything the avatar knows about this meeting comes from the
browser.

**What that costs, stated once.** Every observation is a rendering rather than an event, so
two people sharing a display name are one person, a rejoin under the same name is invisible,
captions have to be switched on by somebody, and a Teams release can rename the hooks the
observers read. The ledgers downstream were written for that grade of signal — hold windows,
merge gaps, name-keyed history — so none of them had to be weakened to accept it. See doc
010 for the full accounting.
"""
