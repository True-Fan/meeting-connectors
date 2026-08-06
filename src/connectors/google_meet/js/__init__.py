"""Browser-side assets, and the loader that reads them.

The only JavaScript in this repository. It lives as ``.js`` files rather than as Python
string literals for three reasons that all turned out to matter: an editor lints and
formats it, ``git blame`` on a media bug points at a line of JavaScript instead of at a
1200-character string, and the two AudioWorklet processors *must* be separate module
sources anyway because ``audioWorklet.addModule`` takes a URL.

The worklets never reach the page as files, though. ``bridge.js`` wraps each source in a
``Blob`` and calls ``addModule`` on the resulting object URL, which means no HTTP server
has to exist to serve them and no path on disk has to be reachable from the browser's
origin. So the loader's job is to hand three strings to
``automation/driver.py``, which injects them.

Assets are cached on first read. They are immutable at runtime, and a session start
should not do file I/O that a module import already did.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

_ASSET_DIR = Path(__file__).resolve().parent

BRIDGE_ASSET = "bridge.js"
CAPTURE_WORKLET_ASSET = "capture_worklet.js"
PLAYOUT_WORKLET_ASSET = "playout_worklet.js"


@dataclass(frozen=True, slots=True)
class BrowserAssets:
    """The three sources the page needs."""

    bridge: str
    """The init script. Installs the device patches, the peer-connection tap, the DOM
    observers, and the socket."""

    capture_worklet: str
    """``mc-capture`` — conference audio to 16 kHz mono s16le."""

    playout_worklet: str
    """``mc-playout`` — the avatar's PCM into the synthetic microphone track."""


@cache
def read_asset(name: str) -> str:
    """Read one browser asset by file name.

    Raises:
        FileNotFoundError: the asset is missing, which means the package was installed
            without its data files. Worth failing loudly at session start rather than
            injecting an empty script and watching the page never connect.
    """
    path = _ASSET_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"browser asset {name!r} is missing from {_ASSET_DIR}; the google_meet "
            "connector cannot run without its injected scripts"
        )
    return path.read_text(encoding="utf-8")


def load_assets() -> BrowserAssets:
    """Read every browser asset."""
    return BrowserAssets(
        bridge=read_asset(BRIDGE_ASSET),
        capture_worklet=read_asset(CAPTURE_WORKLET_ASSET),
        playout_worklet=read_asset(PLAYOUT_WORKLET_ASSET),
    )
