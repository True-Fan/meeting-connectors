"""Microsoft Teams connector.

One integration, in both directions — the structural opposite of Zoom's two (doc 005
§2). The ``Microsoft.Graph.Communications.Calls.Media`` platform owns receive *and*
send inside a single ``LocalMediaSession`` bound to a single Graph call, so a single
link carries participant audio up and the avatar's audio/video down.

* ``graph/``      — join resolution. ``models.py`` holds the wire types and must not
                    leave this package; ``join_url.py`` is the outbound boundary.
* ``sidecar/``    — the IPC codec, the TCP/TLS transport, and ``link.py``, which owns
                    the media session. ``sidecar/dotnet/`` is the Windows .NET bot.
* ``ingest/``     — the ``AudioSource`` port, plus ``mapping.py``, the inbound
                    anti-corruption boundary.
* ``publisher/``  — the ``MediaSink`` port.
* ``session/``    — composition: ``TeamsMeetingSession`` and its factory.

**Only official Microsoft technologies.** Azure AD client credentials, Microsoft Graph
``/communications/calls``, and the Graph Communications Media SDK. No browser
automation, no unofficial endpoints. The consequence is a hard platform constraint:
app-hosted media is Windows-and-.NET only, which is why the media runtime is a sidecar
on a separate host rather than a library in this process (doc 005 §1).

Nothing here is shared with ``connectors/zoom``: the SDKs, transports, credentials, and
pixel formats all differ. What the two share is the ports in ``src/protocols`` and the
media pipeline in ``src/services/media`` — which is the entire point of the boundary.
"""
