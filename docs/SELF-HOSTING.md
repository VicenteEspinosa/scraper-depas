# Hosting your own

This walks a fresh clone to an hourly bot posting cards into your own Telegram
chat. Nothing here is specific to my setup: the three things that are mine — the
chat, where I need to get to, and where I live now — are the three the repo does
not carry, and step 3 is where you supply yours.

Roughly twenty minutes, most of it Telegram.

## What you need

- **Python 3.12 and [uv](https://docs.astral.sh/uv/)** to run it locally.
- **A Telegram account**, for @BotFather and for the chat the cards land in.
- **A box with Docker**, if you want the hourly pass to keep running when your
  laptop sleeps. The shipped `Dockerfile` pins the **arm64** supercronic binary,
  because that is what my host is — see [Not on arm64](#not-on-arm64).

No inbound port, no domain, no reverse proxy: the bot long-polls Telegram, so
every connection is outbound. A Raspberry Pi is enough.

## 1. The bot

Talk to [@BotFather](https://t.me/BotFather):

1. `/newbot` → a name and a username. It answers with the token — that is
   `TELEGRAM_BOT_TOKEN`.
2. `/setprivacy` → pick the bot → **Disable**.

Step 2 is not optional. With privacy mode on, a bot in a group sees only
messages addressed to it, which means no pasted links to grade and no `/like`
on a card that isn't a reply to the bot itself.

## 2. The chat

Two shapes work, and the difference is whether each listing gets its own
conversation.

**A channel with a linked discussion group** is the one to want. Telegram
copies every card into the group as its own thread, so each apartment gets a
«Comentarios» button and its own thread of notes, and the verdict keyboard is
posted into that thread. Create the channel, create (or link) its discussion
group under *Manage channel → Discussion*, then add the bot **as an admin of
the channel** — it has to post — **and as a member of the group** — that is
where comments and keyboards live.

**A plain group** also works, with no threads: the cards pile into one
conversation and verdicts are replies to a card. Add the bot to the group.

Either way, post any message in the chat so the bot has seen it, then ask for
the id:

```bash
uv run depas chats
```

```
   -1001234567890  channel       Depas
   -1009876543210  supergroup    Depas — comentarios
```

Take the **channel** id if you built a channel; the discussion group's id is not
the one you want. Both are `-100`-prefixed and indistinguishable by eye, which
is why every `watch` pass logs which kind it is posting to before it posts.

## 3. Your three settings

```bash
git clone <this repo> && cd scraper-depas
uv sync
cp .env.example .env      # put the bot token in it
```

The first `depas` command opens the database and seeds the `preferences` table
from `seed.env`, so everything else already has a sensible value. Three settings
are deliberately missing from that seed, because they are yours and this repo is
public:

```bash
# where the cards go
uv run depas config set TELEGRAM_CHAT_ID -1001234567890

# every place you have to be able to reach — addresses are geocoded on the way in
uv run depas config set DEPAS_LOCATIONS "pega,Avenida Providencia 1234; gimnasio,Los Leones 500"

# your current flat, so /compare has something to measure against (optional)
uv run depas config set DEPAS_CURRENT_HOME '{"commune":"nunoa","price_clp":800000,"common_expenses":130000,"area_m2":62,"lat":-33.4559,"lon":-70.5978}'
```

Then make the rest yours. `depas config` prints every setting, its value and
where that value came from; `depas config get NAME` explains one. The ones worth
looking at first are `DEPAS_COMMUNES`, `DEPAS_COST_MAX`, `DEPAS_COST_TARGET`,
`DEPAS_BEDROOMS_MIN` and `DEPAS_GRADE_MIN` — what gets crawled and what is worth
a card. The full table is in the README's [Configuration](../README.md#configuration).

Values are checked as they are stored, so a commune that does not exist or a
half-filled `DEPAS_CURRENT_HOME` is refused while you are still typing.

## 4. A first pass, by hand

```bash
uv run depas watch          # scrape, enrich, grade, post
```

The first run is the slow one: enrichment fetches one detail page per listing
behind a polite delay, so a few hundred new listings take several minutes. Later
passes only see what is genuinely new.

If it posts nothing, it found nothing over `DEPAS_GRADE_MIN` — normal on a first
pass with a strict grade. Prove the wiring instead:

```bash
uv run depas show --limit 10   # is there anything in the database at all
uv run depas test-alert        # post the best of it, marked as a test
```

Then paste any Portal Inmobiliario link into the chat while `uv run depas bot`
runs, and press the buttons on the card it answers with.

## 5. Onto the box

```bash
git clone <this repo> && cd scraper-depas
mkdir -p data
printf 'TZ=America/Santiago\nTELEGRAM_BOT_TOKEN=123456:ABC-DEF\n' > .env
docker compose up -d --build
```

Two containers off one image: `depas-cron` runs `depas watch` hourly at `:07`
under supercronic, `depas-bot` long-polls for pasted links and verdicts. `data/`
holds the SQLite file and is the only state worth backing up — it is gitignored
and never baked into the image.

That `.env` is the whole environment. Every other setting lives in the database,
so set your three inside the container, once:

```bash
docker compose exec depas-cron depas config set TELEGRAM_CHAT_ID -1001234567890
docker compose exec depas-cron depas config set DEPAS_LOCATIONS "pega,Avenida Providencia 1234"
```

To start from settings you already tuned on your laptop, copy the database in
before the first `up` — `cp depas.db data/depas.db`. A database that already has
a `preferences` table is never re-seeded.

### Day to day

```bash
docker compose logs -f depas-cron              # the hourly pass
docker compose exec depas-cron depas watch     # force one now
docker compose exec depas-cron depas config    # what it thinks its settings are
docker compose exec depas-cron depas show --limit 10
```

Most preference edits need no restart at all: the bot re-reads the table on
every poll, and the cron pass loads it fresh. Only the two environment values
— `TELEGRAM_BOT_TOKEN` and `TZ` — need `docker compose up -d`.

### Not on arm64

`Dockerfile` downloads supercronic by URL with a pinned SHA1, and that pin is
the arm64 build. On an x86 host, swap `SUPERCRONIC_URL` to the
`supercronic-linux-amd64` asset of the same release and replace
`SUPERCRONIC_SHA1SUM` with that asset's checksum from the
[release page](https://github.com/aptible/supercronic/releases). Do not just
delete the check.

## 6. Deploying from GitHub, optionally

`.github/workflows/deploy.yml` pushes `main` to the box over SSH:
`scripts/deploy-remote.sh` renders `.env`, fast-forwards the checkout to the
deploy commit, validates the settings, and rebuilds. Details in
[DEPLOY.md](DEPLOY.md); what it needs from you is:

| Kind | Name |
| --- | --- |
| Secret | `SSH_PRIVATE_KEY`, `SSH_HOST`, `SSH_USER`, `DEPLOY_PATH` |
| Secret | `TELEGRAM_BOT_TOKEN` |
| Variable | `TZ` |

The host, user and path are **secrets rather than variables** because Actions
logs on a public repo are public and only secrets are masked — otherwise one
failed `ssh` prints your box for everyone.

Skip all of this and `git pull && docker compose up -d --build` on the box does
the same job.

## When it doesn't work

| What you see | Why |
| --- | --- |
| `set TELEGRAM_CHAT_ID (run depas chats)` | Step 2 and 3. Posting an alert without a chat raises rather than guessing. |
| `depas chats` prints nothing | The bot has not seen a message. Post in the chat and rerun; with privacy mode on it never will. |
| Cards arrive, buttons don't | In a channel with a discussion group the keyboard is the first comment in the card's thread, not on the card — Telegram gives a channel post one slot below it and «Comentarios» loses to an inline keyboard ([bugs.telegram.org/c/41803](https://bugs.telegram.org/c/41803)). The bot must be a member of the group for that to land. |
| Cards with a keyboard and no «Comentarios» | Posted before that was understood. `depas redraw` takes the keyboard off and the comments come back. |
| Pasted links get no answer | Privacy mode is still on, or `depas-bot` is not running. |
| Containers restarting in a loop | A setting the parsers refuse stops `connect()`. `docker compose run --rm depas-bot depas config check` names it. |
| Nothing posted, ever | Grades under `DEPAS_GRADE_MIN`, a `DEPAS_COST_MAX` under the market, or `DEPAS_COMMUNES` set to somewhere with no supply. `depas show` tells you which. |

## Before you point it at a portal

These are other people's servers. The defaults are deliberately polite — one
detail page at a time behind a delay, an hourly pass at `:07` rather than on the
hour, every routed commute cached for the life of the listing. Raising the rate
is easy and is the one change that will get your IP blocked, and Transitous, the
router behind the commute column, is a free community service that is
[non-commercial only](https://transitous.org). Leave the throttles alone.
