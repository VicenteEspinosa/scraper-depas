# Deploy

The image is built **natively on the arm64 deploy host** by
`docker compose build` — there is no registry. A push to `main` triggers
`.github/workflows/deploy.yml`, which pipes `scripts/deploy-remote.sh` over SSH;
that script renders `.env`, fast-forwards the checkout to the deploy commit, and
runs `docker compose up -d --build`.

Standing up your own copy from a clone — bot, chat, settings — is
[SELF-HOSTING.md](SELF-HOSTING.md); this file is how this one is wired.

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

Repository **variables**: `TZ`.

That is the whole list. Every preference lives in the `preferences` table, so the
deploy carries only what a database cannot hold — see the README's
[Configuration](../README.md#configuration). Editing one is `depas config set` on
the box and needs no deploy; only the bot token and `TZ` are re-rendered into
`.env` by `scripts/deploy-remote.sh`.

Every cost figure is the **net** monthly cost — rent plus gastos comunes minus
sublet income, where an unpublished gasto comun counts as the assumed $120.000
default rather than as zero. There is no asking-rent setting: the rent ceiling the
crawl uses is derived as `DEPAS_COST_MAX + 2 × parking income + storage income`,
because gastos only add to the net and sublet is the only thing that subtracts, so
rent above that can never come in under budget.

Requirements apply at two different points. The derived rent ceiling and
`DEPAS_BEDROOMS_MIN` bound the scrape, so listings far outside budget never enter
the database at all — but every requirement is re-checked when announcing, because
enrichment can overwrite a card value (bedrooms included) with the detail page's.
`DEPAS_COST_MAX`, `DEPAS_WALK_MAX`, `DEPAS_AREA_MIN`, `DEPAS_AVAILABLE_BY` and
`DEPAS_COMMUTE_MAX` depend on the detail page, so they gate *alerts* instead — the
listings are still stored and queryable.

The targets are not filters: at `DEPAS_COST_TARGET` or `DEPAS_WALK_TARGET` a listing
scores 80 on that component, beating it earns the rest, and past the target the score
degrades to 40 at the matching maximum. Nothing is excluded for being over target, it just
ranks lower. `DEPAS_SECURITY_WANTED` and `DEPAS_FLOOR_TARGET` are preferences all
the way — neither ever excludes. Many publishers declare no security type and most
declare no floor, so filtering on either dropped listings for missing data rather
than for being a bad fit; both cost score instead.

A listing that misses a requirement is left unstamped rather than marked notified,
so a later price drop can still bring it into range. A listing that clears the
requirements but misses `DEPAS_GRADE_MIN` *is* stamped: the grade is measured
against your preferences alone, so nothing but a re-scrape or a changed preference
could ever lift it, and neither should arrive as a surprise card.

**This repository is public, so its Actions logs are public too.** Actions masks
secrets but not variables, so anything that would identify the host — hostname,
user, path — is a secret rather than a variable: otherwise one failed `ssh`
prints it for everyone. The workflow also `::add-mask::`es `ENV_B64`, because
masking covers a secret's literal value but not its base64 encoding, and that
blob carries the bot token.

Secrets travel as that single base64 blob rather than inline `VAR=value` ssh
arguments, because the remote shell re-expands `$` in those and would corrupt a
token containing one.

## Where the cards land

`TELEGRAM_CHAT_ID` is one id and it accepts either kind of chat, so moving the
alerts is `depas config set TELEGRAM_CHAT_ID` and nothing else — no deploy, and
nothing in the code pins one.

Post to a **channel with a linked discussion group** and Telegram forwards every
card into that group as its own thread, which is what puts a Comments button on
each listing and keeps one apartment's conversation off the next one's. Replies
already come back with `message_thread_id`, and the bot hands it straight back,
so a link pasted under a card is graded inside that card's thread.

Point the same setting at the group instead and everything still works, minus
the comments: the cards pile into one flat conversation.

The id cannot be checked by eye — channels and discussion groups are both
`-100`-prefixed — so every `depas watch` pass asks Telegram and logs
`alerts: posting to a channel` (or `supergroup`) before it posts. That line is
the fastest way to confirm a switch landed.

Listings already announced carry a `notified_at` stamp and are never posted
twice, so repointing the id does not move what already went out. `depas resend
--hours N` clears the stamp on recent alerts and the next pass re-announces them
to the new destination:

```bash
docker compose exec depas-cron depas resend --hours 6
docker compose exec depas-cron depas watch
```

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
schedule. `depas watch` scrapes `DEPAS_COMMUNES`, then enriches only
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
