"""Everything about *this meeting*: which one, getting in, and what is happening in it.

* ``meet_url.py``     — the outbound anti-corruption boundary. The only module that
  knows what a Google Meet URL looks like.
* ``join.py``         — the join flow, and the states it can end in.
* ``controls.py``     — mute, camera, and leave, once in the call.
* ``participants.py`` — the roster, as the page observes it.
"""
