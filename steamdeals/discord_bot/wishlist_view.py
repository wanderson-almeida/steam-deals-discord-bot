"""Paginated, price-sorted listing for ``/wishlist`` (one embed per page, navigation view)."""

from __future__ import annotations

import discord

from ..steam.money import format_brl
from .offers import OffersView

PAGE_SIZE = 10  # games per page: ~150 chars each, far below the 4096 description limit
MAX_NAME = 60
BLUE = 0x1B2838
TITLE = "Minha Wishlist — Steam"
SORT_NOTE = "ordenado por menor preço"
STORE_APP_URL = "https://store.steampowered.com/app/{appid}"
MAX_DESCRIPTION = 4096
MAX_EMBED_CHARS = 6000


def sort_games(games: list[dict]) -> list[dict]:
    """Cheapest first (free = 0 first), unknown price last; ties by name, then appid."""

    def key(g: dict):
        final = g.get("final_cents")
        name = (g.get("name") or str(g["appid"])).casefold()
        return (final is None, final if final is not None else 0, name, g["appid"])

    return sorted(games, key=key)


def _clean_name(game: dict) -> str:
    name = game.get("name") or f"App {game['appid']}"
    name = discord.utils.escape_markdown(name).replace("[", "(").replace("]", ")")
    name = " ".join(name.split())
    return name if len(name) <= MAX_NAME else name[: MAX_NAME - 1] + "…"


def price_text(game: dict) -> str:
    final = game.get("final_cents")
    if final is None:
        return "aguardando verificação" if game.get("updated_at") is None else "preço indisponível"
    original = game.get("original_cents")
    if game.get("on_sale") and original and original > final:
        pct = game.get("discount_pct") or 0
        text = f"~~{format_brl(original)}~~ **{format_brl(final)}** (-{pct}%)"
        return text + (" · gratuito" if final == 0 else "")
    if final == 0:
        return f"**{format_brl(0)}** · gratuito"
    return f"**{format_brl(final)}** · sem promoção"


def game_line(position: int, game: dict) -> str:
    link = f"[{_clean_name(game)}]({STORE_APP_URL.format(appid=game['appid'])})"
    tag = " · manual" if game.get("sources") == {"manual"} else ""
    return f"`{position:>2}.` {link} — {price_text(game)}{tag}"


def build_pages(games: list[dict]) -> list[list[discord.Embed]]:
    """Sorted snapshot split in pages; each page is a list with a single embed."""
    games = sort_games(games)
    total = len(games)
    pages_total = max(1, -(-total // PAGE_SIZE))
    pages: list[list[discord.Embed]] = []
    for index in range(pages_total):
        start = index * PAGE_SIZE
        lines = [game_line(start + i + 1, g) for i, g in enumerate(games[start : start + PAGE_SIZE])]
        embed = discord.Embed(
            title=TITLE,
            description=f"**{total}** jogo(s) monitorado(s) · {SORT_NOTE}\n\n" + "\n".join(lines),
            color=BLUE,
        )
        embed.set_footer(text=f"Página {index + 1}/{pages_total}")
        pages.append([embed])
    return pages


class WishlistView(OffersView):
    """Same navigation rules as the offers view (author only, timeout disables buttons);
    the page text lives in the embed, so the message has no extra content."""

    @property
    def content(self) -> str | None:  # type: ignore[override]
        return None
