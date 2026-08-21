"""Ingest — the meeting's audio, tapped out of the page.

**The only ingest leg this connector has, and that is the honest difference from
``connectors/zoom_web``.** That connector can fall back to RTMS wherever a Zoom account will
serve it: named audio, named transcript, named chat, named participant events. Teams has no
equivalent a guest can reach — its media and event streams live behind exactly the Graph
entitlement ``connectors/teams`` needs, which is what this connector exists not to need.

So there is nothing here to select between. One mixed stream from the page's playout graph, and
everything else about the meeting read off the DOM.
"""
