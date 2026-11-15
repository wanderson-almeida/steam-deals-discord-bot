"""Resilient HTTP client for Steam endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable, Mapping

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; SteamDealsBot/1.0)"
MAX_RETRY_AFTER_SECONDS = 120.0


class SteamError(Exception):
    """Base class for Steam client errors."""


class SteamUnavailable(SteamError):
    """Steam could not be reached or kept returning server errors."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SteamRateLimited(SteamError):
    """Steam answered HTTP 429 and retries were exhausted."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SteamSchemaError(SteamError):
    """The response did not have the expected structure."""


SleepFn = Callable[[float], Awaitable[None]]


class SteamHttp:
    """aiohttp wrapper with timeout, retries, backoff and bounded concurrency."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        max_concurrency: int = 5,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        sleep: SleepFn = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._rng = rng
        self._headers = {"User-Agent": user_agent, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5"}

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self._backoff_max, self._backoff_base * (2**attempt))
        return base + base * 0.25 * self._rng()

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))

    async def get_text(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        last_error: SteamError | None = None
        for attempt in range(self._max_retries + 1):
            delay: float | None = None
            try:
                async with self._semaphore:
                    session = self._get_session()
                    async with session.get(url, params=params, timeout=self._timeout) as resp:
                        status = resp.status
                        if status == 200:
                            return await resp.text()
                        retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                        # Drain/ignore body for non-200 responses.
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = SteamUnavailable(f"{type(exc).__name__}: {exc}")
                delay = self._backoff_delay(attempt)
                log.warning("Steam request failed (attempt %d): %s", attempt + 1, type(exc).__name__)
            else:
                if status == 429:
                    last_error = SteamRateLimited("HTTP 429 from Steam", retry_after)
                    delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
                    log.warning("Steam rate limited (attempt %d), waiting %.1fs", attempt + 1, delay)
                elif status >= 500:
                    last_error = SteamUnavailable(f"HTTP {status} from Steam", status)
                    delay = self._backoff_delay(attempt)
                    log.warning("Steam server error %d (attempt %d)", status, attempt + 1)
                else:
                    # Other 4xx: retrying will not help.
                    raise SteamUnavailable(f"HTTP {status} from Steam", status)
            if attempt < self._max_retries and delay is not None:
                await self._sleep(delay)
        assert last_error is not None
        raise last_error

    async def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        text = await self.get_text(url, params)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SteamSchemaError(f"Response is not valid JSON: {exc}") from exc
