"""Page assets, loaded from disk and injected before Zoom's own scripts run.

Files rather than string literals so they can be linted, diffed and read as
JavaScript. Injected with ``add_init_script`` rather than served, because there is no
HTTP origin of ours to serve them from — the page's origin is Zoom's.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).parent


@lru_cache(maxsize=1)
def inject_script() -> str:
    """The synthetic microphone: worklet, socket, and the ``getUserMedia`` patch."""
    return (_HERE / "inject.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def playout_worklet() -> str:
    """The playout worklet source, handed to the page as a string.

    It cannot be a module fetch: ``addModule`` needs a URL, and the only one
    available is a blob the page builds from this text.
    """
    return (_HERE / "playout_worklet.js").read_text(encoding="utf-8")
