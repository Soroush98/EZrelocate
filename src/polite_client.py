"""Polite async HTTP client — concurrency cap, per-request jitter, smart retries.

Ported from EZrelocate's etl/_scrape.py::PoliteClient, with three additions for
the Apify runtime:
  * optional proxy (Apify Proxy URL) so blocked sources can rotate IPs;
  * per-request header overrides (rentfaster's Cloudflare challenge needs
    Referer / Origin / X-Requested-With);
  * an optional `impersonate` mode (curl_cffi) that forges a real browser's TLS +
    HTTP/2 fingerprint. Cloudflare's managed challenge fingerprints the TLS
    ClientHello (JA3), so plain httpx is 403'd regardless of headers or IP;
    curl_cffi clears it. We translate curl_cffi's errors back into httpx types at
    this boundary, so the retry policy below and each source's error handling stay
    engine-agnostic — only this file knows curl_cffi exists.
"""

from __future__ import annotations

import asyncio
import random

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

try:  # curl_cffi powers the impersonation path; keep httpx-only usage importable.
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    from curl_cffi.requests.exceptions import RequestException as _CurlError
except ImportError:  # pragma: no cover - curl_cffi is a hard dep in requirements
    _CurlAsyncSession = None
    _CurlError = ()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def _is_retryable(exc: BaseException) -> bool:
    """Retry only transient failures: 429, 5xx, and network/timeout errors.

    403 (bot/IP block) and 404 won't change on retry, so let them propagate
    immediately rather than burning the retry budget on a lost cause.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


_expo_wait = wait_exponential(min=2, max=15)


def _wait_with_retry_after(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        ra = exc.response.headers.get("Retry-After", "")
        if ra.isdigit():
            return min(float(ra), 60.0)
    return _expo_wait(retry_state)


class PoliteClient:
    """Async HTTP client with a concurrency cap and per-request jitter."""

    def __init__(
        self,
        *,
        max_concurrency: int = 3,
        min_delay_ms: int = 500,
        max_delay_ms: int = 1500,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy: str | None = None,
        proxy_new_url=None,
        impersonate: str | None = None,
    ) -> None:
        """proxy_new_url: async callable returning a FRESH proxy URL. When set, we
        fetch a new one (i.e. a new residential exit IP) for every request, so a
        single blocked IP can't sink the whole run. When None, a single persistent
        client is reused (optionally pinned to `proxy`).

        impersonate: a curl_cffi browser target (e.g. "chrome") that forges that
        browser's TLS/HTTP2 fingerprint to pass Cloudflare. Uses one sticky
        curl_cffi session (so the Cloudflare cookie stays paired with the pinned
        `proxy` IP); incompatible with proxy_new_url (the bypass needs a stable IP)."""
        self._sem = asyncio.Semaphore(max_concurrency)
        self._min = min_delay_ms / 1000
        self._max = max_delay_ms / 1000
        self._timeout = timeout
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
        }
        self._proxy_new_url = proxy_new_url
        self._impersonate = impersonate
        self._session = None  # curl_cffi AsyncSession, set only when impersonating
        if impersonate:
            if _CurlAsyncSession is None:
                raise RuntimeError(
                    "impersonate requires curl_cffi — add it to requirements.txt"
                )
            # One sticky session: curl_cffi keeps the Cloudflare cookie (__cf_bm)
            # in its jar and replays it on the API calls, paired to the pinned IP.
            # curl_cffi sets the impersonated browser's own UA/Accept headers; we
            # only override Accept-Language to keep the Canadian locale.
            self._session = _CurlAsyncSession(
                impersonate=impersonate,
                proxy=proxy,
                timeout=timeout,
                headers={"Accept-Language": self._headers["Accept-Language"]},
            )
            self._client = None
        else:
            # Persistent client only when we're NOT rotating the IP per request.
            self._client = (
                None
                if proxy_new_url is not None
                else httpx.AsyncClient(
                    timeout=timeout,
                    headers=self._headers,
                    follow_redirects=True,
                    proxy=proxy,
                )
            )

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *args) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._session is not None:
            await self._session.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
        cookies: dict | None = None,
    ) -> httpx.Response:
        async with self._sem:
            await asyncio.sleep(random.uniform(self._min, self._max))
            if self._session is not None:
                return await self._impersonated_get(url, headers, params, cookies)
            if self._proxy_new_url is not None:
                # Fresh residential IP per request (and per retry — a retry after a
                # block lands on a different IP).
                proxy = await self._proxy_new_url()
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    headers=self._headers,
                    follow_redirects=True,
                    proxy=proxy,
                ) as client:
                    r = await client.get(
                        url, headers=headers, params=params, cookies=cookies
                    )
                    r.raise_for_status()
                    return r
            r = await self._client.get(
                url, headers=headers, params=params, cookies=cookies
            )
            r.raise_for_status()
            return r

    async def _impersonated_get(
        self,
        url: str,
        headers: dict[str, str] | None,
        params: dict | None,
        cookies: dict | None,
    ):
        """curl_cffi GET, with errors re-cast as httpx so the caller can't tell.

        Returns the curl_cffi Response (duck-compatible: callers only touch
        ``.json()`` / ``.text`` / ``.status_code``). Transport failures become
        ``httpx.TransportError`` and HTTP >= 400 becomes ``httpx.HTTPStatusError`` —
        the exact types the retry policy (``_is_retryable``) and each source's
        ``except httpx.HTTPStatusError`` already expect. The semaphore + jitter are
        already held by the caller (``get``)."""
        try:
            r = await self._session.get(
                url, headers=headers, params=params, cookies=cookies
            )
        except _CurlError as e:  # network / TLS / proxy failure -> retryable
            raise httpx.TransportError(f"curl_cffi: {e}") from e
        if r.status_code >= 400:
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                f"HTTP {r.status_code} for {url}",
                request=req,
                response=httpx.Response(r.status_code, request=req),
            )
        return r
