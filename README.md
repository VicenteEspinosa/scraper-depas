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
never enters the pool at all — no setting turns it back on.
It is caught from the `Amoblado` spec row, the description, or the title, whichever
the portal bothered to fill in.

**A grade that means something.** Every listing is scored **against your
preferences and nothing else**, so `A 82` means it met what you asked for and beat
some of it — not that it won a bad week. Eleven components — value against the
zone, net cost, walk to the Metro, size, amenities, security, floor, Metro line,
commute, antigüedad, traits — each on the same curve out of 100: **80** on your
target, **40** on the hard bound you set, **100** a full span better than the
target, and **0** two spans past the bound. Meeting a target is deliberately not
full marks; the last fifth of every component is earned only by beating it, which
is why 80 is a very good flat. The exception is a component that can only be
matched — the conserjería you wanted, your best Metro line, carrying none of your
dislikes — which scores the full 100, because for those there is nothing better
than met. Weights you control, and the same listing grades the same tomorrow.

A card carries ✅ when it is at or past **every target it could be scored on** —
nothing compromised. Past **100** it has gone further and beaten those targets
across the board, and the card opens with a row of 💎 so it cannot be scrolled
past. Neither is common: nothing in the pool today earns either.

```
grade  on     commune      area  floor  rent    gastos        est  bod  net     metro              walk
A 87   9/9    providencia  43.0  9      600000  120000 (def)  0    1    690000  Manuel Montt       3
B 79   9/9    providencia  42.0  11     653938  160000        1    1    723938  Pedro de Valdivia  3
C 64*  7/9    providencia  52.0  22     690000  80000         0    0    770000  Pedro de Valdivia  3
```

## Quick start

```bash
uv sync
cp .env.example .env          # just the bot token; the settings come from seed.env

uv run depas scrape --commune nunoa --commune providencia --max-price 900000
uv run depas enrich --limit 100
uv run depas show --max-cost 800000 --max-walk 12 --min-bedrooms 2
```

Wiring it to your own Telegram chat and running it hourly on a box of your own is
[docs/SELF-HOSTING.md](docs/SELF-HOSTING.md), start to finish.

## How it works

Scraping is two-stage, because detail pages are expensive:

- **`scrape`** — cheap breadth from search-result cards.
- **`enrich`** — one detail page per listing, for the 46-field spec table,
  coordinates, the portal's routed walk times, the broker, and its own price
  benchmark. Only touches rows where `detail_fetched_at IS NULL`.
- **`watch`** — both of the above in one scheduled pass, driven by the stored settings.
- **`show`** — filter and rank. Pass raw SQL instead for anything ad hoc.
- **`resend`** — drop the notified stamp from recent alerts so the next `watch`
  posts them again, which is how listings announced to the wrong chat are moved.
- **`redraw`** — re-render cards already posted, newest first, with today's grades
  and today's rules. Also the repair for cards posted with a keyboard the channel
  could not afford (see below): the redraw takes it off and the «Comentarios»
  button comes back.
- **`bot`** — long-polls Telegram: grades any portal link pasted in the chat, and
  takes the verdict commands below. Re-reads the settings on every poll, so a
  preference edited while it runs takes effect without a restart.
- **`config`** — read and edit those settings; see [Configuration](#configuration).

### Judging a listing from the chat

Every card comes with two buttons — **⭐ Me interesa** and **🚫 Descartar**.
Pressing one records the verdict, redraws the card, and replaces both buttons with
a single **↩️ deshacer**, which is the way back: it clears the verdict and puts the
card and its two buttons back exactly as they were. Nothing is typed and nothing is
posted to the chat: the answer comes back as a toast, so a thread of judged listings
stays a thread of listings.

A discarded card is also cut down to what says which listing it was — grade,
commune, id, title, the 🏠 line, the price and the link. The photo stays, because
Telegram cannot turn a photo message into a text one, and deleting the card to
repost it would take its whole comment thread with it.

Where the buttons sit depends on the chat, and not by choice. In a group they are
on the card. In a channel with a discussion group they are the first comment in
the card's thread, because a channel post's «Comentarios» button and a bot's
inline keyboard share the one slot below the message and the keyboard wins: a card
that carries its own buttons there cannot be commented on at all
([bugs.telegram.org/c/41803](https://bugs.telegram.org/c/41803)). Cards posted
before that was understood are repaired by `depas redraw`.

The same two verdicts are also commands, for cards whose keyboard is out of reach
or was never posted. Comment on the card — in its Comments thread if alerts go to
a channel, or as a reply to it in a plain group:

| Button | Command | Effect |
| --- | --- | --- |
| ⭐ Me interesa | `/like` | Marks the listing interesting. The card gains a ⭐. |
| 🚫 Descartar | `/dislike` | Marks it out. The card gains a 🚫, loses everything below its price, and the listing leaves the pool: never announced again and gone from `show`. Not even `resend` brings it back. |
| ↩️ deshacer | — | Undoes whichever verdict was given: the listing goes back to unrated and the card is redrawn whole. |

A verdict is changed by undoing it and giving the other one — the card only ever
shows the buttons that make sense for the state it is in.

### Comparing a listing with where you live now

`/compare`, left in the same place a verdict is, answers with the listing set
against your own apartment figure by figure: both grades, commune, net cost and
the rent and gastos comunes behind it, surface, bedrooms, bathrooms, floor,
antigüedad, UF/m², the Metro station and its lines, the minutes to every
`DEPAS_LOCATIONS` place, and the amenities the move would gain or lose. Each line
reads `tuyo → este aviso` with the difference marked **mejor** or **peor**, and a
figure neither side states simply leaves its line out.

Your apartment is one setting, `DEPAS_CURRENT_HOME`, holding a single JSON object
whose keys are the listing column names — `depas config get DEPAS_CURRENT_HOME`
prints the format, and `seed.env` has a commented-out one.
`price_clp`, `common_expenses`, `area_m2`, `lat` and `lon` are required; the rest
is optional. Travel times are routed from the coordinates on each `/compare`, so
they are measured exactly the way a listing's are. Setting this also makes
`DEPAS_CURRENT_COST` redundant: the net cost is worked out from the same object,
sublet income included, and an explicit `DEPAS_CURRENT_COST` still overrides it.

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

Whether a card can hold the keyboard is read off `getChat`, once per chat per
process: a channel with a `linked_chat_id` is the case that cannot, so the card
goes out bare and the keyboard is posted into the thread as soon as Telegram's copy
of the card shows up — the same update the thread id is learned from. A channel
with no discussion group keeps its buttons on the card, since there are no comments
there to lose. A press in the thread rates the card the thread hangs off, redraws
that card in the channel, and redraws the keyboard where it sits, which is a separate
message from the card.

Both the buttons and the typed commands need the bot to be a member of the
discussion group — that is where comments land, and now where the keyboards live,
not the channel — and its privacy mode must be off in @BotFather, which it already
needs to be to see pasted links at all. Registering the two commands with
`/setcommands` is optional and only buys autocomplete.

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
  grade.py       scoring against your preferences
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
- Listings graded on partial data are marked `*` with an `on` column, and their
  score is shrunk toward 40 in proportion to what they left unanswered — silence
  is the one thing that cannot buy a good grade.

## Configuration

**Settings live in the database, not in the environment.** The first `connect()` on a
fresh database seeds the `preferences` table from `seed.env` — a checked-in starting
set, so a clone that was never configured still scrapes something sensible — with
anything `.env` (gitignored) or the environment says layered on top. From then on the
table is the configuration: editing either file changes nothing until you ask for it,
which is what makes a setting editable from a chat rather than from a shell on the box.

Only two things stay in the environment for good, because they are read before a
database can be opened or must not sit beside the data: `TELEGRAM_BOT_TOKEN` and
`DEPAS_DB_PATH`.

`DEPAS_LOCATIONS` is the one setting you can give in words — pass an address and the
coordinates are looked up once, on the way in:

```bash
uv run depas config set DEPAS_LOCATIONS "pega,Avenida Providencia 1234; gimnasio,Los Leones 500"
#   pega → Avenida Providencia 1234, Providencia
#   gimnasio → Avenida Los Leones, Providencia
```

```bash
uv run depas config                       # every setting, its value, and where it came from
uv run depas config get DEPAS_COST_TARGET # one setting, with what it means
uv run depas config set DEPAS_COST_TARGET 850000
uv run depas config unset DEPAS_COST_TARGET   # back to its default, or off
uv run depas config import-env --force        # pull .env in again, on purpose
uv run depas config check                     # validate .env, touching nothing
```

`config check` is what the deploy runs after building the image and before restarting
anything: a value the parsers refuse would otherwise stop `connect()`, and with
`restart: unless-stopped` that is a crash loop rather than an error somebody reads. It
also names any `DEPAS_*` key that is not a setting, which the seed would silently skip.

Every value is checked before it is stored, against the same declaration that
`config get` prints — a commune that does not exist, a date that is not a date or a
half-filled `DEPAS_CURRENT_HOME` is refused at the moment somebody types it rather
than on the next watch pass. `depas/preferences.py` holds that declaration, and it is
the only place a new setting has to be added.

Two things stay in the environment, because they are needed before a database can be
opened or must not be stored beside the data: `TELEGRAM_BOT_TOKEN` and `DEPAS_DB_PATH`.

Those two are all the deploy passes. Every setting it used to carry is stored in the
table and versioned in `seed.env`, so sending one again would be a second source that
nothing reads: the seed runs once, and the table has long since won.

The three `seed.env` leaves out — `TELEGRAM_CHAT_ID`, `DEPAS_LOCATIONS` and
`DEPAS_CURRENT_HOME`, a chat id and two sets of real coordinates that do not belong in
a public repo — are set on the box with `depas config set`. A fresh install has no chat
until you do, and says so: posting an alert without one raises rather than guessing.

| Setting | Meaning |
| --- | --- |
| `DEPAS_PARKING_INCOME`, `DEPAS_STORAGE_INCOME` | Monthly CLP you would collect subletting. Default 0 — net then equals total, rather than inventing a market rate. |
| `DEPAS_*_WEIGHT` | Relative weight per grading component. Default 1 each. |
| `DEPAS_FURNISHED`, `DEPAS_TOP_FLOOR` | What a yes/no property does to a listing: `exclude` drops it from the pool, `penalise` docks 20 points off the component it belongs to, `ignore` stops reading it. Defaults reproduce the old hardcoded behaviour — amoblado excluded, top floor docked. |
| `DEPAS_GRADE_MIN` | Lowest grade worth a card. The scale is absolute, so 80 is "everything I asked for" and the number is comparable across weeks. |
| `DEPAS_AMENITIES_TARGET` | How many of the nine amenities you expect. Full marks there, more still pays, and `0` switches the component off. Default 4. |
| `DEPAS_COMMUNES`, `DEPAS_COST_MAX`, `DEPAS_BEDROOMS_MIN` | What the scheduled `watch` pass scrapes. The rent ceiling used while crawling is derived from the cost budget, so there is no separate asking-rent setting. |
| `DEPAS_AVAILABLE_BY` | Latest move-in date you would accept, `YYYY-MM-DD`. A listing that only frees up after it is not alerted on; one that never stated a date still is, because most portals simply do not publish the field. |
| `DEPAS_AGE_TARGET` | Ideal antigüedad in years. Defaults to **25 even when unset** — unlike the other targets, leaving it blank does not switch the component off. Newer than it earns score, older loses it; never a cutoff, and an undeclared antigüedad is left unscored rather than assumed old. |
| `DEPAS_LOCATIONS` | `name,lat,lon` per place you need to reach, `;`-separated, any number of them. |
| `DEPAS_COMMUTE_TARGET`, `DEPAS_COMMUTE_MAX` | Minutes to the location a listing reaches worst, by whichever of walking, bus and Metro is fastest. The target scores 80, the ceiling 40 — and the ceiling is also the point past which there is no alert. |
| `DEPAS_CURRENT_HOME` | Your own apartment as one JSON object, which `/compare` sets a listing against and which `DEPAS_CURRENT_COST` falls back to. Requires `price_clp`, `common_expenses`, `area_m2`, `lat`, `lon`. |
| `DEPAS_DB_PATH` | SQLite location. Defaults to `depas.db`. Environment only — it says where the settings live, so it cannot be one of them. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. Environment only: a credential does not belong in the table beside the data. |
| `TELEGRAM_CHAT_ID` | Where alerts are posted, from `depas chats`. A **channel** with a linked discussion group gives every card its own Comments thread, which is also where `/like` and `/dislike` are read from; a group takes the cards but leaves them undiscussable, so verdicts have to be replies. Switching between the two is only this value. |

## Schema

`migrations/*.sql`, applied in filename order on every `connect()` and recorded
in `schema_migrations`. Add a column by adding `002_*.sql` — never by editing
`001`.

## Deploying

Docker, on an arm64 host, built natively — [docs/DEPLOY.md](docs/DEPLOY.md) is how
this one is deployed, and [docs/SELF-HOSTING.md](docs/SELF-HOSTING.md) is how to
stand up your own from a clone.

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
