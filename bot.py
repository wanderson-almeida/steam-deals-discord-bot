"""Entry point: ``python bot.py``."""

from __future__ import annotations

import logging
import sys

from steamdeals.config import ConfigError, load_settings
from steamdeals.logging_setup import setup_logging


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.discord_token)

    from steamdeals.discord_bot.client import SteamDealsBot

    bot = SteamDealsBot(settings)
    try:
        # log_handler=None: logging is configured above (with token masking).
        bot.run(settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger(__name__).exception("Bot terminated with an error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
