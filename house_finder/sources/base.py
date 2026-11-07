"""Base class and shared HTTP/browser helpers for source adapters."""

from __future__ import annotations

import abc
import logging
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx

from ..config import get_settings
from ..schemas import RawListing, SearchQuery

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Raised when a source cannot be fetched or parsed."""


class BaseSource(abc.ABC):
    """A pluggable housing source.

    Subclasses set ``name`` and ``base_url`` and implement :meth:`fetch`.
    """

    name: str = "base"
    base_url: str = ""
    #: Whether this source generally needs a headless browser.
    needs_browser: bool = False

    def __init__(self) -> None:
        self.settings = get_settings()
        self._last_request = 0.0
        # robots.txt parsers keyed by scheme://host. ``None`` means the file
        # was unavailable, in which case we fail open (a missing/redirected
        # robots.txt is not a prohibition).
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # ---- public API -------------------------------------------------
    @abc.abstractmethod
    def fetch(self, query: SearchQuery) -> list[RawListing]:
        """Return raw listings for the given query. Must not raise for
        individual bad listings — skip them instead."""

    def search_url(self, query: SearchQuery) -> str:
        """Human-facing search URL (used for 'open on site' links)."""
        return self.base_url

    # ---- HTTP helpers ----------------------------------------------
    def _throttle(self) -> None:
        delay = self.settings.request_delay_seconds
        elapsed = time.monotonic() - self._last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }

    def _robots_allows(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            rp: urllib.robotparser.RobotFileParser | None
            try:
                # urllib's RobotFileParser.read() has no timeout and treats
                # redirects/5xx inconsistently. Fetch with httpx instead and
                # parse the text; an unavailable file means "allow".
                resp = httpx.get(
                    f"{origin}/robots.txt",
                    headers={"User-Agent": self.settings.user_agent},
                    timeout=min(15, self.settings.request_timeout),
                    follow_redirects=True,
                )
                if resp.status_code >= 400 or not resp.text.strip():
                    rp = None
                else:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.set_url(f"{origin}/robots.txt")
                    rp.parse(resp.text.splitlines())
            except Exception:  # pragma: no cover - network issues
                log.debug("robots.txt unavailable for %s; allowing", origin)
                rp = None
            self._robots[origin] = rp
        rp = self._robots[origin]
        if rp is None:
            return True
        return rp.can_fetch(self.settings.user_agent, url)

    def get_text(self, url: str, *, params: dict | None = None) -> str:
        """GET a URL and return text, honouring robots + throttling."""
        if not self._robots_allows(url):
            raise SourceError(f"robots.txt disallows fetching {url}")
        self._throttle()
        try:
            resp = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.settings.request_timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            raise SourceError(f"HTTP error for {url}: {exc}") from exc

    def get_json(self, url: str, *, params: dict | None = None) -> object:
        if not self._robots_allows(url):
            raise SourceError(f"robots.txt disallows fetching {url}")
        self._throttle()
        try:
            resp = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.settings.request_timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"HTTP/JSON error for {url}: {exc}") from exc

    def get_html_with_browser(self, url: str, *, wait_selector: str | None = None) -> str:
        """Playwright fallback for JS-rendered pages.

        This behaves like a normal browser request, so it is used for sites
        that block plain HTTP clients (notably Zoopla). A short settle period
        plus a scroll lets lazy-loaded result cards render.
        """
        if not self.settings.use_playwright_fallback:
            raise SourceError("Playwright fallback disabled")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise SourceError(
                "Playwright not installed (pip install playwright && playwright install chromium)"
            ) from exc

        self._throttle()
        timeout_ms = self.settings.request_timeout * 1000
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=self.settings.user_agent,
                    locale="en-GB",
                    viewport={"width": 1440, "height": 1200},
                )
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=min(15000, timeout_ms))
                    except Exception:
                        log.debug("wait_selector %s not found on %s", wait_selector, url)
                # Trigger lazy-loaded images/cards.
                try:
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(min(self.settings.browser_settle_ms, 6000))
                except Exception:  # pragma: no cover - browser quirks
                    pass
                return page.content()
            finally:
                browser.close()

    def absolute(self, url: str) -> str:
        return urljoin(self.base_url, url)
