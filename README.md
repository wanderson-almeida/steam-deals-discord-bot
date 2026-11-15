# Steam Deals Discord Bot

A Discord bot that monitors Steam promotions and announces them in **a single channel**:

- store **special offers** (base games on sale, prices in BRL);
- games on the **wishlist** of users who linked their Steam account (public wishlist);
- games added **manually** with `/wishlist_add`.

Each promotion produces **one notification per game and per price state**, even when several users or several
origins (special offers, wishlist, manual list) contain the game. All interested users are mentioned in the same
message.

User-facing bot messages (slash command replies and notification embeds) are in Brazilian Portuguese; the code,
logs and this README are in English.

## Requirements

- Python 3.12 or newer (developed and tested on Python 3.14; `discord.py` 2.7 imports and runs normally).
- A bot created in the Discord Developer Portal.
- HTTPS access to Steam (no Steam API key is needed).
- Git, to clone the repository.

## Installation on Windows

```powershell
git clone https://github.com/wanderson-almeida/steam-deals-discord-bot.git
cd steam-deals-discord-bot
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# to run the tests:
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env
```

All commands in this document are run from the cloned directory (`steam-deals-discord-bot`).

### Creating the bot in the Developer Portal

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. On the **Bot** tab, create the bot and copy the token (**Reset Token**). Paste it into `DISCORD_BOT_TOKEN` in
   `.env`. Never share or commit this token.
3. **No** *Privileged Gateway Intent* is required (no *Message Content Intent*).
4. Under **OAuth2 > URL Generator**, select the `bot` and `applications.commands` scopes.
5. Minimum bot permissions in the notification channel: **View Channel**, **Send Messages** and **Embed Links**.
6. Open the generated URL and add the bot to your server.
7. Enable developer mode in Discord, copy the notification channel ID (`NOTIFY_CHANNEL_ID`) and, optionally, the
   server ID (`GUILD_ID`).

## Running

```powershell
.venv\Scripts\python bot.py
```

On the first cycle the bot records every existing special offer as a *baseline* **without notifying** (see
"Baseline" below). Slash commands are synced on startup; with `GUILD_ID` set the sync is immediate for that server,
without it the global commands can take up to about an hour to appear. If global commands were synced earlier with
the same bot, they stay registered and show up next to the guild commands (duplicates) until removed.

## Configuration (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | (required) | Bot token. Never written to logs (masked). |
| `NOTIFY_CHANNEL_ID` | (required) | Channel where promotions are posted. |
| `GUILD_ID` | empty | Server used for fast command sync. |
| `CHECK_INTERVAL_MINUTES` | `30` | Interval between monitoring cycles, counted from the start of one cycle to the start of the next. A cycle takes about 4 minutes, so values below 10 log a warning and are meant for short tests. |
| `MAX_CONCURRENCY` | `5` | Simultaneous requests to Steam. |
| `MAX_NOTIFICATIONS_PER_CYCLE` | `20` | Maximum notifications sent per cycle (the rest stays queued). |
| `MANUAL_CHECK_COOLDOWN_SECONDS` | `300` | Minimum seconds between manual checks (`/steam_verificar`). Global for the bot (one cycle serves every server), measured from the start of the last manual cycle. `0` disables it. |
| `INCLUDE_NON_GAMES` | `false` | `false` = base games only (`category1=998`); `true` also includes DLC, software, etc. |
| `DATABASE_PATH` | `data/steamdeals.db` | SQLite file. |
| `STEAM_COUNTRY` | `br` | Store country (prices). |
| `STEAM_LANGUAGE` | `brazilian` | Store API language (the search endpoint uses `portuguese`, mapped automatically). |
| `LOG_LEVEL` | `INFO` | Log level. |

Configuration errors (missing variable, invalid number, etc.) are listed all at once at startup.

## Commands

All commands work in servers only, and replies are **ephemeral** (only the invoking user sees them). Only promotion
notifications are public. Each user can only change their own data.

| Command | What it does |
| --- | --- |
| `/steam_status` | Last check per source, pending queue, next cycle and number of monitored games. Internal error details are shown only to members with the **Manage Server** permission. |
| `/steam_verificar` | **Administrators only** (the **Manage Server** permission, the same rule used for the details in `/steam_status`). Runs one monitoring cycle now, in the background: the reply is immediate and the result (duration, games checked, notifications enqueued and sent, failures per source) is sent as a follow-up when the cycle ends (about 4 minutes; interaction webhooks are valid for 15 minutes). |
| `/steam_vincular steam_id` | Links your account (17-digit SteamID64 or a `steamcommunity.com/profiles/<id>` URL) and imports the wishlist (respecting the minimum import interval; during the interval the next cycle imports it). Custom URLs (`/id/name`) are not supported. |
| `/steam_desvincular` | Removes the link and the games that came from the wishlist; manually added games stay. |
| `/wishlist` | Lists your monitored games with the last known price (up to 25 items plus a count). |
| `/wishlist_add appid` | Adds a game manually (autocomplete by name or numeric AppID; validated against Steam). Limit of 100 manual games per user; AppID up to 2^31-1. |
| `/wishlist_remover appid` | Removes a game from the manual list (autocomplete over your own manual games). |
| `/wishlist_importar` | Re-imports the linked wishlist now (120 s minimum interval per user). |
| `/steam_ofertas [quantidade]` | Biggest discounts recorded in the database (1 to 15, default 10). |

## Architecture

```
bot.py                      entry point
steamdeals/
  config.py                 Settings from .env
  logging_setup.py          logging + token masking
  app.py                    wiring of all components (Services)
  models.py                 DealSnapshot, promotion signature (hash)
  steam/                    http.py (retry/backoff/429), specials.py, store.py, wishlist.py, suggest.py
  db/                       database.py (aiosqlite, WAL, FKs), migrations.py, repositories.py
  monitor/service.py        cycle: sources -> detection -> queue
  notify/                   dispatcher.py (persistent queue), embeds.py
  discord_bot/              client.py (loop, sync, sending), commands.py (slash commands)
scripts/smoke_steam.py      manual validation against the real Steam (no Discord)
scripts/validate_cycle.py   full real cycles with a temporary database and a fake sender
tests/                      pytest (no network)
```

### Monitoring cycle

1. **Specials**: paginates `store.steampowered.com/search/results` (`specials=1`, 100 per page, 3 s pause between
   pages; a full cycle takes about 4 minutes). A source failure is recorded in `check_state` and the cycle continues
   with the other sources.
2. **Wishlists**: one `GetWishlist` per distinct SteamID (bounded concurrency); syncs the `wishlist` origins (removes
   what left, preserves `manual` ones).
3. **Prices**: the deduplicated set of monitored AppIDs (union of all users) is queried in batches of 100 on
   `IStoreBrowseService/GetItems` (each AppID once per cycle; falls back to `appdetails` if a batch fails).
4. **Detection**: the *signature* is `hash(final price, original price, discount %, sale epoch)` and exists only for
   items that are actually on sale. A new signature for an AppID enqueues a notification
   (`UNIQUE(appid, signature)`). Inconsistent prices (final above original, zero discount, no price) never produce a
   notification.
5. **Queue**: one notification per game/state. Origins and users to mention are merged while it is pending. When a
   newer signature is enqueued, the older pending one becomes `superseded` (it stays in the database, never silently
   deleted).
6. **Sending**: up to `MAX_NOTIFICATIONS_PER_CYCLE` per cycle, oldest first; the rest stays `pending`. The loop waits
   for a cycle to finish before starting the next iteration, and an `asyncio.Lock` additionally skips (and logs) an
   overlapping run.

### Baseline

On the first run in which the `specials` source is read successfully (`specials_baseline_done` flag in `settings`),
every existing offer is stored in `deal_state` with `baseline=1` and **without a notification**, so the channel is not
flooded. Games with a wishlist/manual origin are **not** suppressed and notify normally. After the baseline, new games
in the offers or with a changed signature notify. If the `specials` read fails, the baseline is **not** marked as done
and is retried on the next cycle. If a baseline game later becomes monitored by someone, it is announced once (it had
never been announced).

Note: the offers search does not report the end date. The end date only comes from `GetItems`, for monitored games.

**Detection behaviours to know**

- **A changed `discount_end` does not notify.** The end date is not part of the signature. If Steam extends or adjusts
  the end of a promotion that keeps the same final price, original price and discount, no new notification is sent.
  The latest known end date is still stored in `deal_state` (and recorded in `price_history`) and is applied to the
  payload of a notification that is still pending, for display. A new final price, original price or discount is a
  new promotion and notifies; learning the end date of a promotion first seen in the specials listing does not notify.
- **A sale that ended and came back notifies again.** `deal_state.sale_epoch` increases when a game goes from "not on
  sale" to "on sale"; it is part of the signature, so an identical promotion (even with the same prices) returning
  after it ended is announced again.
- **Extension versus a new period.** If the recorded end date had already passed (by more than one hour) and the same
  final price, original price and discount now report a **later** end date, it is treated as a new sale period
  (`sale_epoch` increases) and notifies once. Extending the end **before** it passes stays silent. This only applies
  to games whose end date is known (monitored games); the specials listing, which has no end date, never triggers it.
- **Known limitation: a priceless reading counts as the end of the sale.** If one cycle reads a monitored game without
  a valid price (for example a transient Steam glitch), the game is considered off sale and its pending notification
  becomes `expired`. When the identical promotion is read again, it is announced again (as a new row), so a duplicate
  notification is possible in that case; there is no "no duplicates" guarantee across such a reading.
- **Upgrading an existing database.** Migration 3 rewrites `deal_state.last_signature` of games currently on sale to
  the new signature format, so upgrading does not re-announce every game on sale. Existing notification rows keep
  their old signature (history only).
- **A -> B -> A reversion**: if the price returns to a state that was already announced, the existing row
  (`UNIQUE(appid, signature)`) is reactivated as `pending` and announced again, and the intermediate pending one (B)
  becomes `superseded`.
- **An invalid `specials` source does not erase state.** An empty list, one with fewer than 90% of `total_count`, or
  one that collapses to less than 50% of what was already known (with 20+ items) is treated as a source failure:
  nothing is cleared and the baseline is not marked. A game that disappears from the listing stops being on sale only
  after **2 consecutive cycles** of absence (`deal_state.missing_cycles`); disappearing for one cycle (page shift) and
  coming back does not re-announce. The same applies to a game that stopped being monitored (also reset when the
  origin is removed from `game_sources`).
- **`/wishlist_add` and an already announced game**: if the game is already on sale in the same state that was
  announced before, the user who adds it later does **not** trigger a new notification and is not mentioned; they are
  mentioned on following promotions (or on the still-unsent pending one, since users and origins are merged per game).
- Changing the SteamID64 with `/steam_vincular` discards the `wishlist` origins of the old account (manual ones stay).
  A wishlist result obtained for a link that changed or was removed during the request is discarded.

### Wishlist import outcomes

`steam_links.last_import_status` records the last outcome per user:

- `ok`: wishlist read and synced.
- `empty_or_private`: Steam answered `{"response":{}}` (or an empty list). Private and empty look identical, so
  existing wishlist sources are **kept** and the user is told it is "empty or private".
- `invalid_id`: Steam rejected the SteamID64 itself (HTTP 400/404, for example a well formed ID outside the valid
  range). Sources are kept; the user is told to check the ID and link again. A well formed but nonexistent profile is
  **not** rejected by Steam: it answers `{"response":{}}`, so it is reported as `empty_or_private`.
- `error`: temporary failure (HTTP 429/5xx, timeout, network error) or a malformed response. Sources are **kept**, the
  failure is never reported as "private", and the next cycle retries.

### Manual check (`/steam_verificar`)

The command runs the regular `MonitorService.run_cycle` (same baseline, dedupe, persistent queue, 20-per-cycle limit and
dispatch as the scheduled cycle) in a background `asyncio` task; it never restarts or reschedules the `tasks.loop`.

- Only members with **Manage Server** can use it; anyone else gets an ephemeral refusal. It is guild-only. The command
  is also registered with `default_member_permissions=manage_guild`, which only hides it from other members in the
  Discord UI (server admins can change that there); the check in the handler is the real guard.
- The report does not mix numbers: `Ofertas` is the number of specials read (or "falhou" when that source failed),
  `Monitorados` is the number of monitored games with the count that had no price or failed, wishlist failures are
  listed separately, and the title says "concluída com falhas" when any source reported an error.
- A scheduled tick that fires while a manual cycle is running is skipped by the monitor lock (and logged); the next
  scheduled cycle happens at the following interval, it is not postponed or restarted.
- While the bot is shutting down new manual requests are refused ("closing").
- If a cycle is already running (scheduled or manual) it says so and starts nothing. The request has no `await` between
  the checks and the task creation, so two simultaneous requests start a single cycle, and the monitor lock remains the
  guarantee against overlap: if a scheduled cycle takes the lock first, the manual run is skipped.
- Cooldown (`MANUAL_CHECK_COOLDOWN_SECONDS`, default 300) is **global** for the bot, because the cycle is global. It is
  consumed only when a cycle was really started (not when refused, skipped or already running) and the remaining time
  is shown.
- The result goes to the interaction follow-up (ephemeral). If that fails (expired webhook, HTTP error) it is only
  logged, never posted to the public channel; the last manual result (who, when, summary) is kept and shown to
  administrators in `/steam_status`. Internal error details are never sent to non-admins.
- `client.close()` cancels and waits for running manual checks before closing the HTTP session and the database.
- Command sync is unchanged: the existing `tree.sync` in `setup_hook` registers it, so the log line now reads
  `Synced 9 slash commands` (immediate with `GUILD_ID`, up to about an hour for global commands).

### "At-least-once" delivery and queue states

A notification is marked `sent` only **after** sending to Discord returns success (and the `message_id` is stored).
Failures (`HTTPException`, 429, `Forbidden`, missing channel) store `last_error` and keep it `pending` for the next
cycle without bringing the cycle down; channel-level errors (403/404/429, unavailable channel) stop the batch instead of
retrying and do **not** consume attempts, and the cycle error list (visible to admins in `/steam_status`) reports
`dispatch: channel unavailable or rate limited`. Other errors increment `attempts`; at **5 attempts** the row becomes
`failed` (a "poison" message: it stays recorded, leaves the selection, and `/steam_status` shows the count). Pending
notifications survive restarts.

States in the `notifications` table: `pending`, `sent`, `superseded` (replaced by a newer state of the same game),
`expired` (the game stopped being on sale before sending; revalidated against `deal_state` before sending) and
`failed`. None is silently deleted, but **not every enqueued notification reaches the channel**: `superseded`,
`expired` and `failed` ones are only recorded in the database. An `expired` row is not reactivated when the game returns:
going off sale and on sale again increases `sale_epoch`, so the return is a new promotion and gets a **new**
notification row (see the known limitation below).

**Risk:** if the process dies (or the write fails) exactly between a successful `send()` and the `UPDATE ... sent`, the
same notification is sent again after restart. A duplicate is possible; a notification never disappears without a
record, but the guarantees above (superseded/expired/failed) mean there is no guaranteed delivery of every observed
promotion.

## Real limitations of the Steam APIs

- The endpoints used are **not officially documented** and may change without notice (search, `GetItems`,
  `GetWishlist`, `search/suggest`, `appdetails`).
- The offers search returns **HTML inside JSON**: the parser is fragile by nature. A page with no rows while
  `total_count > 0` raises `SteamSchemaError` (the source fails visibly, without notifying wrong data).
- **Private and empty wishlists are indistinguishable** (`{"response":{}}` in both cases). The bot explains this to
  the user and, in that case, **does not erase** already imported games (use `/steam_desvincular` to clear them).
- The **end date of an offer** only comes from `GetItems`; the offers search does not report it.
- `GetItems`: batch of **100 AppIDs** adopted. In testing, 500 returned HTTP 400, 200 worked and POST returns 405.
- **Undocumented rate limits**: during validation the paginated search received HTTP 429 without a `Retry-After`
  header. The client uses exponential backoff with jitter, honours `Retry-After` when present and pauses 3 s between
  pages (with 1.6 s/page the 429 appeared after ~26 pages; with 3 s the 67 pages passed without a 429). If a page
  stays rate limited, the client pauses 60 s and retries (twice) before failing the source, which is retried on the
  next cycle. `count` above 100 is ignored by Steam.
- **Custom (vanity, `/id/name`) URLs are not supported** (resolving them would require an API key).
- `category1=998` restricts the search to base games: in real validation the total dropped from 9906 to 6592 items
  and the 100-item sample contained only `type=0` (no DLC, which appeared as `type=4` without the filter).
- The default search order (relevance) is **unstable during pagination**: in one full cycle there were 210 duplicate
  rows and about 210 missing games, which would cause false "new" games on the next cycle (137 spurious notifications
  in validation). That is why the search uses `sort_by=Name_ASC`: 6570 unique games, 0 duplicates.
- Games that go on sale as a package/bundle (`data-ds-bundleid`/`packageid`) are ignored.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

The tests do not use the network: HTTP is replaced by injected fakes and the responses come from real samples in
`tests/fixtures` (search, `GetItems`, public/private wishlist, suggestions). A fixture blocks any real network access.

Manual validation against the real Steam (read-only, no Discord):

```powershell
.venv\Scripts\python scripts\smoke_steam.py [SteamID64]
```

Full real cycles (uses a temporary database and a fake sender, never touches `data/` and never connects to Discord;
each cycle takes about 4 minutes, so keep the default of 2 consecutive cycles to be polite to Steam):

```powershell
.venv\Scripts\python scripts\validate_cycle.py [--cycles 2] [--wishlist-limit 150] [--steam-id <SteamID64>]
```

It links one test user to a public SteamID64 and caps the imported wishlist (default 150 AppIDs) to avoid hundreds of
price lookups per cycle. It prints, per cycle: duration per source, real requests per endpoint, 429s, retries, time
spent in backoff, pages, parsed items against `total_count`, duplicates, discarded rows, items without a price, price
integrity checks, event loop lag and the notifications created (with the reason for each). Cycle 1 shows the
baseline behaviour; cycle 2 shows whether anything is announced again without a real change.

### What was verified

| Item | Tested automatically | Validated with real calls (Steam) | NOT validated (Discord) |
| --- | --- | --- | --- |
| Offers search parsing, bundles, changed HTML | Yes (real fixture) | Yes (full pagination, `category1=998`) | n/a |
| `GetItems`, items without a price, batches, fallback | Yes | Yes (batch of 100; 500 = HTTP 400; POST = 405) | n/a |
| Public/private wishlist, SteamID | Yes | Yes (public test wishlist) | n/a |
| Suggestions/autocomplete (parser, cache) | Yes | Yes (`portal`) | Real autocomplete in the Discord client |
| Retry, backoff, `Retry-After`, timeout | Yes | 429 observed in the paginated search | n/a |
| Database: migrations (including the signature migration), FKs, UNIQUE, restart | Yes | n/a | n/a |
| Detection, dedupe, `discount_end` handling, baseline, queue, superseded | Yes | Full cycles with the real Steam and a fake sender (`scripts/validate_cycle.py`) | n/a |
| Coverage/absence heuristics (90% of `total_count`, 50% collapse, 2 missing cycles) | Yes (simulated incomplete listings) | Measured on real full listings | n/a |
| Wishlist empty/private/unavailable outcomes and user messages | Yes | Public wishlist only | n/a |
| Slash commands (9) and permissions, including `/steam_verificar` | Yes (fake `Interaction` objects) | n/a | **Not validated** on a real Discord server |
| Embed/mention sending, `allowed_mentions` | Yes (fake channel) | n/a | **Not validated** on real Discord |
| Command sync, loop, bot login, channel permissions | Bot construction, setup and shutdown tested | n/a | **Not validated** (requires a token and a server) |

The Discord tests use fake objects: the real Discord API behaviour (permissions, rate limits, embed and mention
rendering) still has to be checked manually on a test server.
