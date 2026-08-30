# depas

Scrapes Chilean rental listings, works out what each one would **actually** cost
you, and grades it against everything else on the market.

Built for a specific question — *is this apartment a good deal?* — which the
portals themselves answer badly. Two things make the numbers honest:

**Net cost, not asking rent.** If you sublet the parking space and the storage
unit, the real monthly figure is `rent + gastos comunes − parking − storage`. A
listing at $800.000 with two parking spaces and a bodega can land below one
asking $650.000. The portals never show this.

**A grade that means something.** Every listing gets a 0–100 **percentile
against the current pool**: `A 94` beats 94% of what is listed right now. Five
components — value against the zone, net cost, walk to the Metro, size,
amenities — with weights you control.

```
grade  on   commune      area  floor  rent    gastos  est  bod  net     metro              walk
A 99   5/5  providencia  43.0  9      600000  -       0    1    570000  Manuel Montt       3
A 97   5/5  providencia  42.0  11     653938  160000  1    1    723938  Pedro de Valdivia  3
C 64   5/5  providencia  52.0  22     690000  80000   0    0    770000  Pedro de Valdivia  3
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
  building. Those look artificially cheap; filter `common_expenses > 0` when it
  matters.
- **`Ambientes` is unusable** (117 of 161 null). Use `bedrooms`.
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
| `DEPAS_LOCATIONS` | `name,lat,lon` per place you need to reach, `;`-separated, any number of them. |
| `DEPAS_TARGET_COMMUTE`, `DEPAS_ALERT_MAX_COMMUTE` | Minutes to the location a listing reaches worst, by whichever of walking, bus and Metro is fastest. Full marks at or under the target, no alert over the ceiling. |
| `DEPAS_DB_PATH` | SQLite location. Defaults to `depas.db`. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. |
| `TELEGRAM_CHAT_ID` | Where alerts are posted, from `depas chats`. A **channel** with a linked discussion group gives every card its own Comments thread; a group takes the cards but leaves them undiscussable. Switching between the two is only this value. |

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
