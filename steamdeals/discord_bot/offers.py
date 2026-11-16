"""Paginated, ranked offers for ``/steam_ofertas`` (embeds + navigation view)."""

from __future__ import annotations

import logging

import discord

from ..steam.money import format_brl
from ..steam.store import header_image_url

log = logging.getLogger(__name__)

PAGE_SIZE = 5  # embeds per message (Discord allows 10)
MAX_GAMES = 50
VIEW_TIMEOUT_SECONDS = 180
GREEN = 0x5BA32B
STORE_APP_URL = "https://store.steampowered.com/app/{appid}"
NO_REVIEWS_TEXT = "Sem avaliações suficientes"
RANKING_NOTE = "ordenado por relevância (avaliações, qualidade e desconto)"
REVIEWS_NOT_LOADED_NOTICE = (
    "Avaliações ainda não carregadas; serão preenchidas no próximo ciclo ou com /steam_verificar."
)
# Discord limits that matter here.
MAX_TITLE = 256
MAX_EMBED_CHARS_PER_MESSAGE = 6000


def _decimal(value: float) -> str:
    text = f"{value:.1f}".replace(".", ",")
    return text[:-2] if text.endswith(",0") else text


def compact_count(n: int) -> str:
    """Compact pt-BR number: 835, 2,2 mil, 835 mil, 1,2 mi."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{_decimal(n / 1000)} mil"
    if round(n / 1000) < 1000:
        return f"{round(n / 1000)} mil"
    return f"{_decimal(n / 1_000_000)} mi"


def review_text(review_count: int | None, percent_positive: int | None) -> str:
    if not review_count or percent_positive is None:
        return NO_REVIEWS_TEXT
    noun = "avaliação" if review_count == 1 else "avaliações"
    return f"⭐ {percent_positive}% positivas · {compact_count(review_count)} {noun}"


def build_offer_embed(deal: dict) -> discord.Embed:
    """One game of the ranking (same look as the notification embed, minus origin)."""
    appid = deal["appid"]
    name = discord.utils.escape_markdown(deal.get("name") or f"App {appid}")[:MAX_TITLE]
    embed = discord.Embed(title=name, url=STORE_APP_URL.format(appid=appid), color=GREEN)
    embed.add_field(name="Preço original", value=f"~~{format_brl(deal.get('original_cents'))}~~", inline=True)
    embed.add_field(name="Preço com desconto", value=f"**{format_brl(deal.get('final_cents'))}**", inline=True)
    embed.add_field(name="Desconto", value=f"-{int(deal.get('discount_pct') or 0)}%", inline=True)
    end = deal.get("discount_end")
    if end:
        embed.add_field(name="Termina", value=f"<t:{int(end)}:R>", inline=True)
    embed.set_image(url=deal.get("image_url") or header_image_url(appid))
    embed.set_footer(text=review_text(deal.get("review_count"), deal.get("percent_positive")))
    return embed


def build_pages(deals: list[dict]) -> list[list[discord.Embed]]:
    deals = deals[:MAX_GAMES]
    return [
        [build_offer_embed(d) for d in deals[i : i + PAGE_SIZE]] for i in range(0, len(deals), PAGE_SIZE)
    ]


def page_content(page: int, total_pages: int, notice: bool = False) -> str:
    text = f"Página {page + 1}/{total_pages} · {RANKING_NOTE}"
    if notice:
        text += f"\n{REVIEWS_NOT_LOADED_NOTICE}"
    return text


class OffersView(discord.ui.View):
    """Previous/next buttons. Only the author may use them; buttons are disabled on timeout."""

    def __init__(self, author_id: int, pages: list[list[discord.Embed]], notice: bool = False) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.author_id = author_id
        self.pages = pages
        self.notice = notice
        self.page = 0
        self.interaction: discord.Interaction | None = None  # set after the first response
        # Newest component interaction: its token is the freshest one for the timeout edit.
        self._last_interaction: discord.Interaction | None = None
        self._sync_buttons()

    @property
    def content(self) -> str:
        return page_content(self.page, len(self.pages), self.notice)

    @property
    def embeds(self) -> list[discord.Embed]:
        return self.pages[self.page]

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        try:
            await interaction.response.send_message(
                "Apenas quem executou o comando pode navegar por estas páginas.", ephemeral=True
            )
        except discord.HTTPException:
            log.warning("Could not answer a foreign interaction on the offers view")
        return False

    async def _go(self, interaction: discord.Interaction, delta: int) -> None:
        self._last_interaction = interaction
        self.page = min(max(self.page + delta, 0), len(self.pages) - 1)
        self._sync_buttons()
        try:
            await interaction.response.edit_message(content=self.content, embeds=self.embeds, view=self)
        except discord.HTTPException as exc:
            log.warning("Could not update the offers page: HTTP %s", getattr(exc, "status", "?"))

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, -1)

    @discord.ui.button(label="Próxima", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, +1)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        # Prefer the latest component interaction (newest token), fall back to the original one.
        sources = [i for i in (self._last_interaction, self.interaction) if i is not None]
        for source in sources:
            try:
                await source.edit_original_response(view=self)
                return
            except discord.HTTPException as exc:  # message deleted or webhook expired
                log.info("Offers view timeout: could not disable buttons (HTTP %s)", getattr(exc, "status", "?"))
