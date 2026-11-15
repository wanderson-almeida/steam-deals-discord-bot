import asyncio

import aiohttp
import pytest

from steamdeals.steam.http import (
    SteamHttp,
    SteamRateLimited,
    SteamSchemaError,
    SteamUnavailable,
)


class FakeResponse:
    def __init__(self, status=200, text="ok", headers=None):
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self):
        return self._text


class _Ctx:
    def __init__(self, session, item):
        self.session, self.item = session, item

    async def __aenter__(self):
        s = self.session
        s.active += 1
        s.max_active = max(s.max_active, s.active)
        try:
            if s.delay:
                await asyncio.sleep(s.delay)
            if isinstance(self.item, BaseException):
                raise self.item
            return self.item
        except BaseException:
            s.active -= 1
            raise

    async def __aexit__(self, *exc):
        if not isinstance(self.item, BaseException):
            self.session.active -= 1
        return False


class FakeSession:
    def __init__(self, script, delay=0.0):
        self.script = list(script)
        self.calls = []
        self.delay = delay
        self.active = 0
        self.max_active = 0

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return _Ctx(self, item)


def make(script, **kw):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    kw.setdefault("rng", lambda: 0.0)
    session = FakeSession(script, delay=kw.pop("delay", 0.0))
    return SteamHttp(session, sleep=sleep, **kw), session, sleeps


async def test_success_first_try():
    http, session, sleeps = make([FakeResponse(200, "hello")])
    assert await http.get_text("u", {"a": 1}) == "hello"
    assert len(session.calls) == 1 and sleeps == []
    assert session.calls[0][2].total == 20  # timeout configured


async def test_retries_5xx_with_exponential_backoff():
    http, session, sleeps = make([FakeResponse(500), FakeResponse(503), FakeResponse(200, "fine")])
    assert await http.get_text("u") == "fine"
    assert sleeps == [1.0, 2.0]


async def test_jitter_is_added_and_backoff_capped():
    http, _, sleeps = make([FakeResponse(500)] , max_retries=4, backoff_max=3.0, rng=lambda: 1.0)
    with pytest.raises(SteamUnavailable):
        await http.get_text("u")
    assert sleeps == [1.25, 2.5, 3.75, 3.75]


async def test_429_honours_retry_after():
    http, _, sleeps = make([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, "ok")])
    assert await http.get_text("u") == "ok"
    assert sleeps == [7.0]


async def test_429_retry_after_is_capped_and_exhaustion_raises():
    http, session, sleeps = make([FakeResponse(429, headers={"Retry-After": "9999"})], max_retries=2)
    with pytest.raises(SteamRateLimited) as exc:
        await http.get_text("u")
    assert exc.value.retry_after == 120.0
    assert sleeps == [120.0, 120.0]
    assert len(session.calls) == 3


async def test_429_without_header_uses_backoff():
    http, _, sleeps = make([FakeResponse(429), FakeResponse(200, "ok")])
    assert await http.get_text("u") == "ok"
    assert sleeps == [1.0]


async def test_timeout_is_retried_then_unavailable():
    http, session, sleeps = make([asyncio.TimeoutError()], max_retries=2)
    with pytest.raises(SteamUnavailable):
        await http.get_text("u")
    assert len(session.calls) == 3 and len(sleeps) == 2


async def test_client_error_is_retried():
    http, _, sleeps = make([aiohttp.ClientConnectionError("x"), FakeResponse(200, "ok")])
    assert await http.get_text("u") == "ok"
    assert len(sleeps) == 1


async def test_4xx_not_retried():
    http, session, sleeps = make([FakeResponse(400)])
    with pytest.raises(SteamUnavailable) as exc:
        await http.get_text("u")
    assert exc.value.status == 400
    assert len(session.calls) == 1 and sleeps == []


async def test_invalid_json_is_schema_error():
    http, _, _ = make([FakeResponse(200, "<html>")])
    with pytest.raises(SteamSchemaError):
        await http.get_json("u")


async def test_get_json_parses():
    http, _, _ = make([FakeResponse(200, '{"a": 1}')])
    assert await http.get_json("u") == {"a": 1}


async def test_concurrency_is_bounded():
    http, session, _ = make([FakeResponse(200, "ok")], max_concurrency=2, delay=0.01)
    await asyncio.gather(*(http.get_text("u") for _ in range(8)))
    assert session.max_active == 2


async def test_user_agent_is_generic():
    http = SteamHttp()
    assert "Mozilla" in http._headers["User-Agent"]
    await http.close()
