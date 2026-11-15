"""Discord client: setup, command sync, monitoring loop and notification sender."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from ..app import Services
from ..config import Settings
from ..notify.dispatcher import ChannelUnavailable
from ..notify.embeds import build_embed, build_mentions
from .commands import SteamCommands, register_commands

log = logging.getLogger(__name__)


class SteamDealsBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        # Only the default intents: no privileged intents (no message content).
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.services: Services | None = None
        self._cycle_loop = tasks.loop(minutes=settings.check_interval_minutes)(self._run_cycle)
        self._cycle_loop.before_loop(self._before_cycle)
        self.tree.error(self._on_command_error)

    # ------------------------------------------------------------- lifecycle
    async def setup_hook(self) -> None:
        self.services = await Services.create(self.settings, self.send_notification)
        handlers = SteamCommands(
            self.services.repo,
            self.services.monitor,
            self.services.store,
            self.services.suggest,
            next_cycle=lambda: self._cycle_loop.next_iteration,
            interval_minutes=self.settings.check_interval_minutes,
        )
        register_commands(self.tree, handlers)
        try:
            if self.settings.guild_id:
                guild = discord.Object(id=self.settings.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except discord.HTTPException as exc:
            log.error("Slash command sync failed: HTTP %s", exc.status)
        self._cycle_loop.start()

    async def close(self) -> None:
        if self._cycle_loop.is_running():
            self._cycle_loop.cancel()
        if self.services is not None:
            await self.services.close()
            self.services = None
        await super().close()

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)

    # ------------------------------------------------------------------ loop
    async def _before_cycle(self) -> None:
        await self.wait_until_ready()

    async def _run_cycle(self) -> None:
        # Never let an exception stop the loop.
        try:
            assert self.services is not None
            await self.services.monitor.run_cycle()
        except Exception:
            log.exception("Monitoring cycle crashed")

    # --------------------------------------------------------------- sending
    async def send_notification(self, payload: dict) -> int:
        channel_id = self.settings.notify_channel_id
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise ChannelUnavailable(f"Channel {channel_id} is not a text channel")
        content, allowed = build_mentions(payload)
        message = await channel.send(content=content, embed=build_embed(payload), allowed_mentions=allowed)
        return message.id

    # ---------------------------------------------------------------- errors
    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("Command error in %s: %s", getattr(interaction.command, "name", "?"), type(error).__name__,
                  exc_info=error)
        message = "Ocorreu um erro ao executar o comando. Tente novamente em instantes."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
