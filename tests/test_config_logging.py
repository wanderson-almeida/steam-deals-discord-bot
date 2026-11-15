import io
import logging

import pytest

from steamdeals.config import ConfigError, load_settings
from steamdeals.logging_setup import TokenMaskFilter

TOKEN = "MTIzNDU2.fake-token-value.abcdef"


def base_env(**extra):
    env = {"DISCORD_BOT_TOKEN": TOKEN, "NOTIFY_CHANNEL_ID": "123"}
    env.update(extra)
    return env


def test_defaults():
    s = load_settings(base_env())
    assert s.notify_channel_id == 123
    assert s.guild_id is None
    assert s.check_interval_minutes == 30
    assert s.max_concurrency == 5
    assert s.max_notifications_per_cycle == 20
    assert s.include_non_games is False
    assert s.database_path == "data/steamdeals.db"
    assert s.steam_country == "br" and s.country_code == "BR"
    assert s.steam_language == "brazilian" and s.search_language == "portuguese"
    assert s.log_level == "INFO"


def test_overrides():
    s = load_settings(
        base_env(GUILD_ID="9", CHECK_INTERVAL_MINUTES="10", INCLUDE_NON_GAMES="true", STEAM_LANGUAGE="english")
    )
    assert s.guild_id == 9 and s.check_interval_minutes == 10
    assert s.include_non_games is True
    assert s.search_language == "english"


def test_old_token_name_is_not_accepted():
    with pytest.raises(ConfigError) as exc:
        load_settings({"DISCORD_TOKEN": TOKEN, "NOTIFY_CHANNEL_ID": "1"})
    assert "DISCORD_BOT_TOKEN" in str(exc.value)


def test_errors_are_aggregated_and_clear():
    with pytest.raises(ConfigError) as exc:
        load_settings({"NOTIFY_CHANNEL_ID": "abc", "CHECK_INTERVAL_MINUTES": "0", "LOG_LEVEL": "LOUD",
                       "INCLUDE_NON_GAMES": "maybe", "STEAM_COUNTRY": "brasil"})
    msg = str(exc.value)
    for part in ("DISCORD_BOT_TOKEN is required", "NOTIFY_CHANNEL_ID must be an integer",
                 "CHECK_INTERVAL_MINUTES must be >= 1", "LOG_LEVEL", "INCLUDE_NON_GAMES", "STEAM_COUNTRY"):
        assert part in msg


def test_repr_does_not_leak_token():
    assert TOKEN not in repr(load_settings(base_env()))


def _capture(filter_):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(filter_)
    logger = logging.getLogger("test.mask." + str(id(stream)))
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, stream


def test_token_masked_in_message_args_and_exception():
    logger, stream = _capture(TokenMaskFilter(TOKEN))
    logger.info("login with %s", TOKEN)
    logger.info(f"plain {TOKEN} text")
    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        logger.exception("failed")
    out = stream.getvalue()
    assert TOKEN not in out
    assert out.count("***") >= 3
