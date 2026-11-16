"""Discord embed for a promotion notification (pt-BR)."""

from __future__ import annotations

import discord

from ..models import ORIGIN_MANUAL, ORIGIN_SPECIALS, ORIGIN_WISHLIST
from ..steam.money import format_brl

MAX_MENTIONS = 10
STORE_APP_URL = "https://store.steampowered.com/app/{appid}"
GREEN = 0x5BA32B


def origin_label(origins: list[str] | set[str]) -> str:
    origins = set(origins)
    has_specials = ORIGIN_SPECIALS in origins
    has_watch = bool(origins & {ORIGIN_WISHLIST, ORIGIN_MANUAL})
    if has_specials and has_watch:
        return "Ofertas especiais e wishlist"
    if has_watch:
        return "Wishlist"
    return "Ofertas especiais"


def build_embed(payload: dict) -> discord.Embed:
    appid = payload["appid"]
    name = (payload.get("name") or f"App {appid}")[:256]
    embed = discord.Embed(title=name, url=STORE_APP_URL.format(appid=appid), color=GREEN)
    original = payload.get("original_cents")
    final = payload.get("final_cents")
    embed.add_field(name="Preço original", value=f"~~{format_brl(original)}~~", inline=True)
    embed.add_field(name="Preço com desconto", value=f"**{format_brl(final)}**", inline=True)
    embed.add_field(name="Desconto", value=f"-{int(payload.get('discount_pct') or 0)}%", inline=True)
    end = payload.get("discount_end")
    if end:
        embed.add_field(name="Termina", value=f"<t:{int(end)}:R>", inline=True)
    embed.add_field(name="Encontrado em", value=origin_label(payload.get("origins", [])), inline=True)
    if payload.get("image_url"):
        embed.set_image(url=payload["image_url"])
    embed.set_footer(text="Steam")
    return embed


def build_mentions(payload: dict) -> tuple[str | None, discord.AllowedMentions]:
    """Mention only the users that monitor the game (max 10), never roles/everyone."""
    user_ids = [int(u) for u in payload.get("user_ids", [])]
    shown = user_ids[:MAX_MENTIONS]
    if not shown:
        return None, discord.AllowedMentions.none()
    content = " ".join(f"<@{u}>" for u in shown)
    extra = len(user_ids) - len(shown)
    if extra > 0:
        content += f" e mais {extra}"
    allowed = discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=u) for u in shown],
        replied_user=False,
    )
    return content, allowed
