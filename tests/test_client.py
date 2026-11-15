import discord
import pytest

from steamdeals.config import load_settings
from steamdeals.discord_bot.client import SteamDealsBot
from steamdeals.notify.dispatcher import ChannelUnavailable


def make_bot():
    return SteamDealsBot(
        load_settings({"DISCORD_BOT_TOKEN": "t0ken", "NOTIFY_CHANNEL_ID": "42", "CHECK_INTERVAL_MINUTES": "7"})
    )


class FakeChannel:
    def __init__(self):
        self.kwargs = None

    async def send(self, **kwargs):
        self.kwargs = kwargs
        return type("M", (), {"id": 777})()


PAYLOAD = {"appid": 1, "name": "G", "final_cents": 100, "original_cents": 200, "discount_pct": 50,
           "origins": ["manual"], "user_ids": [5]}


async def test_bot_construction_and_loop_interval():
    bot = make_bot()
    assert bot._cycle_loop.minutes == 7
    assert not bot.intents.message_content  # no privileged intent
    assert bot.tree is not None


async def test_send_notification_uses_embed_and_restricted_mentions(monkeypatch):
    bot = make_bot()
    channel = FakeChannel()
    monkeypatch.setattr(bot, "get_channel", lambda cid: channel if cid == 42 else None)
    assert await bot.send_notification(PAYLOAD) == 777
    assert isinstance(channel.kwargs["embed"], discord.Embed)
    assert channel.kwargs["content"] == "<@5>"
    allowed = channel.kwargs["allowed_mentions"]
    assert allowed.everyone is False and allowed.roles is False


async def test_send_to_non_messageable_channel_raises(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(bot, "get_channel", lambda cid: object())
    with pytest.raises(ChannelUnavailable):
        await bot.send_notification(PAYLOAD)


async def test_cycle_errors_do_not_escape_the_loop():
    bot = make_bot()

    class Boom:
        class monitor:
            @staticmethod
            async def run_cycle():
                raise RuntimeError("x")

    bot.services = Boom()
    await bot._run_cycle()  # must swallow and log
