"""Browser ingest — the meeting's audio, tapped out of the page instead of RTMS.

This package is what makes the connector work on an **ordinary Zoom account**. RTMS is
Zoom's own real-time media stream and it is a better signal in every respect, but it
requires the meeting to be hosted on an account with RTMS enabled for the app — which most
deployments do not have and cannot obtain, because the meeting belongs to whoever booked it.

So there are two ingest legs, selected by ``MC_ZOOM_WEB__INGEST_MODE``:

* ``rtms`` — ``connectors/zoom/rtms``, unchanged, and still the better one where it is
  available. Named audio, named transcript, named chat, named participant events.
* ``browser`` — this package. One mixed audio stream tapped from Zoom's playout graph, and
  the meeting's roster, speaker, chat and captions read off the page.

Nothing downstream distinguishes them. Both satisfy ``AudioSource``, and both feed the same
``ZoomMeetingObserver`` with the same observation types — which is why the ledgers, the
announcer, the interrupt source and every HTTP endpoint were untouched by this being added.
"""
