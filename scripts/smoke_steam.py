"""Manual smoke test against the real Steam endpoints (no Discord involved).

Usage:
    .venv\\Scripts\\python scripts\\smoke_steam.py [STEAM_ID64]

The default SteamID64 is a public profile used only as an example. The script
makes a handful of read-only requests and prints a summary.
"""

from __future__ import annotations

import asyncio
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steamdeals.steam.http import SteamHttp  # noqa: E402
from steamdeals.steam.specials import SpecialsClient  # noqa: E402
from steamdeals.steam.store import StoreClient  # noqa: E402
from steamdeals.steam.suggest import SuggestClient  # noqa: E402
from steamdeals.steam.wishlist import WishlistClient, parse_steam_id  # noqa: E402

DEFAULT_STEAM_ID = "76561198028121353"


async def main(steam_id: str) -> int:
    failures = 0
    http = SteamHttp(max_concurrency=3)
    try:
        # 1. Specials: one page with and without the base-game filter.
        print("== Specials (1 page) ==")
        all_page = await SpecialsClient(http, include_non_games=True).fetch_page(0)
        games_page = await SpecialsClient(http, include_non_games=False).fetch_page(0)
        print(f"total_count sem filtro            : {all_page.total_count}")
        print(f"total_count com category1=998     : {games_page.total_count}")
        print(f"filtro reduziu o total?           : {games_page.total_count < all_page.total_count}")
        print(f"linhas lidas (filtrado) / validas : {games_page.rows_seen} / {len(games_page.items)} "
              f"(descartadas: {games_page.skipped})")
        for item in games_page.items[:3]:
            print(f"  {item.appid} {item.title!r} {item.final_cents}/{item.original_cents} -{item.discount_pct}%")
        if not games_page.items:
            failures += 1

        # 2. GetItems for the AppIDs of that page, plus a free game (no price).
        print("== GetItems ==")
        appids = [i.appid for i in games_page.items] + [440]
        items, failed = await StoreClient(http).get_items(appids)
        types = collections.Counter(i.type for i in items.values())
        priced = sum(1 for i in items.values() if i.has_price)
        on_sale = sum(1 for i in items.values() if i.on_sale)
        with_end = sum(1 for i in items.values() if i.discount_end)
        print(f"pedidos={len(set(appids))} recebidos={len(items)} falhos={len(failed)}")
        print(f"com preco={priced} em oferta={on_sale} com data de termino={with_end} tipos={dict(types)}")
        free = items.get(440)
        print(f"TF2 (gratuito) -> tem preco? {free.has_price if free else 'ausente'}")
        if not items or failed:
            failures += 1

        # 3. Public wishlist.
        print("== Wishlist ==")
        wishlist = await WishlistClient(http).fetch(parse_steam_id(steam_id))
        print(f"steamid={steam_id} jogos={len(wishlist.appids)} vazia_ou_privada={wishlist.empty_or_private}")

        # 4. Suggestions.
        print("== Suggest ==")
        suggestions = await SuggestClient(http).search("portal")
        for s in suggestions[:3]:
            print(f"  {s.appid} {s.name!r} {s.price}")
        if not suggestions:
            failures += 1
    except Exception as exc:  # report, do not hide
        failures += 1
        print(f"ERRO: {type(exc).__name__}: {exc}")
    finally:
        await http.close()
    print("RESULTADO:", "OK" if failures == 0 else f"{failures} falha(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STEAM_ID)))
