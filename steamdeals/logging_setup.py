"""Logging configuration with secret masking."""

from __future__ import annotations

import logging

MASK = "***"


class TokenMaskFilter(logging.Filter):
    """Replaces every occurrence of the given secrets in log records."""

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]
        self._formatter = logging.Formatter()

    def _mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self._mask(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._mask(record.exc_text)
        if record.stack_info:
            record.stack_info = self._mask(record.stack_info)
        return True


def setup_logging(level: str = "INFO", token: str | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    if token:
        handler.addFilter(TokenMaskFilter(token))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # Library chatter that can include URLs/headers.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
