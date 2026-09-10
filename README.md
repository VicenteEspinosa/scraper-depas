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
some of it — not that it won a bad week. Twelve components — value against the
zone, net cost, walk to the Metro, size, amenities, security, floor, Metro line,
commute, antigüedad, entrega, traits — each on the same curve out of 100: **80**
on your target, **40** on the hard bound you set, **100** a full span better than
the target, and **0** two spans past the bound. Meeting a target is deliberately
not full marks; the last fifth of every component is earned only by beating it,
which is why 80 is a very good flat. The exception is a component that can only be
matched — the conserjería you wanted, your best Metro line, an entrega on the date
you asked for, carrying none of your dislikes — which scores the full 100, because
for those there is nothing better than met. Weights you control, and the same
listing grades the same tomorrow.

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
- **`healthcheck`** — warns `DEPAS_ADMINS` by direct message when a stage has gone
  too long without completing: four hours for `watch` and `discover`, six for `enrich`
  and `announce`, a day for `route`. Runs every four hours from the same crontab,
  because a `watch` that crashes every hour looks exactly like a quiet market from the
  chat. `--stale-hours N` applies one patience to every stage instead.
- **`backup`** — copies the database through SQLite's backup API, **without** opening
  it the normal way, so the copy is of the schema as it stands and not as the current
  code would migrate it. Into `backups/` beside the file, keeping the last five. The
  deploy runs it before every restart; run it by hand before anything you are unsure of.
- **`show`** — filter and rank. Pass raw SQL instead for anything ad hoc.
- **`resend`** — drop the notified stamp from recent alerts so the next `watch`
  posts them again, which is how listings announced to the wrong chat are moved.
- **`redraw`** — re-render cards already posted, newest first, with today's grades
  and today's rules. Also the repair for cards posted with a keyboard the channel
  could not afford (see below): the redraw takes it off and the «Comentarios»
  button comes back.
- **`shortlist`** — re-post or re-render the pinned list of what you starred, the
  way `redraw` is the repair for a card. Verdicts keep it current on their own.
- **`bot`** — long-polls Telegram: grades any portal link pasted in the chat, takes
  the verdict commands below, and serves `/top`, the browser over the whole pool.
  Re-reads the settings on every poll, so a preference edited while it runs takes
  effect without a restart.
- **`config`** — read and edit those settings; see [Configuration](#configuration).
  The same settings are editable from Telegram with `/config`; see
  [Changing the settings from the chat](#changing-the-settings-from-the-chat).

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

The thread opens in that order too: the keyboard first, because the verdict is what
the thread is for, and the breakdown of the grade underneath it.

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

### Why a card got the grade it got

Every card explains itself, without being asked. Underneath it — under the buttons,
where there are buttons in a thread — comes the grade broken into the components it
was built from, each as a bar out of 100, sorted worst last so the row worth acting
on is the one the eye stops at:

```
📊 A 80 · 9 de 11 componentes

caminata     ██████████ 100
conserjería  ██████████ 100
metro        ██████████ 100
piso         ██████████  96
costo        ██████████  95
antigüedad   █████████·  92
precio zona  █████████·  91
comodidades  ████████··  80
metraje      ████······  45  ← lo más flojo

❓ sin puntaje: viajes · entrega
```

A component you weighted at anything but 1 says so (`×3`), because that is what
turned an average into this grade. A component nothing could answer is **named
rather than scored** — the same rule the `*` on a card follows: silence is not a
zero, and it is not a pass either.

Which is also the point of it. `metraje 45` is not a verdict on the flat, it is a
sentence about `DEPAS_AREA_TARGET` — so the breakdown is as much a way to find the
setting that is wrong as it is a way to read the listing.

A discarded card cuts its breakdown down with it, and ↩️ deshacer brings both back
whole. `depas redraw` re-renders it too, so a card and its explanation never end up
a week apart.

### The pinned list of what you starred

⭐ used to record a verdict and show you nothing: the shortlist it built was
queryable in SQL and invisible in the chat. It is now one **pinned message**,
rewritten by every verdict, every undo and every `watch` pass:

```
⭐ Tu lista · 3 deptos · 04/09 21:56

🟢 A 87 · Providencia · $690.000 · 43 m²
    tarjeta · aviso · [41]
```

Each line carries the grade **as it is graded today**, not as it was graded when the
card went out, plus two ways back: **tarjeta** is a deep link to the card itself,
where its buttons and its breakdown are, and **aviso** is the portal. A listing you
pasted into the chat never had a card of ours and gets the second alone; the `[id]`
is printed either way, so an old card still answers a command.

Telegram rejects a message past 4096 characters rather than trimming it, so the trim
is done here — what does not fit is counted (`…y 4 más`), never dropped silently.

Nothing about the list can cost a verdict: it fails to a log line and the verdict
still lands. Pinning needs rights the bot may not have in a channel, and without them
the list is still posted and still kept current — just not pinned. `depas shortlist`
re-posts or re-renders it.

### Browsing the whole pool

`/top`, in a private chat with the bot, pages through the same pool the alerts draw
from, ranked the same way, in **one message that edits itself** — not a screenful of
cards per browse. Each screen is the **text** of the card the listing would have been
posted as, under three rows of buttons: where to go (`◀️ 7/34 ▶️`), the two verdicts,
and a switch between the whole pool and just what you starred.

No photo, and that is what makes it one message. Telegram will not turn a text message
into a photo message or back, so a browser that showed photos would break on the first
listing whose portal published none — and every screen would cost a re-upload and drop
the card's 4096 characters to a caption's 1024. The photo is one tap away on the card
itself, which the pinned list links to; the browser is for scanning the pool.

A verdict given here writes the same column a card's button writes, so it marks the
listing, redraws the card it was announced on, and rewrites the pinned list.

Private chat only, and behind `DEPAS_ADMINS` — the same whitelist `/config` uses,
checked on the message **and on every press**. In a group it says so and points at
the pinned list, which answers the same question there without a public keyboard.

Nothing is stored between presses: the button carries the screen to render next, so a
keyboard left open across a restart still works, and an index into a pool that has
since shrunk lands on the last listing rather than raising.

### Changing the settings from the chat

`/config` opens the whole registry as a menu: eight groups, every setting inside one
of them, and its current value on the button so you can see what you are about to
change. Each press writes immediately and takes effect on the next pass — nothing is
restarted, because the bot reloads the preferences every poll.

**Nothing to register.** `/config` works as soon as the bot is running — and so does
`/start`, which is what the START button in a fresh private chat sends, so a person who
has just found the bot gets an answer rather than silence. `/setcommands` in @BotFather
is optional and only buys autocomplete; privacy mode has to be off for the group, which
it already does to see pasted links at all.

**Who may.** `DEPAS_ADMINS` holds the Telegram user ids allowed to edit, and it is
checked on the message *and on every press*: in a group anybody can reach the buttons
on somebody else's message. Being in the alert chat is deliberately not enough — a
channel's discussion group is joinable. `/config` from anybody else answers with their
own id, which is what they paste into `depas config set DEPAS_ADMINS` on the box or
send to somebody who is already an admin. Bootstrapping the first one is a shell
command by design; anything the chat could bootstrap, whoever got there first could.

It works in a private chat with the bot and in the discussion group. It cannot work in
the channel itself, and says so: a channel post is signed by the channel rather than by
a person, so there is nobody for the whitelist to match.

**Only valid values are offered.** The editor for a setting is chosen by the parser the
setting already declares, so what you get is what the value can be:

| What it is | How you set it |
| --- | --- |
| A weight | Six presets, `0` through `3`, the one in use ticked. |
| A number of pesos | `±$25.000` and `±$100.000`, never below zero. |
| Minutes, m², floors, years | `±1` and `±5`, with the unit shown. |
| Amoblado, último piso | The three things a trait can mean: excluir, castigar, ignorar. |
| Comunas | A paged list of the 32 in the Provincia de Santiago, each button carrying what that commune is worth — `100 Ñuñoa`, `40 Santiago`, `⬜ Macul` for one nothing even scrapes. Pressing one opens its own screen: ten presets from 100 down to 10, and **⬜ Sacar de la búsqueda**. The other eleven RM communes the portal indexes are an hour out, so they are typed rather than scrolled past every time — and once chosen, one shows up first in the list so it can be scored like any other. |
| Líneas de metro | One row per line, its tier ticked — the `>` and `,` string is rebuilt for you. |
| Entrega hasta | The first of each of the next six months. |
| Conserjería, dónde publicar | The values that appear in the database, so nothing offered could fail to match. |
| Tu depto actual | Field by field, saved only once it has everything `/compare` needs. |

What is typed is what a keyboard should not carry: an address (geocoded on the way in,
same as the CLI), somebody's user id, and the tail of a list too long to be worth
scrolling — plus the escape hatch every editor keeps, an **✏️ Escribir** and a
**🗑️ Borrar**. Typing into a list setting appends to it and de-duplicates, so it adds
to the checklist rather than replacing it. A typed value answers a force-reply prompt
that names the setting, which is how it finds its way home without any pending-edit
state to go stale. Only a prompt the bot itself posted is answered: a message shaped
like one but written by somebody else in the group is left alone, however an admin
replies to it.

Every write goes through the same path `depas config set` uses, so a value the parsers
refuse is refused here too, with the same message, before it is stored.

The one edit the menu will not make is emptying `DEPAS_ADMINS`: there would be nobody
left it would take an edit from.

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
needs to be to see pasted links at all. Registering the commands with
`/setcommands` is optional and only buys autocomplete.

Anyone who can see the chat can press a button — there is no per-user check, which
suits a private channel and would not suit a public one.

A pasted link is only fetched, and only answered with a card, when its host **is** one of
the portals or a subdomain of one — `departamento.portalinmobiliario.com` yes,
`miportalinmobiliario.com` no. The card the bot posts carries its endorsement, so a
lookalike host must never earn one.

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
  shortlist.py   the ⭐ set as one pinned message, rewritten by every verdict
  browse.py      /top: the pool paged through in one self-editing message
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

A value that was valid when it was stored can stop being valid when the code that parses
it changes — a commune dropped from the enum, say. That is logged as a warning rather than
raised: the database still opens, the bot keeps running on the last reading that did
parse, and `depas config unset NAME` is the repair. Raising would have been a crash loop
with the repair command locked out of the database.

Two things stay in the environment, because they are needed before a database can be
opened or must not be stored beside the data: `TELEGRAM_BOT_TOKEN` and `DEPAS_DB_PATH`.

Those two are all the deploy passes. Every setting it used to carry is stored in the
table and versioned in `seed.env`, so sending one again would be a second source that
nothing reads: the seed runs once, and the table has long since won.

The three `seed.env` leaves out — `TELEGRAM_CHAT_ID`, `DEPAS_LOCATIONS` and
`DEPAS_CURRENT_HOME`, a chat id and two sets of real coordinates that do not belong in
a public repo — are set on the box with `depas config set`. A fresh install has no chat
until you do, and says so: posting an alert without one raises rather than guessing.

`DEPAS_ADMINS` is the one identifying value the seed does carry, because a clone with
no admin in it can only be configured over SSH. It is the author's id: change it.

Since the seed runs **once per database**, adding a line to `seed.env` does nothing to a
deployment that already has one — `preferences_seeded` in the `settings` table is what
stops it, and the deploy never re-runs it. Apply such a change with `depas config set`,
not with `config import-env --force`, which re-imports everything and overwrites
whatever was edited from the chat since.

| Setting | Meaning |
| --- | --- |
| `DEPAS_PARKING_INCOME`, `DEPAS_STORAGE_INCOME` | Monthly CLP you would collect subletting. Default 0 — net then equals total, rather than inventing a market rate. |
| `DEPAS_*_WEIGHT` | Relative weight per grading component. Default 1 each. |
| `DEPAS_FURNISHED`, `DEPAS_TOP_FLOOR` | What a yes/no property does to a listing: `exclude` drops it from the pool, `penalise` docks 20 points off the component it belongs to, `ignore` stops reading it. Defaults reproduce the old hardcoded behaviour — amoblado excluded, top floor docked. |
| `DEPAS_GRADE_MIN` | Lowest grade worth a card. The scale is absolute, so 80 is "everything I asked for" and the number is comparable across weeks. |
| `DEPAS_AMENITIES_TARGET` | How many of the nine amenities you expect. Full marks there, more still pays, and `0` switches the component off. Default 4. |
| `DEPAS_COMMUNES`, `DEPAS_COST_MAX`, `DEPAS_BEDROOMS_MIN` | What the scheduled `watch` pass scrapes. The rent ceiling used while crawling is derived from the cost budget, so there is no separate asking-rent setting. |
| `DEPAS_COMMUNES` again, as a preference | Each commune can carry what an aviso there is worth, out of 100: `nunoa,providencia=90,santiago=40`. A commune with no number is worth 100. The score is never a cutoff — every commune listed is scraped and alerted on exactly as a plain list always was, and only the **comuna** component of the grade knows the difference. 80 is «cumple» and 40 is «al límite», the same anchors every other component is measured against, so `santiago=40` is literally "an aviso there starts at the bottom of my range". A commune the setting never names is not looked at at all, and scores 0 if a pasted link puts one in front of you anyway. With every commune at 100 there is no preference to score, so the component stays off — which is what every configuration written before this parses to. |
| `DEPAS_AVAILABILITY_TARGET` | The move-in date you are aiming for, `YYYY-MM-DD`. Scored on distance rather than as a deadline, and the two sides are not the same: everything that frees up between **today and that date** is in play, worth 80 at the near end of the window and 100 on the date itself, while past the date the score falls a whole span per week — 80 a week late, 40 two weeks late, nothing at three. Taking a flat early only costs you the overlap; taking it late leaves you nowhere to live. Never a cutoff, and a listing that stated no date is left unscored on it, because most portals simply do not publish the field. |
| `DEPAS_AGE_TARGET` | Ideal antigüedad in years. Defaults to **25 even when unset** — unlike the other targets, leaving it blank does not switch the component off. Newer than it earns score, older loses it; never a cutoff, and an undeclared antigüedad is left unscored rather than assumed old. |
| `DEPAS_LOCATIONS` | `name,lat,lon` per place you need to reach, `;`-separated, any number of them. |
| `DEPAS_COMMUTE_TARGET`, `DEPAS_COMMUTE_MAX` | Minutes to the location a listing reaches worst, by whichever of walking, bus and Metro is fastest. The target scores 80, the ceiling 40 — and the ceiling is also the point past which there is no alert. |
| `DEPAS_CURRENT_HOME` | Your own apartment as one JSON object, which `/compare` sets a listing against and which `DEPAS_CURRENT_COST` falls back to. Requires `price_clp`, `common_expenses`, `area_m2`, `lat`, `lon`. |
| `DEPAS_DB_PATH` | SQLite location. Defaults to `depas.db`. Environment only — it says where the settings live, so it cannot be one of them. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. Environment only: a credential does not belong in the table beside the data. |
| `DEPAS_ADMINS` | Numeric Telegram user ids allowed to change the settings from a chat, comma-separated. Empty is nobody, and being in the alert chat is not enough — a discussion group is joinable. Ids rather than usernames, because a username can be given away and reclaimed. **The seed carries the author's id**, so replace it with yours if you are hosting your own; `@userinfobot` tells you what it is. |
| `DEPAS_SWEEP_QUIET_PAGES` | Consecutive pages of nothing new that end a portal's sweep. Default 2; `0` reads every page always, which is what to use if you do not trust the portal's ordering. |
| `DEPAS_DEEP_SWEEP_HOURS` | How often a portal is read to the bottom regardless of the cutoff. Default 24 — the safety net that turns a wrong ordering guess into a delay rather than a loss. `0` makes every sweep deep. |
| `DEPAS_REFRESH_LIMIT` | Detail pages **re-read** per pass, on top of the new ones. A page is re-read when the price moved since it was read, or when its own backoff comes due; the two budgets are separate so a listing nobody has read yet never waits behind a re-read. Default 20, `0` never re-reads. |
| `DEPAS_DELIST_AFTER` | How many believable sweeps of a portal must fail to turn a listing up before it is marked gone. A sweep counts only if it finished *and* saw listings, so a portal that is down or whose markup moved delists nobody. Default 3. `0` never delists — and it has to be special-cased, since "at least zero sweeps" is true of every row. |
| `DEPAS_ENRICH_LIMIT`, `DEPAS_COMMUTE_LIMIT`, `DEPAS_ALERTS_LIMIT` | How much work one `watch` pass may do: detail pages fetched, listings routed, cards posted. Defaults 250, 40 and 25. The detail read is spread across the six portals at once, so 250 is about 40 per portal and a couple of minutes of the ten between runs — reading them in single file is what used to make 60 the sensible number. Routing stays at 40: one third-party host, still sequential. They belong in the table rather than in the crontab because the right figure moves with how many comunas you watch, and moving it should not need a redeploy. `0` switches a stage off. The flags still exist and override the setting for one run. |
| `DEPAS_ENRICH_ROUNDS` | How many times the detail read may repeat inside one run while the unread queue is still filling its whole budget. Default 3, so a backlog drains at up to 750 pages a run instead of waiting ten minutes per batch — and only while there is a backlog, which is what a standing higher limit could not express. `1` reads one batch and stops. Rounds × limit has to fit the window between runs; if it does not, the stage lock makes the next run a clean no-op rather than two processes fighting. |
| `DEPAS_UPDATES_LIMIT` | Listings **already posted** that get corrected per pass when they change: the card edited, its thread told what moved, and one digest naming all of them. Default 10, `0` reports no changes at all. What the budget pushes out is not stamped, so it goes out next pass. |
| `TELEGRAM_CHAT_ID` | Where alerts are posted, from `depas chats`. A **channel** with a linked discussion group gives every card its own Comments thread, which is also where `/like` and `/dislike` are read from; a group takes the cards but leaves them undiscussable, so verdicts have to be replies. Switching between the two is only this value. |

## Not re-reading pages that hold nothing new

The two portals that paginate are read only as deep as they need to be: once
`DEPAS_SWEEP_QUIET_PAGES` pages in a row bring nothing the database has not already
stored, that portal's sweep stops. Consecutive on purpose — a single stale page between
finds does not end it.

That only works if a portal returns its newest listings first, which both appear to do
and neither promises. So it is not taken on faith:

- Every `DEPAS_DEEP_SWEEP_HOURS` a portal is read all the way down anyway, so the worst
  a wrong guess costs is a day's delay rather than a listing lost for good.
- Each sweep records the deepest page a listing it had never seen turned up on. While
  that stays inside the cutoff, stopping early cannot have dropped anything — and
  `depas discover` prints a warning the first time it does not, naming the portal.

Setting either to `0` restores the old behaviour of reading every page, every time.

## When a listing comes off the market

A flat that gets rented does not tell you so; it just stops appearing. `last_seen` was
recorded from the beginning and read by nothing, so an apartment rented three weeks ago
stayed in the pool, kept skewing its comuna's median price per m², and kept sitting in
the pinned ⭐ list.

Now every sweep records what it saw, per portal per comuna, and a listing that
`DEPAS_DELIST_AFTER` believable sweeps have failed to find is marked `delisted_at` and
drops out of the pool and the rankings. A detail page answering 404 delists on its own —
that is the portal saying so outright.

One place it does **not** drop out of is the pinned ⭐ list, where it stays marked «ya no
está» instead. A flat you starred and then lost is something you want told, not
disappeared: removing it silently answers "what happened to that one?" by losing the
question.

Being wrong is cheap in both directions. A sweep that finds the listing again clears the
mark unconditionally, so a portal outage or a comuna you removed and later added back
sorts itself out. And a sweep that raised, or that came back with no listings at all,
delists nobody: from the outside, a portal whose markup changed looks exactly like a
comuna with nothing for rent, so neither is treated as evidence.

That last case gets a warning of its own. `watch` prints one when a portal's latest sweep
saw nothing where an earlier one saw plenty — the failure that used to be completely
silent, since a parser returning nothing for every card raises no error and lets the pass
report success.

## What changed about a listing since we last looked

A detail page used to be read once and never again, so gastos comunes, entrega dates and
the portal's own UF/m² were frozen at whatever they said the first time — while the rent
was refreshed hourly from the search card. A listing whose price moved was ranked on a
price-per-m² computed from the old one.

Pages are re-read now: as soon as the price moves, and otherwise on a backoff that starts
at three days and doubles for every re-read that finds nothing new, up to a month. A flat
that has been idle for two months is checked monthly; one that moved yesterday is checked
in three days.

Every field that actually moves is appended to `detail_changes`, with the current value
staying on the listing itself. The `listing_changes` view reads that together with the
price trail, so `depas show "SELECT * FROM listing_changes WHERE external_id = '...'"`
is the history of one aviso. An entrega date that slips three times means the flat has
been sitting unrented for months, and a field that used to be published and now is not
is what a broken parser looks like from the inside.

## Being told what changed

A card used to be a one-off: posted once and left there, saying for good whatever the
rent was that day. So a card aged silently, and a flat that got rented sat in the chat
looking available. Every pass now says what moved, in one of two ways.

**A card you already have** gets three things. The card itself is edited in place with
today's figures and today's grade; its Comments thread gets the diff; and one message per
pass — not one per listing — names every aviso that moved, with a link back to each card:

```
🔄 Cambió lo que ya te mandé · 3 avisos · 10/09 14:20

🟢 A 88 · era B 79 · Nunoa · $920.000
    tarjeta · aviso · [713]
    · El arriendo bajó de $1.050.000 a $920.000
    · El gasto común subió de $80.000 a $95.000

⚫ ya no está · Providencia · $890.000
    tarjeta · aviso · [688]
    · Se dio de baja: el portal ya no publica su ficha
```

**A card arriving for the first time** carries, in its thread, why it is arriving now —
but only when the listing has been stored more than a day, since a flat announced the
hour it turned up explains itself. The answer is read from the same history, and it is
usually not the one you would guess: announcing is gated on requirements a listing can
cross on its own, so a rebaja or a gasto común finally published is a likelier reason
than anything you changed. When nothing about the listing moved, the note says the wait
was ours — the detail queue is newest-first, so a flat can sit unenriched for weeks
behind the ones that turned up after it — or that it came back after being delisted.

What counts as a change is everything `detail_changes` records except three fields that
move by mechanics rather than by the flat: `published_days_ago` and `published_label`
shift on every re-read through the passing of time alone, and `zone_price_per_m2_uf` is
the comuna's median, which is the neighbourhood changing and not the apartment. A baja
and a vuelta count too, and are recorded in `delisting_events` — `delisted_at` holds a
state and says only the last one, so until now a listing could leave the pool and come
back with nothing anywhere saying it had happened.

Two budgets bound it. `DEPAS_UPDATES_LIMIT` caps the listings corrected per pass, because
two hundred moved prices is two hundred edits and ten minutes of channel; and the digest
is capped separately at Telegram's 4096 characters, which it rejects a message for rather
than trimming. Whatever either one pushes out is not stamped, so it goes out next pass.

## More than one reader

Cards go to **subscribers**, and a subscriber is a place rather than a person: a private
conversation with the bot, or a channel — whose linked discussion group carries the
comments, which is how this bot's own instance runs. `depas subscribers` manages them:

```bash
depas subscribers                      # every chat cards go to, and whose pool each shows
depas subscribers add -1001234567890   # shared: anybody's verdict counts for it
depas subscribers add 467291452 --owner 467291452   # private: only that person's does
```

Until you add one, whatever `TELEGRAM_CHAT_ID` says stands in, so nothing changes by
upgrading — and a shared channel keeps behaving exactly as it did, because a shared
subscriber counts anybody's verdict.

The split that makes this work: **a verdict belongs to a person, an announcement belongs
to a chat.** Your `/dislike` no longer empties somebody else's pool, and two people can
disagree about the same flat and each see their own ⭐ list. But a card posted in a
channel has been posted — that is a fact about the channel, not about each reader — so it
is never repeated there once per person.

A listing is only given up on for enrichment when *everybody* who has an opinion has
turned it down, so one person's dislike cannot stop another from ever seeing the flat.

**A chat you add starts on what turns up from then on.** Everything already in the
database is written off as already seen, so subscribing does not dump years of listings
into a new conversation. `depas subscribers add <chat> --catch-up` asks for the backlog
if that is what you want.

Upgrading keeps every verdict and every "already posted" mark. The old columns are
renamed to `legacy_*` rather than deleted, so the migration can be checked against the
original afterwards — `SELECT COUNT(*) FROM listings WHERE legacy_interest IS NOT NULL`
against `SELECT COUNT(*) FROM user_interest` — or redone.

One caveat worth knowing: **subscribers share one set of preferences.** Every subscriber
is graded and filtered by the same settings, so they all receive the same cards — with
their own verdicts and their own ⭐ list. Per-reader criteria is the next change.

## The pass, and its four stages

One hourly `depas watch` does everything in order and is still supported. But the work is
four stages that feed each other, and run as separate crontab entries they stop waiting
on one another:

| Stage | What it does |
| --- | --- |
| `depas discover` | Sweeps every comuna in `DEPAS_COMMUNES` across all six portals **at once**, then delists what no believable sweep turned up. |
| `depas enrich` | Reads the detail pages that are due — the new ones first, then the re-reads. |
| `depas route` | Travel times and the zone benchmarks. |
| `depas announce` | Posts what is over the bar and restates the pinned ⭐ list. |

`deploy/crontab` runs them at staggered minutes. The enrichment gets six goes an hour in
small batches rather than one big one — the same number of requests, spread out, so a
backlog clears six times faster without asking any portal for more per second — and
alerts go out every five minutes, because a pass with nothing to post asks Telegram
nothing at all. **Frequency is what decides how long a listing waits; the batch size only
decides how fast a backlog drains.** That is worth keeping straight when a card feels
late: raising `DEPAS_ALERTS_LIMIT` does nothing for a listing whose detail page has not
been read yet.

The six portals are both swept **and read** in parallel, because they are six different
hosts and `Fetcher`'s polite delay is there for the host rather than for the process; each
worker keeps that delay, so no single portal sees more requests per second than before.
Reading the detail pages was the last stage still going in single file, and it is the
expensive one: with the real shape of the queue — Portal Inmobiliario is about 40% of it —
one batch now takes 2.4× less wall clock. That headroom is what makes `DEPAS_ENRICH_LIMIT`
worth raising: the ceiling is no longer the ten minutes between runs.

**Newest first is not a queue.** Every arrival goes in *front* of what is waiting, so an
old row does not advance as time passes — it falls back. A fifth of every unread batch is
therefore spent on the rows that have waited longest, as a floor: after a flood (a comuna
added, the budget raised, a portal read to the bottom for the first time) the oldest could
otherwise wait weeks, which is how a card for a flat first seen in July turns up in
September. The share is a floor and not a carve-out — when fewer old rows are waiting than
it reserves, the newest fill the rest of the budget.

**One stage at a time.** `stage_locks` holds a row while a stage runs, so its own crontab
entry firing again mid-run is a clean no-op rather than two writers meeting. Nothing
enforced this before, and nothing needed to while a batch took two minutes of a ten-minute
window — but the batch is bigger now and may repeat while there is work left. A lock older
than thirty minutes is not a lock: one nobody released — OOM, SIGKILL, the container
restarted mid-run — would wedge the stage far worse than the overlap it prevents, and
`depas healthcheck` says when a stale stage is one that is still holding its lock.

A detail page that fails is that listing's problem and no longer the stage's. It used to
be: any status but 404 was re-raised, so a page answering 403 for good aborted the pass at
the same point every time and everything older than it in the queue went unread. Now it is
counted, warned about, and held out of the queue for an hour — not delisted, since a page
failing is about the page. All of them failing still fails the stage, because that is a
portal that moved its markup rather than one bad listing. Posting is paced the same way: Telegram's limit is per chat — twenty messages a
minute to a group or channel, about one a second to a private conversation — so a card in
the channel and its thread comment in the linked group no longer wait for each other, and
a 429 is waited out for exactly as long as Telegram asks rather than costing the chat the
rest of its pass. And one portal being down no longer costs you the other five's alerts: the
failure is recorded and the pass carries on. All six failing still fails the pass.

**Each stage keeps its own heartbeat**, and `depas healthcheck` warns about any that has
stopped completing, with its own patience per stage — discovery feeds everything
downstream and gets four hours, routing is somebody else's server and gets a day. That
closes the gap a single "the pass ran" stamp left: a stalled enrichment used to hide
behind a scrape that kept succeeding.

## Schema

`migrations/*.sql`, applied in filename order on every `connect()` and recorded
in `schema_migrations`. Add a column by adding `002_*.sql` — never by editing
`001`. The deploy takes a `depas backup` of the file before the new code opens it, so a
migration that goes wrong is a copy away from being undone; `data/backups/` keeps the
last five.

## Where this is going

[docs/MULTI-USER.md](docs/MULTI-USER.md) is the plan for letting several people, each
with their own criteria, read the same bot without stepping on one another — and the
audit of the code that preceded it.

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
