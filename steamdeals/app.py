"""Wiring of all long-lived components."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db.database import Database
from .db.repositories import Repository
from .monitor.service import MonitorService
from .notify.dispatcher import NotificationDispatcher, Sender
from .steam.http import SteamHttp
from .steam.specials import SpecialsClient
from .steam.store import StoreClient
from .steam.suggest import SuggestClient
from .steam.wishlist import WishlistClient


@dataclass
class Services:
    settings: Settings
    db: Database
    http: SteamHttp
    repo: Repository
    specials: SpecialsClient
    store: StoreClient
    wishlist: WishlistClient
    suggest: SuggestClient
    dispatcher: NotificationDispatcher
    monitor: MonitorService

    @classmethod
    async def create(cls, settings: Settings, sender: Sender) -> "Services":
        db = await Database(settings.database_path).connect()
        http = SteamHttp(max_concurrency=settings.max_concurrency)
        repo = Repository(db)
        specials = SpecialsClient(
            http,
            country=settings.steam_country,
            language=settings.search_language,
            include_non_games=settings.include_non_games,
        )
        store = StoreClient(http, country_code=settings.country_code, language=settings.steam_language)
        wishlist = WishlistClient(http)
        suggest = SuggestClient(http, country=settings.steam_country, language=settings.search_language)
        dispatcher = NotificationDispatcher(repo, sender, settings.max_notifications_per_cycle)
        monitor = MonitorService(
            repo, specials, store, wishlist,
            max_concurrency=settings.max_concurrency, dispatcher=dispatcher,
        )
        return cls(settings, db, http, repo, specials, store, wishlist, suggest, dispatcher, monitor)

    async def close(self) -> None:
        await self.http.close()
        await self.db.close()
