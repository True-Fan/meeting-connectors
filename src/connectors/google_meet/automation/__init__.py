"""Playwright and the Meet DOM.

Named ``automation`` rather than ``playwright`` on purpose. Imports in this repository
are absolute and relative imports are banned, so a package named
``src.connectors.google_meet.playwright`` would resolve unambiguously — but every reader
of ``from playwright.async_api import ...`` inside it would have to stop and work that
out. The cost of the clearer name is one line in a docstring.

* ``selectors.py`` — every Meet DOM selector and UI string, in one place.
* ``driver.py``    — Playwright and page lifecycle, and script injection.
"""
