"""What the injected script looks at to find a raised hand.

**Injected as data, not written into the JavaScript**, for the reason
``ZoomWebSelectors`` in ``meeting/join.py`` is: a Zoom UI change then costs a settings
edit rather than an asset edit and a redeploy. The script treats every one of these as
optional and unparseable ones as a miss, so a selector that stops matching costs the
signal it carried and nothing else.

**Why there are three lists rather than one.** They answer three different questions,
and conflating them would make each worse:

* ``hand_indicator`` — the element that *is* a raised hand. Precise, and the most
  likely of the three to be renamed by a Zoom release.
* ``participant_row`` — the container that element sits in, which is how an icon with
  no text of its own becomes a person. Far more stable, because it is the panel's
  fundamental unit.
* ``participant_name`` — where the name lives inside that row.

The script also runs a label sweep that depends on none of them, matching the sentence
Zoom shows a human ("… raised hand") wherever it appears. That is the pass that keeps
working when every class name here has changed, and it is why these being wrong
degrades the feature instead of disabling it.

Nothing here is a guess dressed as a fact: these are the class names Zoom's web client
has used, listed most-specific first, with generic fallbacks behind them. Verify
against a live meeting with ``MC_ZOOM_WEB__HEADLESS=false`` and the ``handsIdle``
diagnostics, which report what the page actually contains when nothing matched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZoomHandSelectors:
    """CSS selectors for the raised-hand observer. Ordering is most-specific first."""

    hand_indicator: tuple[str, ...] = (
        # Zoom's participants panel marks the row with a nonverbal-feedback icon.
        ".participants-icon__participants-raisehand",
        ".participants-item__buttons .participants-icon-raisehand",
        "[class*='raisehand' i]",
        "[class*='raise-hand' i]",
        # The video tile's footer badge, for a layout with the panel closed.
        ".video-avatar__avatar-footer [class*='hand' i]",
        "[aria-label*='raised hand' i]",
        "[title*='raised hand' i]",
    )

    participant_row: tuple[str, ...] = (
        ".participants-item__item-inner",
        ".participants-li",
        "[class*='participants-item' i]",
        # Zoom's newer participants panel is a virtualised list; these are what it renders
        # rows as. Added after a live run reported ``rows: 0`` with the panel open, which
        # means every selector above it missed — the list exists, under other names.
        "#participants-ul > li",
        "[class*='participants-list' i] li",
        "[role='listitem']",
        ".video-avatar__avatar",
        "[data-participant-id]",
    )

    participant_name: tuple[str, ...] = (
        ".participants-item__display-name",
        "[class*='display-name' i]",
        ".video-avatar__avatar-name",
        "[class*='avatar-name' i]",
        # The tile's title, which is where the name lives when the tile is showing video
        # — and a tile showing video is exactly the one a raised hand appears on, because
        # ``video-avatar__avatar-img`` replaces ``…-name`` there. Observed in a live run:
        # the row that gained the hand indicator was the only row with no name element.
        ".video-avatar__avatar-title",
        "[class*='avatar-title' i]",
    )

    participants_panel_button: tuple[str, ...] = (
        # Zoom labels the toggle by what it opens, and the count is appended to it —
        # "Open the participants list panel, 3" — so every match here is a substring.
        "button[aria-label*='participants' i]",
        "[role='button'][aria-label*='participants' i]",
        ".footer-button__participants-icon",
        "#participant",
    )
    """The control that opens the participants panel.

    **The indicator does not exist in a DOM nobody opened**, which is the single most
    likely reason for a correctly-configured observer to see nothing. Zoom renders a
    raised hand as a transient toast with the panel closed, and the panel is where the
    persistent indicator lives."""


DEFAULT_HAND_SELECTORS = ZoomHandSelectors()
