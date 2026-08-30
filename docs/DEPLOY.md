# Deploy

The image is built **natively on the arm64 deploy host** by
`docker compose build` — there is no registry. A push to `main` triggers
`.github/workflows/deploy.yml`, which pipes `scripts/deploy-remote.sh` over SSH;
that script renders `.env`, fast-forwards the checkout to the deploy commit, and
runs `docker compose up -d --build`.

## No inbound port, no Cloudflare route

The Telegram bot **long-polls** `getUpdates`, which is an outbound connection.
Nothing needs to reach this container, so `docker-compose.yml` publishes no port
and joins no network, and no reverse proxy or tunnel route is needed. There is
nothing to configure.

Only a move to webhooks would change that: Telegram would then need to reach the
container, which would mean publishing it behind whatever proxy the host uses.

## One-time setup on the box

```bash
git clone <this repo>
cd scraper-depas && mkdir -p data
```

`data/` holds `depas.db` plus its `-wal` / `-shm` siblings and is the only state
worth backing up. It is gitignored and never baked into the image.

## GitHub configuration

Repository **secrets**:

| Secret | Purpose |
| --- | --- |
| `SSH_PRIVATE_KEY` | Key authorised on the deploy host |
| `SSH_HOST`, `SSH_USER`, `DEPLOY_PATH` | Where to deploy |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | The group the bot posts to |

Repository **variables**: `TZ`, `DEPAS_PARKING_INCOME`, `DEPAS_STORAGE_INCOME`,
`DEPAS_ALERT_COMMUNES`, `DEPAS_ALERT_MIN_BEDROOMS`, `DEPAS_ALERT_MIN_GRADE`,
`DEPAS_TARGET_COST`, `DEPAS_ALERT_MAX_COST`, `DEPAS_ALERT_MAX_WALK`,
`DEPAS_ALERT_MIN_FLOOR`, `DEPAS_ALERT_MIN_AREA`, `DEPAS_ALERT_SECURITY`,
`DEPAS_CURRENT_COST`.

Every cost figure is the **net** monthly cost — rent plus gastos comunes minus
sublet income. There is no asking-rent setting: the rent ceiling the crawl uses
is derived as `MAX_COST + 2 × parking income + storage income`, because gastos
only add to the net and sublet is the only thing that subtracts, so rent above
that can never come in under budget.

Requirements apply at two different points. The derived rent ceiling and
`MIN_BEDROOMS` bound the scrape, so listings far outside budget never enter the
database at all — but every requirement is re-checked when announcing, because
enrichment can overwrite a card value (bedrooms included) with the detail page's.
`MAX_COST`, `MAX_WALK`, `MIN_FLOOR`, `MIN_AREA` and `SECURITY` depend on the
detail page, so they gate *alerts* instead — the listings are still stored and
queryable. Note `MAX_PRICE` is the asking rent and `MAX_COST` is what you
actually pay (rent + gastos comunes − sublet income); a listing can clear the
first and fail the second.

`TARGET_COST` is not a filter: at or under it a listing gets full marks on the
cost component, and above it the score degrades to zero at `MAX_COST`. Nothing is
excluded for being over target, it just ranks lower.

A listing that misses a requirement is left unstamped rather than marked
notified, so a later price drop can still bring it into range. A listing that
clears the requirements but misses `MIN_GRADE` *is* stamped, because the grade
is a percentile and would otherwise resurface as the pool shifts.

**Changing a variable needs a deploy** — `.env` is only re-rendered by
`scripts/deploy-remote.sh`, so re-run the workflow after editing one.

**This repository is public, so its Actions logs are public too.** Actions masks
secrets but not variables, so anything that would identify the host — hostname,
user, path — is a secret rather than a variable: otherwise one failed `ssh`
prints it for everyone. The workflow also `::add-mask::`es `ENV_B64`, because
masking covers a secret's literal value but not its base64 encoding, and that
blob carries the bot token.

Secrets travel as that single base64 blob rather than inline `VAR=value` ssh
arguments, because the remote shell re-expands `$` in those and would corrupt a
token containing one.

## Schema

`migrations/*.sql` are applied in filename order on every `connect()` and
recorded in `schema_migrations`, so a deploy migrates itself and re-running is a
no-op. Adding a column means adding `002_*.sql` — never editing `001`.

SQLite runs in WAL mode with a 5s busy timeout because the cron sidecar and the
bot share one file.

## Schedule

`deploy/crontab` drives supercronic inside `depas-cron`:

```
7 * * * * depas watch
```

Hourly at `:07` rather than `:00`, so the portal is not hit on a clock-aligned
schedule. `depas watch` scrapes `DEPAS_ALERT_COMMUNES`, then enriches only
listings where `detail_fetched_at IS NULL`.

**The first run on a fresh box is slow** — enrichment fetches one detail page per
listing behind a polite delay, so a few hundred new listings take several
minutes. Later runs only see the genuinely new ones.

## Operating

```bash
docker compose logs -f depas-cron        # watch the hourly pass
docker compose exec depas-cron depas show --limit 10
docker compose exec depas-cron depas watch   # force a pass now
```
