"""Preview the /steam_ofertas ranking with the real Steam catalogue (no Discord involved).

Usage:
    .venv\\Scripts\\python scripts\\rank_preview.py [--top 15]

Runs ONE full monitoring cycle (about 4 minutes: ~66 search pages with the usual 3 s pause) with the
same Services as the bot, a TEMPORARY database and a fake sender, then:
    * prints catalogue statistics (games on sale, with/without review summaries, distribution),
    * prints the top games with the score components,
    * renders the first 50 games with the same embed builder as the command and reports the
      largest embed/message sizes against the Discord limits,
    * makes ONE extra GetItems request (batch of 100 from the top of the ranking) and compares the
      structured review data (``browse``) with what the search tooltip gave (``search``).

Be polite to Steam: run it rarely. It never touches ``data/``.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steamdeals import ranking  # noqa: E402
from steamdeals.app import Services  # noqa: E402
from steamdeals.config import Settings  # noqa: E402
from steamdeals.discord_bot.offers import (  # noqa: E402
    MAX_EMBED_CHARS_PER_MESSAGE,
    MAX_GAMES,
    PAGE_SIZE,
    build_pages,
    page_content,
)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, payload: dict) -> int:
        self.sent.append(payload)
        return 1


async def run(top: int) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="steamdeals-rank-"))
    settings = Settings(discord_token="not-used", notify_channel_id=1, database_path=str(tmp / "rank.db"))
    services = await Services.create(settings, FakeSender())
    try:
        summary = await services.monitor.run_cycle()
        print(f"ciclo: duracao={summary.duration:.0f}s specials_ok={summary.specials_ok} "
              f"specials={summary.specials_count} erros={summary.errors}")
        if not summary.specials_ok:
            return 1
        repo = services.repo

        async with repo.db.read() as c:
            async with c.execute("SELECT COUNT(*) FROM deal_state WHERE on_sale = 1 AND name <> ''") as cur:
                on_sale = (await cur.fetchone())[0]
            async with c.execute(
                "SELECT COUNT(*) FROM deal_state d JOIN game_reviews r ON r.appid = d.appid "
                "WHERE d.on_sale = 1 AND d.name <> '' AND r.source = 'search_partial'"
            ) as cur:
                partial_total = (await cur.fetchone())[0]
            async with c.execute(
                "SELECT r.review_count FROM deal_state d JOIN game_reviews r ON r.appid = d.appid "
                "WHERE d.on_sale = 1 AND d.name <> ''"
            ) as cur:
                counts = sorted(r[0] for r in await cur.fetchall())
        print("\n== Catalogo ==")
        print(f"jogos em oferta: {on_sale}")
        print(f"com avaliacoes: {len(counts)} ({100 * len(counts) / on_sale:.1f}%)  "
              f"sem avaliacoes: {on_sale - len(counts)} ({100 * (on_sale - len(counts)) / on_sale:.1f}%)")
        print(f"tooltips com paragrafo extra (dado parcial/subestimado): {partial_total}")
        if counts:
            print(f"avaliacoes: mediana={statistics.median(counts):.0f} "
                  f">=1k: {sum(c >= 1000 for c in counts)} >=10k: {sum(c >= 10_000 for c in counts)} "
                  f">=100k: {sum(c >= 100_000 for c in counts)} max={counts[-1]}")

        ranked = await repo.top_ranked_deals(MAX_GAMES)
        print(f"\n== TOP {top} (score = 100*(0,60*reconhecimento + 0,25*qualidade + 0,15*desconto)) ==")
        print(f"{'#':>2} {'jogo':<38} {'desc':>5} {'%pos':>5} {'avaliacoes':>11} {'score':>6}  "
              f"{'recon':>5} {'qual':>5} {'desc':>5}")
        for i, d in enumerate(ranked[:top], 1):
            s = d["score"]
            pos = "-" if d["percent_positive"] is None else f"{d['percent_positive']}%"
            mark = "*" if d["review_source"] == "search_partial" else " "
            n = "-" if d["review_count"] is None else str(d["review_count"])
            print(f"{i:>2} {d['name'][:38]:<38} {d['discount_pct']:>4}% {pos:>5} {n:>10}{mark} {s.score:>6.1f}  "
                  f"{s.recognition:>5.2f} {s.quality:>5.2f} {s.discount:>5.2f}")
        print("(* = dado parcial: contagem de busca com paragrafo extra, subestimada)")
        partial_top = sum(1 for d in ranked if d["review_source"] == "search_partial")
        print(f"itens do top {len(ranked)} com dado parcial: {partial_top}")
        no_reviews_top50 = sum(1 for d in ranked if d["review_count"] is None)
        print(f"itens do top {len(ranked)} sem avaliacao: {no_reviews_top50}")
        print(f"score do #1={ranked[0]['score'].score:.1f}  do #{len(ranked)}={ranked[-1]['score'].score:.1f}")

        print("\n== Renderizacao (primeiros 50) ==")
        pages = build_pages(ranked)
        sizes = [sum(len(e) for e in page) for page in pages]
        titles = [len(e.title) for page in pages for e in page]
        field_counts = [len(e.fields) for page in pages for e in page]
        field_values = [len(f.value) for page in pages for e in page for f in e.fields]
        footers = [len(e.footer.text) for page in pages for e in page]
        contents = [len(page_content(i, len(pages), True)) for i in range(len(pages))]
        print(f"paginas={len(pages)} embeds/pagina<= {PAGE_SIZE}: {max(len(p) for p in pages)}")
        print(f"caracteres por mensagem: max={max(sizes)} (limite {MAX_EMBED_CHARS_PER_MESSAGE}) por pagina={sizes}")
        print(f"maior titulo={max(titles)} (256)  campos/embed max={max(field_counts)} (25)  "
              f"maior valor de campo={max(field_values)} (1024)  maior rodape={max(footers)} (2048)  "
              f"maior conteudo={max(contents)} (2000)")

        print("\n== Conferencia browse x search (1 requisicao GetItems de ate 100 jogos) ==")
        sample = await repo.top_ranked_deals(100)
        items, failed = await services.store.get_items([d["appid"] for d in sample])
        both = same_pct = near_count = 0
        mismatches = []
        for d in sample:
            item = items.get(d["appid"])
            if item is None or item.review_count is None or d["review_count"] is None:
                continue
            both += 1
            same_pct += abs(item.percent_positive - d["percent_positive"]) <= 1
            ok = abs(item.review_count - d["review_count"]) <= max(5, 0.05 * item.review_count)
            near_count += ok
            if not ok or abs(item.percent_positive - d["percent_positive"]) > 1:
                mismatches.append((d["appid"], d["name"][:25], d["percent_positive"], d["review_count"],
                                   item.percent_positive, item.review_count))
        browse_with = sum(1 for i in items.values() if i.review_count is not None)
        print(f"pedidos={len(sample)} recebidos={len(items)} falhos={len(failed)} com avaliacoes (browse)={browse_with}")
        print(f"comparaveis={both} mesmo % (+-1)={same_pct} contagem proxima (5%)={near_count}")
        for m in mismatches[:10]:
            print(f"  diverge: app {m[0]} {m[1]!r} search={m[2]}%/{m[3]} browse={m[4]}%/{m[5]}")
        return 0
    finally:
        await services.close()
        await asyncio.sleep(0.25)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--top", type=int, default=15, help="rows of the ranking table (default 15)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(args.top))


if __name__ == "__main__":
    raise SystemExit(main())
