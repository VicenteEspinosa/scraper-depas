# depas

Scrapes Chilean rental listings, works out what each one would **actually** cost
you, and grades it against everything else on the market.

Built for a specific question — *is this apartment a good deal?* — which the
portals themselves answer badly. Three things make the answer honest:

**Net cost, not asking rent.** If you sublet the parking space and the storage
unit, the real monthly figure is `rent + gastos comunes − parking − storage`,
with an assumed $120.000 standing in for gastos comunes nobody published. A
listing at $800.000 with two parking spaces and a bodega can land below one
asking $650.000. The portals never show this.

**Amoblado is out, full stop.** A furnished apartment is never alerted on and
never enters the pool the others are graded against — no setting turns it back on.
It is caught from the `Amoblado` spec row, the description, or the title, whichever
the portal bothered to fill in.

**A grade that means something.** Every listing gets a 0–100 **percentile
against the current pool**: `A 94` beats 94% of what is listed right now. Five
components — value against the zone, net cost, walk to the Metro, size,
amenities — with weights you control.

```
grade  on   commune      area  floor  rent    gastos        est  bod  net     metro              walk
A 99   5/5  providencia  43.0  9      600000  120000 (def)  0    1    690000  Manuel Montt       3
A 97   5/5  providencia  42.0  11     653938  160000        1    1    723938  Pedro de Valdivia  3
C 64   5/5  providencia  52.0  22     690000  80000         0    0    770000  Pedro de Valdivia  3
```

## Quick start

```bash
uv sync
cp .env.example .env          # set your lease income and search

uv run depas scrape --commune nunoa --commune providencia --max-price 900000
uv run depas enrich --limit 100
uv run depas show --max-cost 800000 --max-walk 12 --min-bedrooms 2
```

## How it works

Scraping is two-stage, because detail pages are expensive:

- **`scrape`** — cheap breadth from search-result cards.
- **`enrich`** — one detail page per listing, for the 46-field spec table,
  coordinates, the portal's routed walk times, the broker, and its own price
  benchmark. Only touches rows where `detail_fetched_at IS NULL`.
- **`watch`** — both of the above in one scheduled pass, driven by `.env`.
- **`show`** — filter and rank. Pass raw SQL instead for anything ad hoc.
- **`resend`** — drop the notified stamp from recent alerts so the next `watch`
  posts them again, which is how listings announced to the wrong chat are moved.
- **`bot`** — long-polls Telegram: grades any portal link pasted in the chat, and
  takes the verdict commands below.

### Judging a listing from the chat

Every card carries two buttons — **⭐ Me interesa** and **🚫 Descartar**. Pressing
one records the verdict, ticks the button that won, and redraws the card. Nothing
is typed and nothing is posted to the chat: the answer comes back as a toast, so
a thread of judged listings stays a thread of listings.

The same two verdicts are also commands, for cards posted before the buttons
existed. Comment on the card — in its Comments thread if alerts go to a channel,
or as a reply to it in a plain group:

| Button | Command | Effect |
| --- | --- | --- |
| ⭐ Me interesa | `/like` | Marks the listing interesting. The card gains a ⭐. |
| 🚫 Descartar | `/dislike` | Marks it out. The card gains a 🚫 and the listing leaves the pool: never announced again, gone from `show`, and no longer moving the percentiles everything else is graded against. Not even `resend` brings it back. |

Either verdict can be changed by pressing the other button; both stay live on the
card.

No webhook and no open port: presses arrive as `callback_query` updates and
commands as ordinary messages, both on the same `getUpdates` long poll the bot
already runs. A press carries the listing's row id in its `callback_data`, which
is why a button needs no context at all. A typed command instead takes its
meaning from where it was left: each card the bot posts is recorded in
`card_messages`, and when Telegram copies a channel post into the linked
discussion group, that copy's id is the thread id every comment carries — which
is how a comment is traced back to its apartment and the card itself edited in
place. A card posted before any of this existed still answers commands: the
`[id]` printed in its header is read back instead, though there is then no
message to redraw.

The buttons are copied into the discussion group along with the card, and pressing
one there redraws the channel post behind it rather than the copy, which belongs
to the channel and is not the bot's to edit.

The buttons need nothing set up. The typed commands do: the bot must be a member
of the discussion group (that is where comments actually land, not the channel),
and its privacy mode must be off in @BotFather, which it already needs to be to
see pasted links at all. Registering the two commands with `/setcommands` is
optional and only buys autocomplete.

Anyone who can see the chat can press a button — there is no per-user check, which
suits a private channel and would not suit a public one.

A verdict is a column on the listing (`interest`, `rated_at`, `rated_by`), so it
survives re-scrapes and is queryable:

```bash
uv run depas show "SELECT commune, url, rated_by FROM listings WHERE interest = 1"
```

```
depas/
  models.py      Listing + Query — the contract every portal implements
  fetch.py       HTTP session with retries and a polite delay
  store.py       SQLite: upsert, price history, migrations
  detail.py      detail-page specs → columns + a features JSON blob
  grade.py       percentile scoring
  metro.py       126 Santiago Metro stations (OpenStreetMap), distance fallback
  commute.py     travel time to your own locations, routed over buses and Metro
  uf.py          UF → CLP, so mixed-currency listings compare
  communes.py    the 43 RM communes the portal indexes
  telegram.py    Bot API client
  portals/       one module per site; a registry maps name → search()
```

Adding a portal is one file exposing `search(fetcher, query) -> Iterator[Listing]`
plus one line in the registry. Portal Inmobiliario, Houm, TocToc,
Chilepropiedades and Assetplan work; Goplaceit is a stub.

## Things the data will lie to you about

Found the hard way, and handled in code:

- **The portal's own filters leak.** A Providencia query for ≤$900.000 and 2+
  bedrooms returned 110 listings: 62 over budget, 37 with one bedroom. Every
  filter is re-checked locally.
- **Paging past the last result 404s** rather than returning an empty page.
- **An unknown commune slug doesn't reliably 404** — one returned 26.140
  nationwide results. Hence the `Commune` enum, verified against all 52 RM
  communes.
- **60% of listings are priced in UF**, so everything is normalised to CLP.
- **Gastos comunes are often 0 or absent** — publisher omission, not a free
  building. Those used to look artificially cheap, so a missing or zero figure
  now costs an assumed $120.000 (`DEFAULT_COMMON_EXPENSES`) in every net cost.
  Cards and the `gastos` column say when that default was used; filter
  `common_expenses > 0` for listings that state their own.
- **`Ambientes` is unusable** (117 of 161 null). Use `bedrooms`.
- **`Antigüedad` is two different numbers.** Some publishers put the age in years,
  others the year the building went up, and none of them say which. The ranked
  view's `age` reads anything over 100 as a year and subtracts, so both end up as
  years old; a year still in the future is a typo and floors at 0.
- **Amoblado is often only in the title.** Most portals publish no `Amoblado` spec
  row, so the exclusion also reads the description and the title. In prose,
  *cocina amoblada* is fitted cabinets rather than furniture, and does not count.
- **Every portal words availability differently.** Portal Inmobiliario leaves
  *Disponible desde* as free text and gets `INMEDIATA`, `15 julio`, `01 / 09 /
  2026`, `domingo, 4 de octubre de 2026` and `conversable`; TocToc states it as
  the project's delivery status, Houm and Assetplan as timestamps, and
  Chilepropiedades only in the description. All of it parses to one ISO date in
  `available_from`, and a date already reached reads as *entrega inmediata*. A
  month with no year means its nearest occurrence, so `Agosto` read in late
  August is that August, not next year's.
- **Assetplan's headline price is a promotion**, typically half of one month.
  The standing rent is the other figure, and that is the one stored.
- Listings graded on partial data are marked `*` with an `on` column, so a high
  score earned by dodging weak axes is visible rather than trusted.

## Configuration

Everything personal lives in `.env` (gitignored) — see `.env.example`.

| Variable | Meaning |
| --- | --- |
| `DEPAS_PARKING_INCOME`, `DEPAS_STORAGE_INCOME` | Monthly CLP you would collect subletting. Default 0 — net then equals total, rather than inventing a market rate. |
| `DEPAS_WEIGHT_*` | Relative weight per grading component. Default 1 each. |
| `DEPAS_ALERT_COMMUNES`, `DEPAS_ALERT_MAX_PRICE`, `DEPAS_ALERT_MIN_BEDROOMS` | What the scheduled `watch` pass scrapes. |
| `DEPAS_AVAILABLE_BY` | Latest move-in date you would accept, `YYYY-MM-DD`. A listing that only frees up after it is not alerted on; one that never stated a date still is, because most portals simply do not publish the field. |
| `DEPAS_TARGET_AGE` | Ideal antigüedad in years. Defaults to **25 even when unset** — unlike the other targets, leaving it blank does not switch the component off. Full marks at or under it, then the score falls away; never a cutoff, and an undeclared antigüedad is left unscored rather than assumed old. |
| `DEPAS_LOCATIONS` | `name,lat,lon` per place you need to reach, `;`-separated, any number of them. |
| `DEPAS_TARGET_COMMUTE`, `DEPAS_ALERT_MAX_COMMUTE` | Minutes to the location a listing reaches worst, by whichever of walking, bus and Metro is fastest. Full marks at or under the target, no alert over the ceiling. |
| `DEPAS_DB_PATH` | SQLite location. Defaults to `depas.db`. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. |
| `TELEGRAM_CHAT_ID` | Where alerts are posted, from `depas chats`. A **channel** with a linked discussion group gives every card its own Comments thread, which is also where `/like` and `/dislike` are read from; a group takes the cards but leaves them undiscussable, so verdicts have to be replies. Switching between the two is only this value. |

## Schema

`migrations/*.sql`, applied in filename order on every `connect()` and recorded
in `schema_migrations`. Add a column by adding `002_*.sql` — never by editing
`001`.

## Deploying

Docker, on an arm64 host, built natively — see [docs/DEPLOY.md](docs/DEPLOY.md).

## Tests

```bash
uv run pytest
```

Parsers are tested against real saved HTML, so a markup change fails loudly
instead of silently returning nothing.

## Data sources and attribution

Travel times are routed by [Transitous](https://transitous.org), a free,
community-run public-transport router — it covers the whole Red network,
buses included, from the DTPM feed. Sources and their licences are listed at
**<https://transitous.org/sources/>**. Transitous is best-effort and
**non-commercial only**; this project caches every answer for the life of a
listing, caps how many it routes per pass, and falls back to an offline
Metro-and-walking estimate whenever the service cannot answer.

Station coordinates in `metro.py` come from
[OpenStreetMap](https://www.openstreetmap.org/copyright), © OpenStreetMap
contributors, ODbL.

## Licence

MIT — see `LICENSE`. The attribution above covers the data, not the code.
