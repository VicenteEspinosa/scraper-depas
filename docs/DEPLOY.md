# Deploy

Same shape as `networth-api`: the image is built **natively on the Oracle ARM
(Ampere A1) box** by `docker compose build` — there is no registry. A push to
`main` triggers `.github/workflows/deploy.yml`, which pipes
`scripts/deploy-remote.sh` over SSH; that script renders `.env`, fast-forwards
the checkout to the deploy commit, and runs `docker compose up -d --build`.

## No inbound port, no Cloudflare route

The Telegram bot **long-polls** `getUpdates`, which is an outbound connection.
Nothing needs to reach this container, so `docker-compose.yml` publishes no port
and joins no network — it stays off `smart-home-net` and needs no `cloudflared`
route. That is the whole Cloudflare story here: there is nothing to configure.

If the bot ever moves to webhooks, Telegram would need to reach us and the
service would then join `smart-home-net` and get a tunnel hostname, exactly like
`networth-api`.

## One-time setup on the box

```bash
git clone https://github.com/VicenteEspinosa/scraper-depas.git
cd scraper-depas && mkdir -p data
```

`data/` holds `depas.db` plus its `-wal` / `-shm` siblings and is the only state
worth backing up. It is gitignored and never baked into the image.

## GitHub configuration

Repository **secrets**:

| Secret | Purpose |
| --- | --- |
| `SSH_PRIVATE_KEY` | Key authorised on the Oracle box |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | The group the bot posts to |

Repository **variables**: `SSH_HOST`, `SSH_USER`, `DEPLOY_PATH`, `TZ`,
`DEPAS_PARKING_INCOME`, `DEPAS_STORAGE_INCOME`, `DEPAS_ALERT_COMMUNES`,
`DEPAS_ALERT_MAX_PRICE`, `DEPAS_ALERT_MIN_BEDROOMS`, `DEPAS_ALERT_MIN_GRADE`.

Secrets travel as one base64 blob (`ENV_B64`) rather than inline `VAR=value` ssh
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
