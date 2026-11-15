"""Slash commands (pt-BR).

User commands are open to everyone in the guild; each user can only change
their own data. ``/steam_status`` is open too, but internal error details are
shown only to members with the ``manage_guild`` permission, which is also the permission
required by ``/steam_verificar`` (manual check). All replies are
ephemeral; only promotion notifications are public.

The command logic lives in :class:`SteamCommands` (plain async methods that
take an ``Interaction``) so it can be unit-tested without Discord;
:func:`register_commands` wires those methods to the command tree.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable

import discord
from discord import app_commands

from ..steam.money import format_brl
from ..steam.wishlist import InvalidSteamId, parse_steam_id

log = logging.getLogger(__name__)

MAX_LIST_ITEMS = 25
MAX_MANUAL_GAMES = 100
IMPORT_COOLDOWN_SECONDS = 120
MAX_OFFERS = 15
BLUE = 0x1B2838
NOT_LINKED_MESSAGE = "Você ainda não vinculou uma conta Steam. Use `/steam_vincular`."

SOURCE_LABELS = {"specials": "Ofertas especiais", "wishlist": "Wishlist", "store": "Preços (GetItems)"}
STATUS_LABELS = {"ok": "ok", "partial": "parcial", "error": "falhou", "running": "em andamento"}


def _clean(text: str, limit: int = 60) -> str:
    text = discord.utils.escape_markdown(text or "").replace("[", "(").replace("]", ")")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


MAX_APPID = 2**31 - 1


def _parse_appid(text) -> int | None:
    try:
        value = int(str(text).strip())
    except ValueError:
        return None
    return value if 0 < value <= MAX_APPID else None


def _is_admin(interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False))


def _price_text(game: dict) -> str:
    final = game.get("final_cents")
    if final is None:
        return "aguardando verificação" if game.get("updated_at") is None else "sem preço"
    if game.get("on_sale"):
        return f"{format_brl(final)} (-{game.get('discount_pct') or 0}%)"
    return f"{format_brl(final)} (sem desconto)"


def _kwargs(content: str | None, embed: discord.Embed | None) -> dict:
    kwargs: dict = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    return kwargs


class SteamCommands:
    def __init__(
        self,
        repo,
        monitor,
        store,
        suggest,
        *,
        next_cycle: Callable[[], datetime | None] = lambda: None,
        interval_minutes: int = 30,
        clock: Callable[[], float] = time.monotonic,
        manual_check=None,
    ) -> None:
        self.repo = repo
        self.monitor = monitor
        self.store = store
        self.suggest = suggest
        self.next_cycle = next_cycle
        self.interval_minutes = interval_minutes
        self.clock = clock
        self._import_times: dict[int, float] = {}
        self.manual_check = manual_check

    # ----------------------------------------------------------------- utils
    @staticmethod
    async def _reply(interaction, content: str | None = None, embed: discord.Embed | None = None) -> None:
        await interaction.response.send_message(**_kwargs(content, embed))

    @staticmethod
    async def _defer(interaction) -> None:
        await interaction.response.defer(ephemeral=True)

    @staticmethod
    async def _followup(interaction, content: str | None = None, embed: discord.Embed | None = None) -> None:
        await interaction.followup.send(**_kwargs(content, embed))

    def _cooldown_left(self, user_id: int) -> int:
        last = self._import_times.get(user_id)
        if last is None:
            return 0
        return max(0, int(IMPORT_COOLDOWN_SECONDS - (self.clock() - last)))

    @staticmethod
    def _import_message(result) -> str:
        if result.status == "ok":
            return (
                f"Wishlist importada: {result.total} jogo(s) monitorado(s) "
                f"({result.added} novo(s), {result.removed} removido(s))."
            )
        if result.status == "empty_or_private":
            return (
                "A Steam retornou uma wishlist vazia. Isso acontece quando ela está **vazia ou privada** "
                "(não é possível distinguir os dois casos). Deixe o perfil e os detalhes dos jogos "
                "públicos em Steam > Perfil > Editar perfil > Configurações de privacidade e tente de novo. "
                "Os jogos que você já monitorava foram mantidos."
            )
        if result.status == "invalid_id":
            return (
                "A Steam recusou esse SteamID64 (ele não existe ou é inválido). Confira o ID e vincule de novo "
                "com `/steam_vincular`."
            )
        if result.status == "not_linked":
            return NOT_LINKED_MESSAGE
        return "Não foi possível consultar a Steam agora. Tente novamente em alguns minutos."

    # -------------------------------------------------------------- commands
    async def steam_status(self, interaction) -> None:
        admin = _is_admin(interaction)
        checks = await self.repo.get_check_states()
        counts = await self.repo.counts()
        pending = await self.repo.count_notifications("pending")
        failed = await self.repo.count_notifications("failed")
        embed = discord.Embed(title="Status do monitoramento Steam", color=BLUE)
        lines = []
        for source in ("specials", "wishlist", "store"):
            state = checks.get(source)
            label = SOURCE_LABELS[source]
            if not state:
                lines.append(f"**{label}**: ainda não verificado")
                continue
            ok_ts = _ts(state.get("last_success"))
            status = STATUS_LABELS.get(state.get("last_status") or "", state.get("last_status") or "?")
            text = f"**{label}**: {status}"
            if ok_ts:
                text += f" — último sucesso <t:{ok_ts}:R>"
            lines.append(text)
            if admin and state.get("last_error"):
                lines.append(f"   erro: `{_clean(state['last_error'], 150)}`")
        embed.add_field(name="Fontes", value="\n".join(lines), inline=False)
        embed.add_field(name="Fila pendente", value=str(pending), inline=True)
        if failed:
            embed.add_field(name="Notificações com falha definitiva", value=str(failed), inline=True)
        embed.add_field(name="Jogos monitorados", value=str(counts["monitored"]), inline=True)
        embed.add_field(name="Ofertas no banco", value=str(counts["on_sale"]), inline=True)
        nxt = self.next_cycle()
        if nxt and nxt.timestamp() > time.time():
            next_text = f"<t:{int(nxt.timestamp())}:R>"
        elif getattr(self.monitor, "running", False):
            # The loop schedules the next iteration when a cycle starts: a cycle longer than
            # the interval leaves that moment in the past.
            next_text = "ciclo em andamento"
        elif nxt:
            next_text = f"<t:{int(nxt.timestamp())}:R>"
        else:
            next_text = f"a cada {self.interval_minutes} min"
        embed.add_field(name="Próximo ciclo", value=next_text, inline=True)
        summary = self.monitor.last_summary
        if admin and summary is not None and summary.errors:
            embed.add_field(
                name="Erros do último ciclo",
                value=_clean(", ".join(summary.errors[:10]), 500),
                inline=False,
            )
        last = self.manual_check.last_result if self.manual_check is not None else None
        if admin and last is not None:
            when = int((last.finished_at or last.started_at).timestamp())
            if last.status != "ok":
                outcome = "falhou"
            elif last.errors or not last.specials_ok:
                outcome = "concluída com falhas"
            else:
                outcome = "concluída"
            text = (
                f"<@{last.user_id}> <t:{when}:R>: {outcome} em {last.duration:.0f}s, "
                f"{last.enqueued} geradas, {last.dispatched} enviadas"
            )
            if last.errors:
                text += f", falhas: {_clean(', '.join(last.errors[:5]), 200)}"
            embed.add_field(name="Última verificação manual", value=text, inline=False)
        await self._reply(interaction, embed=embed)

    async def steam_verificar(self, interaction) -> None:
        """Admin only: run a monitoring cycle now, in the background."""
        if not _is_admin(interaction):
            await self._reply(
                interaction, "Apenas membros com a permissão **Gerenciar Servidor** podem usar este comando."
            )
            return
        if self.manual_check is None:
            await self._reply(interaction, "A verificação manual não está disponível.")
            return

        async def deliver(result) -> None:
            await interaction.followup.send(**_kwargs(self._manual_result_message(result), None))

        outcome = self.manual_check.request(interaction.user.id, deliver)
        if outcome.status == "running":
            await self._reply(interaction, "Já existe uma verificação em andamento. Aguarde ela terminar.")
        elif outcome.status == "closing":
            await self._reply(interaction, "O bot está encerrando; tente novamente depois que ele voltar.")
        elif outcome.status == "cooldown":
            await self._reply(
                interaction, f"Aguarde {outcome.remaining}s antes de pedir outra verificação manual."
            )
        else:
            await self._reply(
                interaction,
                "Verificação iniciada. Ela leva cerca de 4 minutos; envio o resultado aqui quando terminar "
                "(só você vê).",
            )

    @staticmethod
    def _manual_result_message(result) -> str:
        if result.status == "skipped":
            return "Um ciclo agendado já estava em andamento; nenhuma nova verificação foi iniciada."
        if result.status == "failed":
            return "A verificação falhou por um erro interno. Veja os logs do bot."
        failures = ", ".join(result.errors[:10]) if result.errors else "nenhuma"
        partial = bool(result.errors) or not result.specials_ok
        offers = str(result.specials_count) if result.specials_ok else "falhou"
        monitored = str(result.monitored)
        if result.store_failed:
            monitored += f" ({result.store_failed} sem preço/falha)"
        lines = [
            f"Verificação concluída{' com falhas' if partial else ''} em {result.duration:.0f}s.",
            f"Ofertas: {offers}",
            f"Monitorados: {monitored}",
            f"Notificações: {result.enqueued} geradas, {result.dispatched} enviadas.",
        ]
        if result.wishlists_failed:
            lines.append(f"Wishlists com falha: {result.wishlists_failed}")
        lines.append(f"Falhas: {_clean(failures, 500)}")
        return "\n".join(lines)

    async def steam_vincular(self, interaction, steam_id: str) -> None:
        try:
            steam_id64 = parse_steam_id(steam_id)
        except InvalidSteamId as exc:
            if exc.kind == "vanity":
                msg = (
                    "URLs personalizadas (`/id/nome`) não são suportadas, pois exigiriam uma chave de API. "
                    "Use o SteamID64 (17 dígitos, começa com `7656119`) ou a URL no formato "
                    "`https://steamcommunity.com/profiles/<SteamID64>`."
                )
            else:
                msg = (
                    "SteamID64 inválido. Informe os 17 dígitos (começa com `7656119`) ou uma URL "
                    "`https://steamcommunity.com/profiles/<SteamID64>`."
                )
            await self._reply(interaction, msg)
            return
        await self._defer(interaction)
        user_id = interaction.user.id
        await self.repo.set_steam_link(user_id, steam_id64)
        left = self._cooldown_left(user_id)
        if left:
            await self._followup(
                interaction,
                f"Conta Steam `{steam_id64}` vinculada. A importação imediata está em intervalo mínimo "
                f"({left}s); a wishlist será importada automaticamente no próximo ciclo ou com `/wishlist_importar`.",
            )
            return
        result = await self.monitor.import_wishlist(user_id)
        self._import_times[user_id] = self.clock()
        await self._followup(interaction, f"Conta Steam `{steam_id64}` vinculada.\n{self._import_message(result)}")

    async def steam_desvincular(self, interaction) -> None:
        had_link, removed = await self.repo.remove_steam_link(interaction.user.id)
        if not had_link:
            await self._reply(interaction, "Você não tem nenhuma conta Steam vinculada.")
            return
        await self._reply(
            interaction,
            f"Conta Steam desvinculada. {removed} jogo(s) da wishlist deixaram de ser monitorados; "
            "os jogos adicionados manualmente foram mantidos.",
        )

    async def wishlist(self, interaction) -> None:
        games = await self.repo.user_games(interaction.user.id)
        if not games:
            await self._reply(
                interaction,
                "Você não monitora nenhum jogo. Use `/steam_vincular` ou `/wishlist_add`.",
            )
            return
        lines = []
        for g in games[:MAX_LIST_ITEMS]:
            tag = " (manual)" if g["sources"] == {"manual"} else ""
            lines.append(f"**{_clean(g['name'] or str(g['appid']))}**{tag} — {_price_text(g)}")
        extra = len(games) - MAX_LIST_ITEMS
        if extra > 0:
            lines.append(f"... e mais {extra} jogo(s).")
        embed = discord.Embed(
            title=f"Seus jogos monitorados ({len(games)})", description="\n".join(lines), color=BLUE
        )
        await self._reply(interaction, embed=embed)

    async def wishlist_add(self, interaction, appid: str) -> None:
        app_id = _parse_appid(appid)
        if app_id is None:
            await self._reply(
                interaction, "Escolha um jogo da lista de sugestões ou informe um AppID numérico."
            )
            return
        user_id = interaction.user.id
        if "manual" in await self.repo.user_sources(user_id, app_id):
            await self._reply(interaction, "Esse jogo já está na sua lista manual.")
            return
        if await self.repo.manual_count(user_id) >= MAX_MANUAL_GAMES:
            await self._reply(
                interaction, f"Limite de {MAX_MANUAL_GAMES} jogos manuais atingido. Remova algum com `/wishlist_remover`."
            )
            return
        await self._defer(interaction)
        try:
            item = await self.store.get_item(app_id)
        except Exception:
            await self._followup(interaction, "Não foi possível consultar a Steam agora. Tente novamente em instantes.")
            return
        if item is None:
            await self._followup(interaction, f"Nenhum jogo encontrado na Steam com o AppID `{app_id}`.")
            return
        game_name = item.name or f"App {app_id}"
        await self.repo.add_manual(user_id, app_id, game_name)
        await self._followup(
            interaction,
            f"**{_clean(game_name)}** foi adicionado à sua lista. Você será mencionado quando entrar em promoção.",
        )

    async def wishlist_remover(self, interaction, appid: str) -> None:
        app_id = _parse_appid(appid)
        if app_id is None:
            await self._reply(interaction, "Escolha um jogo da lista de sugestões.")
            return
        user_id = interaction.user.id
        if await self.repo.remove_manual(user_id, app_id):
            await self._reply(interaction, "Jogo removido da sua lista manual.")
            return
        if "wishlist" in await self.repo.user_sources(user_id, app_id):
            await self._reply(
                interaction,
                "Esse jogo vem da sua wishlist da Steam. Remova-o por lá (ou use `/steam_desvincular`).",
            )
            return
        await self._reply(interaction, "Esse jogo não está na sua lista manual.")

    async def wishlist_importar(self, interaction) -> None:
        user_id = interaction.user.id
        if await self.repo.get_steam_link(user_id) is None:
            await self._reply(interaction, NOT_LINKED_MESSAGE)
            return
        left = self._cooldown_left(user_id)
        if left:
            await self._reply(interaction, f"Aguarde {left}s antes de importar novamente.")
            return
        await self._defer(interaction)
        self._import_times[user_id] = self.clock()
        result = await self.monitor.import_wishlist(user_id)
        await self._followup(interaction, self._import_message(result))

    async def steam_ofertas(self, interaction, quantidade: int = 10) -> None:
        limit = max(1, min(int(quantidade), MAX_OFFERS))
        deals = await self.repo.top_deals(limit)
        if not deals:
            await self._reply(
                interaction, "Ainda não há ofertas registradas. Aguarde a primeira verificação do bot."
            )
            return
        lines = []
        for d in deals:
            url = f"https://store.steampowered.com/app/{d['appid']}"
            lines.append(
                f"**[{_clean(d['name'], 50)}]({url})** — ~~{format_brl(d['original_cents'])}~~ "
                f"**{format_brl(d['final_cents'])}** (-{d['discount_pct']}%)"
            )
        embed = discord.Embed(title=f"Maiores descontos agora ({len(deals)})", description="\n".join(lines), color=BLUE)
        await self._reply(interaction, embed=embed)

    # ---------------------------------------------------------- autocomplete
    async def autocomplete_suggest(self, interaction, current: str) -> list[app_commands.Choice[str]]:
        items = await self.suggest.search(current)
        choices = []
        for item in items[:25]:
            label = item.name if not item.price else f"{item.name} — {item.price}"
            choices.append(app_commands.Choice(name=label[:100], value=str(item.appid)))
        return choices

    async def autocomplete_manual(self, interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = (current or "").strip().lower()
        games = await self.repo.user_games(interaction.user.id)
        choices = []
        for g in games:
            if "manual" not in g["sources"]:
                continue
            name = g["name"] or str(g["appid"])
            if needle and needle not in name.lower() and needle not in str(g["appid"]):
                continue
            choices.append(app_commands.Choice(name=name[:100], value=str(g["appid"])))
            if len(choices) >= 25:
                break
        return choices


def register_commands(tree: app_commands.CommandTree, handlers: SteamCommands) -> None:
    @tree.command(name="steam_status", description="Mostra o estado do monitoramento de ofertas")
    @app_commands.guild_only()
    async def steam_status(interaction: discord.Interaction):
        await handlers.steam_status(interaction)

    @tree.command(name="steam_verificar", description="(Admin) Executa uma verificação de ofertas agora")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # UX only: the handler is the real guard
    async def steam_verificar(interaction: discord.Interaction):
        await handlers.steam_verificar(interaction)

    @tree.command(name="steam_vincular", description="Vincula sua conta Steam para monitorar sua wishlist")
    @app_commands.describe(steam_id="SteamID64 (17 dígitos) ou URL steamcommunity.com/profiles/<id>")
    @app_commands.guild_only()
    async def steam_vincular(interaction: discord.Interaction, steam_id: str):
        await handlers.steam_vincular(interaction, steam_id)

    @tree.command(name="steam_desvincular", description="Remove o vínculo com sua conta Steam")
    @app_commands.guild_only()
    async def steam_desvincular(interaction: discord.Interaction):
        await handlers.steam_desvincular(interaction)

    @tree.command(name="wishlist", description="Lista os jogos que você monitora e seus preços atuais")
    @app_commands.guild_only()
    async def wishlist(interaction: discord.Interaction):
        await handlers.wishlist(interaction)

    @tree.command(name="wishlist_add", description="Adiciona um jogo à sua lista manual de monitoramento")
    @app_commands.describe(appid="Nome do jogo (autocompletar) ou AppID numérico")
    @app_commands.guild_only()
    async def wishlist_add(interaction: discord.Interaction, appid: str):
        await handlers.wishlist_add(interaction, appid)

    @wishlist_add.autocomplete("appid")
    async def wishlist_add_autocomplete(interaction: discord.Interaction, current: str):
        return await handlers.autocomplete_suggest(interaction, current)

    @tree.command(name="wishlist_remover", description="Remove um jogo da sua lista manual")
    @app_commands.describe(appid="Jogo da sua lista manual")
    @app_commands.guild_only()
    async def wishlist_remover(interaction: discord.Interaction, appid: str):
        await handlers.wishlist_remover(interaction, appid)

    @wishlist_remover.autocomplete("appid")
    async def wishlist_remover_autocomplete(interaction: discord.Interaction, current: str):
        return await handlers.autocomplete_manual(interaction, current)

    @tree.command(name="wishlist_importar", description="Importa agora a wishlist da sua conta Steam vinculada")
    @app_commands.guild_only()
    async def wishlist_importar(interaction: discord.Interaction):
        await handlers.wishlist_importar(interaction)

    @tree.command(name="steam_ofertas", description="Mostra as maiores ofertas registradas no momento")
    @app_commands.describe(quantidade="Quantidade de ofertas (1 a 15)")
    @app_commands.guild_only()
    async def steam_ofertas(interaction: discord.Interaction, quantidade: app_commands.Range[int, 1, MAX_OFFERS] = 10):
        await handlers.steam_ofertas(interaction, quantidade)
