"""Ingest — the meeting's audio, tapped out of the page.

This package is what makes the connector work on an **ordinary Zoom account**. There used
to be a second leg here: Zoom's own RTMS stream, which was a better signal in every respect
— named audio, named transcript, named chat, named participant events. It required the
meeting to be hosted on an account with RTMS enabled for the app, which most deployments do
not have and cannot obtain, because the meeting belongs to whoever booked it. So it was
removed and this is the only leg.

One mixed audio stream tapped from Zoom's playout graph, and the meeting's roster, speaker,
chat and captions read off the page. It satisfies ``AudioSource`` and feeds
``ZoomMeetingObserver`` with this connector's observation types, which is why the ledgers,
the announcer, the interrupt source and every HTTP endpoint neither knew nor cared when the
other leg went away.
"""
