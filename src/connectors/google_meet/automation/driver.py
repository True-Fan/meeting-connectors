"""Playwright and page lifecycle.

The whole of the connector's dependency on Playwright is here. Two consequences worth
naming:

**Playwright is imported lazily, inside the method that needs it.** A module-level import
would make ``import src.containers`` fail on a deployment that runs Zoom only and has no
browser installed — turning an unused connector into a startup crash for the two that are
already in production. The import cost is paid once, at the first session start, by the
deployment that actually asked for Meet.

**Everything above this file talks to ``BrowserDriver``, not to Playwright.** That
protocol is the seam that makes the connector testable: ``ChromiumBridge``, the join flow,
and the session all drive it, so the entire Meet pipeline can be exercised against an
in-process fake with no Chromium, no Google account, and no meeting. It is the same move
``TeamsSidecarLink``'s injectable ``client_factory`` makes, for the same reason.

The protocol is deliberately **thin and dumb**: wait for a selector, click a selector,
read the page's text, evaluate a script. It has no idea what a meeting is. Every decision
about which selector means what lives in ``meeting/`` and ``automation/selectors.py``,
which is what keeps the browser layer free of business logic.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.connectors.google_meet.browser.launcher import LaunchPlan
from src.connectors.google_meet.exceptions import (
    BrowserCrashedError,
    BrowserLaunchError,
    BrowserUnavailableError,
    PlaywrightUnavailableError,
)
from src.infrastructure.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - types only
    from playwright.async_api import BrowserContext, Page, Playwright

logger = get_logger(__name__)

_XPATH_PREFIXES = ("//", "..")


@runtime_checkable
class BrowserDriver(Protocol):
    """A browser this connector can drive.

    Implementations:

    * ``PlaywrightDriver`` — a real Chromium (production)
    * ``tests.fakes.meet_page.FakeBrowserDriver`` — an in-memory page whose DOM is a
      dict, which is what lets the join flow and every recovery path be tested
    """

    async def start(self, plan: LaunchPlan) -> None:
        """Launch the browser and open a page. Returns once a page exists."""
        ...

    async def add_init_script(self, script: str) -> None:
        """Register a script to run before any page script, in every frame.

        Must be called before ``goto``; scripts registered afterwards take effect only
        on the next navigation.
        """
        ...

    async def goto(self, url: str, *, timeout_s: float) -> None:
        """Navigate, returning once the document has loaded."""
        ...

    async def wait_for_any(
        self, selectors: tuple[str, ...], *, timeout_s: float
    ) -> str | None:
        """Wait until one of ``selectors`` is visible; return which one, or ``None``."""
        ...

    async def click_first(self, selectors: tuple[str, ...]) -> str | None:
        """Click the first visible selector; return which one, or ``None``."""
        ...

    async def fill_first(self, selectors: tuple[str, ...], value: str) -> str | None:
        """Type ``value`` into the first visible selector; return which one."""
        ...

    async def page_text(self) -> str:
        """The page's rendered text, for terminal-state detection."""
        ...

    def current_url(self) -> str:
        """The page's current URL, after any redirects.

        Where a *redirect* is the signal rather than the markup — Google sending an
        unauthenticated browser to a sign-in page is its own behaviour, not a DOM detail that
        can be renamed. See ``auth/google_login.py``.
        """
        ...

    async def cookies(self, urls: tuple[str, ...]) -> list[dict[str, Any]]:
        """Cookies the browser holds for ``urls``.

        The only way to read the authoritative signal for "is this profile signed in": a
        Google session *is* its session cookies. Everything else — page text, ARIA labels — is
        a rendering of that fact and can change without notice.
        """
        ...

    async def evaluate(self, script: str) -> Any:
        """Run a script in the page and return its result."""
        ...

    async def screenshot(self, path: Path) -> bool:
        """Capture the page. Returns False when unavailable; never raises."""
        ...

    def is_alive(self) -> bool:
        """False once the page or browser has closed or crashed."""
        ...

    async def stop(self) -> None:
        """Close the browser and release the profile. Must be idempotent."""
        ...


class PlaywrightDriver:
    """``BrowserDriver`` over a real Chromium, launched with a persistent profile."""

    __slots__ = ("_console", "_context", "_crashed", "_page", "_playwright", "_stopped")

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._crashed = False
        self._stopped = False
        self._console: Callable[[str, str], None] | None = None

    def set_console_handler(self, handler: Callable[[str, str], None] | None) -> None:
        """Forward the page's console and uncaught errors to ``handler(kind, text)``.

        **Not on the ``BrowserDriver`` protocol, and called through ``getattr`` by the one
        connector that wants it.** Widening the port would oblige every driver double in the
        suite to implement a method only Playwright can honour, and the protocol earns its
        narrowness by being the thing an in-memory fake can satisfy.

        **Off by default, so no existing connector's behaviour changes.** With no handler the
        listeners still attach but forward nothing — which costs a bound method call per console
        line and keeps ``start`` a single code path.

        It exists because some browser-side failures are visible *only* here. A WebSocket that
        Chromium refuses can end up CLOSED without firing an ``error`` or ``close`` event and
        without the constructor throwing, so the page cannot report the reason and neither can
        the bridge — Chromium writes it to the console and nowhere else. Without this, that
        class of failure is diagnosable only by a human watching a headed browser.
        """
        self._console = handler

    def _on_console(self, kind: str, text: str) -> None:
        """Hand one console line to the registered handler. Never raises.

        Total by construction: this runs on Playwright's event loop callback, and a listener
        that throws there would surface as an unretrieved future rather than as anything
        useful.
        """
        handler = self._console
        if handler is None:
            return
        with suppress(Exception):  # pragma: no cover - defensive
            handler(kind, text)

    # -- lifecycle ---------------------------------------------------------

    async def start(self, plan: LaunchPlan) -> None:
        """Launch Chromium with the resolved plan.

        Raises:
            PlaywrightUnavailableError: Playwright or its Chromium build is missing.
                Fatal — no retry installs a browser.
            BrowserLaunchError: Chromium failed to start. Usually a resource limit or a
                profile already in use, both of which a rejoin can resolve.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightUnavailableError(f"playwright is not installed ({exc})") from exc

        try:
            self._playwright = await async_playwright().start()
        except Exception as exc:
            raise PlaywrightUnavailableError(f"cannot start playwright ({exc})") from exc

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                **plan.to_playwright_kwargs()
            )
        except Exception as exc:
            await self.stop()
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                raise PlaywrightUnavailableError(
                    f"chromium is not installed for playwright ({message.splitlines()[0]})"
                ) from exc
            raise BrowserLaunchError(f"cannot launch chromium: {message}") from exc

        # ``launch_persistent_context`` already opens one page. Reusing it rather than
        # opening a second matters: the extra tab would also load Meet's init scripts and
        # a stray about:blank tab changes which page Chromium considers foreground, which
        # feeds straight back into the backgrounding throttles the launch flags disable.
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        # Lambdas rather than bound methods, and not a style choice: Playwright's handler
        # wrapper stores bookkeeping on ``handler.__self__`` with ``setattr``, which raises
        # ``AttributeError`` on a ``__slots__`` class like this one. Passing a bound method
        # here fails at launch — before any page exists — so the whole connector would have
        # died on its first real ``start()``. A lambda has no ``__self__``, so the wrapper
        # takes its other path.
        self._page.on("crash", lambda _page: self._on_crash())
        self._page.on("close", lambda _page: self._on_close())
        self._context.on("close", lambda _context: self._on_close())

        # Attached unconditionally and forwarded only when a handler is registered — see
        # ``set_console_handler``. Lambdas rather than bound methods for the ``__slots__``
        # reason the handlers above document.
        self._page.on(
            "console", lambda message: self._on_console(message.type, message.text)
        )
        self._page.on("pageerror", lambda error: self._on_console("pageerror", str(error)))

        logger.info("meet_browser.launched", headless=plan.headless, args=len(plan.args))

    def _on_crash(self) -> None:
        self._crashed = True
        logger.error("meet_browser.page_crashed")

    def _on_close(self) -> None:
        # Only interesting when we did not ask for it. During teardown this fires
        # normally and logging it as a fault would make every clean stop look like a
        # failure in the logs.
        if not self._stopped:
            self._crashed = True
            logger.warning("meet_browser.closed_unexpectedly")

    async def stop(self) -> None:
        """Close everything, in order, absorbing failures. Idempotent."""
        self._stopped = True
        page, self._page = self._page, None
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None

        for closer in (page, context):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception as exc:
                logger.debug("meet_browser.close_failed", error=str(exc))

        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                logger.debug("meet_browser.playwright_stop_failed", error=str(exc))

    def is_alive(self) -> bool:
        page = self._page
        if self._crashed or page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:
            return False

    # -- scripting ---------------------------------------------------------

    async def add_init_script(self, script: str) -> None:
        """Register an init script on the *context*, not the page.

        Context scope is what makes it survive navigation and apply to Meet's iframes —
        a page-scoped script would be lost the moment Meet performed its first
        client-side navigation, which it does during the join.

        Raises:
            BrowserUnavailableError: the browser is not started.
        """
        context = self._require_context()
        try:
            await context.add_init_script(script=script)
        except Exception as exc:
            raise BrowserUnavailableError(f"cannot add init script: {exc}") from exc

    async def goto(self, url: str, *, timeout_s: float) -> None:
        """Navigate to ``url``.

        Waits for ``domcontentloaded`` rather than ``load`` or ``networkidle``. Meet is a
        single-page application that keeps long-lived connections open and streams for
        the whole call, so ``networkidle`` never fires and ``load`` waits on assets the
        join does not need. The join flow then waits on the specific element it wants,
        which is a far better signal than any page-level event.

        Raises:
            BrowserUnavailableError: navigation failed or the browser is gone.
        """
        page = self._require_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except Exception as exc:
            if self._crashed:
                raise BrowserCrashedError(f"chromium crashed while loading {url}") from exc
            raise BrowserUnavailableError(f"cannot load {url}: {exc}") from exc

    async def wait_for_any(
        self, selectors: tuple[str, ...], *, timeout_s: float
    ) -> str | None:
        """Poll every candidate until one becomes visible.

        Polling rather than ``expect(locator).to_be_visible()`` on each in turn: the
        candidates are alternatives, so waiting the full timeout on the first before
        trying the second would multiply the timeout by the number of candidates. This
        way the whole set is checked repeatedly within one budget.
        """
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            for selector in selectors:
                if await self._is_visible(selector):
                    return selector
            if asyncio.get_running_loop().time() >= deadline:
                return None
            if not self.is_alive():
                raise BrowserCrashedError("chromium went away while waiting for a selector")
            await asyncio.sleep(0.25)

    async def _is_visible(self, selector: str) -> bool:
        page = self._require_page()
        try:
            return await page.locator(_normalise(selector)).first.is_visible(timeout=250)
        except Exception:
            # A selector that does not resolve, a detached node, or a navigation racing
            # the check are all "not visible right now" rather than errors. The caller is
            # in a polling loop and will ask again.
            return False

    async def click_first(self, selectors: tuple[str, ...]) -> str | None:
        page = self._require_page()
        for selector in selectors:
            if not await self._is_visible(selector):
                continue
            try:
                await page.locator(_normalise(selector)).first.click(timeout=5000)
            except Exception as exc:
                logger.debug("meet_browser.click_failed", selector=selector, error=str(exc))
                continue
            return selector
        return None

    async def fill_first(self, selectors: tuple[str, ...], value: str) -> str | None:
        page = self._require_page()
        for selector in selectors:
            if not await self._is_visible(selector):
                continue
            try:
                await page.locator(_normalise(selector)).first.fill(value, timeout=5000)
            except Exception as exc:
                logger.debug("meet_browser.fill_failed", selector=selector, error=str(exc))
                continue
            return selector
        return None

    async def page_text(self) -> str:
        page = self._require_page()
        try:
            return await page.inner_text("body", timeout=2000)
        except Exception:
            return ""

    def current_url(self) -> str:
        """The page's URL after redirects. Empty when there is no page."""
        page = self._page
        if page is None:
            return ""
        try:
            return page.url
        except Exception:
            return ""

    async def cookies(self, urls: tuple[str, ...]) -> list[dict[str, Any]]:
        """Cookies for ``urls``, as plain dicts.

        Read from the *context*, not the page: cookies belong to the profile and survive
        navigation, which is exactly the property that makes them a trustworthy session
        signal.

        Returns an empty list rather than raising when the context is gone — the caller reads
        "no cookies" as "not signed in", which is the safe interpretation either way.
        """
        context = self._context
        if context is None:
            return []
        try:
            return [dict(cookie) for cookie in await context.cookies(list(urls))]
        except Exception as exc:
            logger.warning("meet_browser.cookies_unavailable", error=str(exc))
            return []

    async def evaluate(self, script: str) -> Any:
        page = self._require_page()
        try:
            return await page.evaluate(script)
        except Exception as exc:
            raise BrowserUnavailableError(f"page evaluate failed: {exc}") from exc

    async def screenshot(self, path: Path) -> bool:
        """Capture the page for diagnosis.

        Never raises. It is called from failure paths, where taking the screenshot is a
        nice-to-have and masking the original error with a screenshot error would be a
        real loss.
        """
        page = self._page
        if page is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), timeout=5000)
        except Exception as exc:
            logger.debug("meet_browser.screenshot_failed", error=str(exc))
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _require_page(self) -> Page:
        page = self._page
        if page is None:
            raise BrowserUnavailableError("chromium is not started")
        if self._crashed:
            raise BrowserCrashedError("chromium page has crashed")
        return page

    def _require_context(self) -> BrowserContext:
        context = self._context
        if context is None:
            raise BrowserUnavailableError("chromium is not started")
        return context


def _normalise(selector: str) -> str:
    """Prefix XPath selectors so Playwright does not guess.

    Playwright infers XPath from a leading ``//``, but the inference is undocumented
    surface and ``automation/selectors.py`` mixes both syntaxes freely. Being explicit
    costs nothing and removes a class of "the selector silently matched nothing".
    """
    if selector.startswith(_XPATH_PREFIXES):
        return f"xpath={selector}"
    return selector
