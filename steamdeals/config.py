"""Application settings loaded from environment variables (.env)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Mapping

from dotenv import load_dotenv

TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"

# Steam uses different language identifiers for the search endpoint and the
# store API (e.g. "brazilian" vs "portuguese").
_SEARCH_LANGUAGE_MAP = {"brazilian": "portuguese"}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    discord_token: str = field(repr=False)
    notify_channel_id: int
    guild_id: int | None = None
    check_interval_minutes: int = 30
    max_concurrency: int = 5
    max_notifications_per_cycle: int = 20
    include_non_games: bool = False
    database_path: str = "data/steamdeals.db"
    steam_country: str = "br"
    steam_language: str = "brazilian"
    log_level: str = "INFO"

    @property
    def search_language(self) -> str:
        return _SEARCH_LANGUAGE_MAP.get(self.steam_language, self.steam_language)

    @property
    def country_code(self) -> str:
        return self.steam_country.upper()


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_int(env, name, default, errors, *, minimum=None, maximum=None, required=False):
    raw = _get(env, name)
    if raw is None:
        if required:
            errors.append(f"{name} is required")
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer (got {raw!r})")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"{name} must be >= {minimum} (got {value})")
    if maximum is not None and value > maximum:
        errors.append(f"{name} must be <= {maximum} (got {value})")
    return value


def _parse_bool(env, name, default, errors):
    raw = _get(env, name)
    if raw is None:
        return default
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    errors.append(f"{name} must be true/false (got {raw!r})")
    return default


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings`.

    When ``env`` is None the process environment is used after loading a local
    ``.env`` file (if present). Passing a mapping skips ``.env`` loading, which
    keeps tests hermetic.
    """
    if env is None:
        load_dotenv()
        env = os.environ

    errors: list[str] = []

    token = _get(env, TOKEN_ENV_VAR)
    if token is None:
        errors.append(f"{TOKEN_ENV_VAR} is required")

    channel_id = _parse_int(env, "NOTIFY_CHANNEL_ID", 0, errors, minimum=1, required=True)
    guild_id = _parse_int(env, "GUILD_ID", None, errors, minimum=1)
    interval = _parse_int(env, "CHECK_INTERVAL_MINUTES", 30, errors, minimum=1, maximum=1440)
    concurrency = _parse_int(env, "MAX_CONCURRENCY", 5, errors, minimum=1, maximum=20)
    per_cycle = _parse_int(env, "MAX_NOTIFICATIONS_PER_CYCLE", 20, errors, minimum=1, maximum=100)
    include_non_games = _parse_bool(env, "INCLUDE_NON_GAMES", False, errors)

    country = (_get(env, "STEAM_COUNTRY") or "br").lower()
    if not (len(country) == 2 and country.isalpha()):
        errors.append(f"STEAM_COUNTRY must be a 2-letter country code (got {country!r})")

    language = (_get(env, "STEAM_LANGUAGE") or "brazilian").lower()
    if not language.isalpha():
        errors.append(f"STEAM_LANGUAGE must be alphabetic (got {language!r})")

    log_level = (_get(env, "LOG_LEVEL") or "INFO").upper()
    if not isinstance(logging.getLevelName(log_level), int):
        errors.append(f"LOG_LEVEL is not a valid level (got {log_level!r})")

    if errors:
        raise ConfigError("Invalid configuration: " + "; ".join(errors))

    return Settings(
        discord_token=token,  # type: ignore[arg-type]
        notify_channel_id=channel_id,
        guild_id=guild_id,
        check_interval_minutes=interval,
        max_concurrency=concurrency,
        max_notifications_per_cycle=per_cycle,
        include_non_games=include_non_games,
        database_path=_get(env, "DATABASE_PATH") or "data/steamdeals.db",
        steam_country=country,
        steam_language=language,
        log_level=log_level,
    )
