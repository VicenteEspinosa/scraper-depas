# Several people, one bot

A plan for letting more than one person read this bot, each with their own criteria,
without any of them stepping on another — and without losing a single thing the bot does
for one person today. It opens with what the code already is, because most of the plan
is already in it.

**Status.** Phase 0 is done and is the PR this document arrived in. Phases 1 to 3 are
designed here and not started: each is its own PR, and each starts with the decisions in
[§6](#6-decisions-that-need-a-yes) being answered. Nothing below moves or drops data;
every phase adds tables and copies rows, and the deploy now takes a copy of the database
before the new code can migrate it.

## 1. Where the code stands

### 1.1 What is already built for more than one reader

The pool-per-subscriber change (`migrations/016_a_pool_per_subscriber.sql`) did the hard half. These are
already keyed the way a multi-user bot needs them:

| Thing | Keyed by | Table |
| --- | --- | --- |
| A verdict (⭐ / 🚫) | the **person** | `user_interest (user_id, portal, external_id)` |
| "Already announced here" | the **chat** | `subscriber_notifications (chat_id, portal, external_id)` |
| The pinned ⭐ list | the chat | `subscriber_shortlist (chat_id)` |
| Every card posted | the chat | `card_messages (chat_id, message_id)` |
| Where cards go, and whose verdicts count there | the chat, with an optional owner | `subscribers (chat_id, owner_user_id)` |

`Subscriber.view()` is the seam: everything that renders a listing — cards, the ⭐ list,
`/top`, the breakdown — reads `row["interest"]` and `row["notified_at"]` off it and
never asks whose they are. That is why the rest of this plan is smaller than it sounds.

### 1.2 What is still single-user

One set of `preferences`, and everything that reads it:

- **Grading and filtering.** `Scale(prefs)`, `pool_query(prefs, subscriber)` and the
  alert requirements all take a `Preferences`, which is good — but every caller hands
  them the same one, `Preferences.load(connection)`.
- **The crawl.** `DEPAS_COMMUNES`, the derived rent ceiling and `DEPAS_BEDROOMS_MIN`
  decide what is fetched at all.
- **Commutes.** `listings.commute` is one JSON object per listing keyed by the *name* in
  `DEPAS_LOCATIONS` — `{"pega": 32}`. Two people with different places to reach cannot
  share the column.
- **Net cost.** `listings_ranked.net_monthly_clp` subtracts parking and storage income
  read from a global mirror in `settings`. Two people who would sublet differently see
  the same net figure.
- **Your own flat.** `DEPAS_CURRENT_HOME` and `DEPAS_CURRENT_COST` are one flat.
- **Who may do what.** `DEPAS_ADMINS` gates both `/config` and `/top`. It conflates two
  different trusts: running the box, and having criteria of your own.
- **Onboarding.** A subscriber is added with `depas subscribers add` on the box. There is
  no way for a person to arrive.
- **Budgets and pace.** `DEPAS_*_LIMIT`, `DEPAS_DELIST_AFTER`, the sweep settings —
  rightly global, but they sit in the same table as somebody's ideal floor.
- **A pasted link** is graded with the one set of preferences whoever pasted it.

### 1.3 What the audit found

Read end to end: every module, every migration, the deploy path, the workflows and the
tests. The suite was green and ruff clean before and after; it is 556 tests now.

**Fixed in Phase 0** (this PR), each with a test that fails on the old code:

| | What | Why it mattered |
| --- | --- | --- |
| security | The pasted-link pattern for Portal Inmobiliario accepted any host *ending* in the portal's name: `miportalinmobiliario.com`, `evilmercadolibre.cl`. | Anybody in the discussion group could have the bot fetch a page of theirs and post it as a card with a «Ver aviso» link — the bot's own endorsement of a phishing page. The host must now be the portal or a subdomain of it. |
| security | A typed `/config` value was accepted as a reply to any message whose first line *looked* like one of the bot's prompts. | An imitation `⚙️ DEPAS_ADMINS · agregar` in the group plus one admin replying to it was an admin added. The replied-to message now has to be the bot's own, checked by the bot's id (the part of the token before the colon). |
| security | `escape()` left `"` alone, and the card puts a url inside `href="…"`. | A url carrying a quote could end the attribute. Telegram would have rejected the message rather than run anything, but the card would have been lost. |
| robustness | A Telegram outage answers HTML, not JSON, and the `JSONDecodeError` was not on the list the polling loop survives. | Every bad hour at Telegram was a bot restart per poll. It is now the same `RuntimeError` a `ok: false` answer is. |
| robustness | A stored preference that stops parsing (a commune leaving the enum, say) raised inside `connect()`. | A crash loop with the repair command, `depas config unset`, locked out of the database it needed to open. It is a warning now; the bot keeps the last reading that parsed. |
| robustness | `stored_uf` failed the whole sweep when the UF indicator was down. | The UF moves by a fraction of a percent a day; yesterday's is a fine price. It falls back to the last cached value and does not write it down as today's. |
| correctness | `healthcheck --stale-hours` defaulted to 4, overriding the per-stage patience in `STALE_HOURS` for every stage. | `route` was documented to get a day and got four hours. The flag defaults to nothing now. |
| data safety | There was no backup anywhere, and every deploy migrates the database on first open. | `depas backup` copies through SQLite's backup API without opening the file the normal way, and the deploy runs it between validating `.env` and restarting. Last five kept in `data/backups/`. |
| hygiene | `show` leaked its connection on the raw-SQL path; `.claude/` was not in `.dockerignore`. | Small, and free. |

**Recommended, not done here** — each is a decision or a change with a blast radius, so
it is written down rather than pushed:

- `.github/workflows/*.yml` use `actions/checkout@v4` and `astral-sh/setup-uv@v5` by
  tag. Pinning to commit SHAs closes a supply-chain hole; it costs a Dependabot rule to
  keep them fresh.
- The deploy learns the box's SSH host key with `ssh-keyscan` on every run, which is
  trust-on-first-use every time. A `SSH_KNOWN_HOSTS` secret holding the key pins it.
- `seed.env` carries the author's Telegram id in `DEPAS_ADMINS`, which is documented and
  deliberate: a clone with no admin can only be configured over SSH. Once Phase 3 gives a
  bot a way to bootstrap its first admin from the chat, the seed should carry nobody.
- `depas chats` calls `getUpdates` and so competes with a running bot for the same poll —
  Telegram answers one of them with `Conflict`. It only matters during setup, but the
  command should say so when it happens.
- `Subscriber.view()` runs four correlated subqueries per listing per column. Fine at
  thousands of rows and one subscriber; worth a `LEFT JOIN` on a per-person latest-verdict
  view before there are dozens of subscribers.
- Anyone who can see a chat can press a verdict button, which the README says and which
  suits a private channel. With registration (Phase 3) it becomes worth refusing presses
  in a shared chat from people who are neither its owner nor registered.

**Looked at and left alone, on purpose:**

- Every SQL interpolation was checked. The two literals — a chat id in
  `Subscriber.view()` and a user id in `Subscriber.mine` — are guarded by `_chat_sql` and
  `int()`; every other value is bound. Column names come from `DETAIL_COLUMNS`/`FIELDS`.
- `show "SELECT …"` runs raw SQL, from the shell on the box, by somebody who already has
  the database file. Not a surface.
- The deploy script's prune rules are already tested against the daemon-wide footguns.
- `.dockerignore` excludes `.env`, every `*.db*` and `data/`; the image carries no secret.

## 2. The model

Five words, used exactly:

- A **person** is a Telegram user id.
- A **chat** is a place messages go: a private conversation, a channel (with its
  discussion group), a plain group.
- A **subscriber** is a chat cards are posted to, with an **owner** (a person) or
  **shared** (nobody's, so anybody's verdict counts). This exists today.
- A **profile** is one set of the preferences a person would recognise as theirs:
  targets, bounds, weights, communes, places, home, sublet income, grade minimum,
  how many cards a pass may send them.
- **Operations** are the settings that belong to the box and to whoever runs it: admins,
  budgets, sweep and delisting knobs, registration policy.

**The subscriber is the tenant.** Every subscriber has exactly one profile. A verdict
stays with the person, an announcement with the chat, a preference with the subscriber.

Why the subscriber and not the person: a couple reading one channel wants *one* set of
criteria for that channel, and already gets shared verdicts there. A person who also has
a private chat with the bot has two views — the channel graded by the channel's criteria,
their private chat by their own — and that is the right answer, not a conflict. It also
keeps the one deployment in production exactly as it is: one shared subscriber, one
profile, which is today's `preferences` table.

Why not a profile *per person* with chats pointing at one: it is the same data model with
one more indirection and one more thing to explain ("whose profile does this channel
use?"). It can be added later as a `subscribers.profile_of` column if two chats ever want
to share criteria; nothing below forecloses it.

### 2.1 Settings get a scope

`Setting` in `depas/preferences.py` grows one field, `scope`, and the registry is split:

| Scope | Settings |
| --- | --- |
| **profile** | `DEPAS_COMMUNES`, `DEPAS_BEDROOMS_MIN`, `DEPAS_GRADE_MIN`, every `_MIN`/`_MAX`/`_TARGET`/`_WEIGHT`/`_WANTED`/`_TIERS`, `DEPAS_LOCATIONS`, `DEPAS_FURNISHED`, `DEPAS_TOP_FLOOR`, `DEPAS_PARKING_INCOME`, `DEPAS_STORAGE_INCOME`, `DEPAS_CURRENT_COST`, `DEPAS_CURRENT_HOME`, `DEPAS_ALERTS_LIMIT` |
| **operations** | `DEPAS_ADMINS`, `TELEGRAM_CHAT_ID`, `DEPAS_ENRICH_LIMIT`, `DEPAS_REFRESH_LIMIT`, `DEPAS_COMMUTE_LIMIT`, `DEPAS_SWEEP_QUIET_PAGES`, `DEPAS_DEEP_SWEEP_HOURS`, `DEPAS_DELIST_AFTER`, and new: `DEPAS_REGISTRATION` |

The house rule stays: a new setting is a `Setting` and nothing else. The `/config` menu,
the CLI and the seed keep reading the one declaration; the scope only decides which table
a value lands in and who may write it. The existing test that every setting has a label
and a keyboard gains a sibling: every setting has a scope.

### 2.2 Storage, additive only

```sql
-- 018: a profile per subscriber. `preferences` is untouched: it becomes the operations
-- scope plus the defaults every new profile starts from, which for the one subscriber
-- in production is a distinction without a difference.
CREATE TABLE profile_preferences (
    chat_id    TEXT NOT NULL REFERENCES subscribers (chat_id),
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, name)
);

-- Every existing subscriber gets today's profile-scope rows, copied, so nothing about
-- what it is sent changes. The rows stay in `preferences` too.
INSERT INTO profile_preferences (chat_id, name, value, updated_at)
SELECT s.chat_id, p.name, p.value, p.updated_at
  FROM subscribers AS s, preferences AS p
 WHERE p.name IN (<the profile-scope names, generated from the registry>);
```

Resolution, for a subscriber `S`: registry default ← `preferences` ← `profile_preferences
WHERE chat_id = S`. `Preferences.load(connection)` keeps its meaning (the global layer,
which is what the CLI and the crawl read) and gains `Preferences.load(connection,
subscriber)` for the rest. Nothing is dropped, nothing is renamed; a database that has run
018 can have the table emptied and be exactly what it was.

The other two columns that are secretly one person's:

```sql
-- 019: a commute is a fact about a listing and a destination, not about a name.
CREATE TABLE listing_commutes (
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    lat         REAL NOT NULL,   -- destination, rounded to 4 decimals (~11 m)
    lon         REAL NOT NULL,
    minutes     INTEGER NOT NULL,
    routed_at   TEXT NOT NULL,
    PRIMARY KEY (portal, external_id, lat, lon)
);
-- Backfilled from listings.commute by joining each key to the coordinates DEPAS_LOCATIONS
-- gives that name today. listings.commute stays, unread, as the legacy_* columns did.

-- The places a profile has to reach, mirrored from its DEPAS_LOCATIONS on every write so
-- the alert filter can join on them -- the same pattern `sync_lease_income` already uses.
CREATE TABLE profile_locations (
    chat_id TEXT NOT NULL, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    PRIMARY KEY (chat_id, name)
);
```

Two people whose offices are 200 m apart still pay two routings; two people with the same
office pay one. `route` routes the union of every active profile's destinations, still
under `DEPAS_COMMUTE_LIMIT`.

**Net cost** moves from the view into `Subscriber.view()`, computed with the profile's
two income figures — integers, interpolated after `int()`, the way `Subscriber.mine`
already does. `listings_ranked.net_monthly_clp` stays for the CLI. One real wrinkle:
`SELECT listings_ranked.*, … AS net_monthly_clp` produces a duplicate column and
`sqlite3.Row` will not pick one, so the ranked view has to stop carrying the net figure
(the CLI's `show` computes it from the global profile the same way) or the subscriber view
has to name its columns. The first is smaller.

**`commute` as readers see it** is rebuilt per profile in `Subscriber.view()` with
`json_group_object(name, minutes)` over `listing_commutes` joined to `profile_locations`,
so the cards, the grade and `/compare` go on reading `row["commute"]` untouched. That is
the same trick that made the pool-per-subscriber change small.

`settings.config_home_draft`, the half-built home the menu parks, becomes
`config_home_draft:<chat_id>`.

### 2.3 One crawl for everybody

The sweep fetches the **union**: every commune any active profile names, the rent ceiling
of the profile with the highest `max_rent()` (none if any profile sets no ceiling), the
lowest `DEPAS_BEDROOMS_MIN`. Everything narrower is already re-applied per subscriber at
announce time, because enrichment can disqualify a listing anyway. `scrape_runs`,
delisting and the cutoff safety are untouched: they are about portals, not people.

What this costs is requests, and requests are the operations scope's to ration:
`DEPAS_ENRICH_LIMIT` bounds the detail pages a pass reads whatever the union grows to, so
adding a user with three new communes slows *everybody's* alerts slightly rather than
tripping any portal's rate limit. That is the right default. The healthcheck already
notices a stalled enrichment.

The enrichment queue's "nobody wants it" is already per person and needs nothing.

### 2.4 The bot

Every message and every press resolves to a subscriber before anything else happens:

1. The chat is a subscriber → that one.
2. The chat is the discussion group of a channel that is → the channel's (the pairing is
   in `card_messages.thread_chat_id`, and can be asked of `getChat` once per process the
   way `hides_comments` already does).
3. Neither → the **default profile**, which is the global layer: exactly today's
   behaviour for a link pasted in a chat nobody subscribed.

Then, with that subscriber's `Preferences` in hand, nothing downstream changes. The
handlers already take a `prefs` argument; today it is the same object every time.

| Surface | Today | After |
| --- | --- | --- |
| `/config` | edits the one profile; `DEPAS_ADMINS` only | edits the resolved subscriber's profile. Allowed: the owner of a private subscriber; `DEPAS_ADMINS` for a shared one. The «⏱️ Ritmo» and «🤖 Bot» groups are operations and appear only for admins. |
| `/top` | private chat, admins only | private chat, its owner — or an admin. Same code; the check moves from "is admin" to "owns this subscriber or is admin". |
| A pasted link | graded with the one profile | graded with the chat's profile |
| `/compare` | against the one home | against the chat's profile's home |
| Verdict buttons and `/like` | anyone in the chat, per person | unchanged |
| Cards, ⭐ list, breakdown | per chat | unchanged |
| `healthcheck` | DMs the admins | unchanged |

### 2.5 Arriving

`/start` in a private chat, from somebody who is not yet a subscriber, does one of three
things according to `DEPAS_REGISTRATION`:

- `closed` (today, and the default after the migration): answers with their id and the
  `depas subscribers add` line, as it does now.
- `approval` (**recommended** once the bot has a second user): records a *pending*
  subscriber and DMs every admin one message with two buttons, ✅ and 🚫, carrying the
  user id (fits in 64 bytes with room to spare). Approving copies the global profile
  defaults into a fresh profile and answers the person with their `/config` menu. They
  start on what turns up from then on, the way a new chat already does; `--catch-up` is
  still the CLI's to grant.
- `open`: the same, without the wait. For a bot behind a private link only.

Storage is two nullable columns on `subscribers` — `requested_at`, `approved_by` — and a
third state for `enabled` is not needed: pending is "in the table, not enabled, with a
`requested_at`". `depas subscribers` lists pending ones; `depas subscribers approve
<chat>` is the shell's way.

Leaving is `depas subscribers remove`, which already keeps everything the chat was told so
that coming back is quiet. A person's verdicts are theirs and are never deleted.

### 2.6 Who may do what, written down once

| Action | Who |
| --- | --- |
| Press a verdict, type `/like` `/dislike` `/compare` | anyone who can see the chat (unchanged) |
| `/config` on a private subscriber | its owner |
| `/config` on a shared subscriber | `DEPAS_ADMINS` (later, optionally, a per-profile editor list) |
| The operations groups of `/config`, any `depas config set` of an operations setting from the chat | `DEPAS_ADMINS` |
| `/top` | the owner of that private chat; an admin anywhere private |
| Approve or refuse a registration | `DEPAS_ADMINS`, or the shell |
| `depas subscribers`, `depas config`, `depas backup`, everything else on the box | whoever has the shell, as today |

Isolation, stated as promises the tests will hold:

- A person can neither read nor change another subscriber's profile: rows are keyed by
  chat, and the editor is checked on every press, as it already is.
- A person's verdicts never enter another private subscriber's pool — `owner` already
  does this — and a shared chat goes on counting everybody's.
- A person's preferences never change what anybody else is sent. The only shared effect
  of a profile is on the crawl's union, which is requests, not results.
- The one production deployment is byte-for-byte unchanged after every phase: same
  cards, same grades, same ⭐ list. A test posts a pass against a fixture database at the
  previous version and diffs the output.

### 2.7 Conflicts that are not conflicts

- **The same flat, two people.** Two cards, in two chats, with two grades. That is the
  feature.
- **One person, two chats.** Their private chat and the shared channel show the flat
  each by its own criteria, and their ⭐ lands on both — a verdict is theirs, not the
  chat's, and that is what a couple wants from a shared channel.
- **Telegram's rate limits.** About one message a second per chat and thirty a second
  overall. The announce loop already sleeps three seconds per card per chat, and runs
  subscribers one after another, so a pass with `N` subscribers and `L` cards each takes
  up to `3·N·L` seconds — twenty subscribers at ten cards is ten minutes, inside the
  fifteen the crontab gives it. Past that the sleeps become per-chat rather than global,
  which is a small change.
- **SQLite.** Two processes, one writer at a time, WAL, a five-second busy timeout. Fine
  for tens of subscribers and hundreds of verdicts an hour. Postgres is not part of this
  plan; the day it is, `Subscriber.view()` is the one place the SQL dialect lives.

## 3. Phases

Each phase is one PR into `main`, from a `feat/` branch, with its migration tested from a
fixture database at the previous version — the pattern `tests/test_subscribers.py` set
with `_at_015`. The deploy backs the database up before each.

**Phase 0 — the ground is safe.** *Done, this PR.* The audit fixes in §1.3, `depas
backup` in the deploy, this document.

**Phase 1 — a profile per subscriber.** `Setting.scope`; migration 018;
`Preferences.load(connection, subscriber)`; every handler resolves its subscriber first;
`/config` edits the resolved profile with the authorisation in §2.6; `depas config
--chat <id>` for the shell. Behaviour for one subscriber identical, held by the diff test
in §2.6. About the size of the pool-per-subscriber PR.

**Phase 2 — the two columns that were one person's.** Migration 019; commutes routed
per destination and rebuilt per profile; net cost per profile; the crawl fetches the
union; `DEPAS_ALERTS_LIMIT` per profile. This is the phase with the most SQL and the one
to review most slowly.

**Phase 3 — arriving.** `DEPAS_REGISTRATION`; `/start` for a stranger; the approval
buttons; `depas subscribers approve`; `/top` for owners; the operations groups hidden
from non-admins; `seed.env` stops carrying an admin once a bot can bootstrap one from
the chat.

**Phase 4 — living with it.** Only if the previous three earn it: a per-profile editor
list for shared chats; the `LEFT JOIN` rewrite of `Subscriber.view()`; refusing presses in
shared chats from people who are not registered; SHA-pinned Actions and a pinned SSH host
key.

## 4. What every phase promises about the data

1. **No `DROP`, no `DELETE`, no primary-key rebuild** of a table that holds anything a
   person typed or the bot was told. New tables, new columns, copied rows. Where a column
   has to stop being read, it is renamed `legacy_*`, as 016 did — never removed.
2. **Every migration has a test that starts from a database at the previous version**
   with real-shaped rows in it, runs the migration, and asserts what came across against
   what was there. 016's tests are the template.
3. **Every migration leaves verification queries in its own comments**, the way 016 does,
   so what it did can be checked on the box afterwards.
4. **The deploy copies the file first.** `depas backup` runs between `config check` and
   the restart, keeps five, and a failed copy stops the deploy with the old containers
   serving. Restoring is a file copy, documented in `docs/DEPLOY.md`.
5. **The single-subscriber deployment is unchanged after every phase**, and a test says
   so by diffing a pass.
6. **Nothing is behind a flag.** With one subscriber the new code and the old code are
   the same code path, which is worth more than a switch nobody flips back.

## 5. What stays exactly as it is

Every filter and every component of the grade, the three shapes of chat (channel with a
discussion group, plain group, private conversation), verdicts by button and by command,
the ⭐ list, `/top`, `/compare`, the breakdown, the settings menu, every CLI command, the
six portals, Transitous and the UF indicator, the four-stage pass and its heartbeats, the
deploy. They gain a "whose" and lose nothing.

## 6. Decisions that need a yes

None of Phases 1 to 3 starts before these are answered. The recommendation is first in
each case.

1. **The tenant is the subscriber (chat), not the person.** Alternative: a profile per
   person that chats point at. §2 argues the first; the second can be layered on later.
2. **`preferences` stays and becomes "operations + the defaults a new profile starts
   from"**; profiles are a second table that overrides it. Alternative: move the
   profile-scope rows out of `preferences` into the new table. Copying keeps the
   rollback trivial; moving keeps the table's meaning purer. Recommendation: copy.
3. **Registration defaults to `closed` after the migration and the owner switches it to
   `approval`** when they want a second reader. Alternative: `approval` from the start.
4. **A shared chat's profile is edited by `DEPAS_ADMINS`**, exactly as today. A
   per-profile editor list is Phase 4 if wanted.
5. **`DEPAS_ALERTS_LIMIT` becomes a profile setting** — how many cards *I* am willing to
   get a pass — with no operations ceiling for now. The alternative is a ceiling too.
6. **Commutes are cached by destination coordinates rounded to four decimals**, shared
   across profiles. The alternative, per profile, routes the same office once per person.
7. **The crawl fetches the union and the operations budgets stay the only brake.** The
   alternative is a per-profile cap on communes; not recommended until it is needed.
8. **SQLite stays.** Nothing here needs more, and the deploy, the backup and the tests
   are built around one file.

## 7. Out of scope, and why

- **Another messenger or a web page.** The Telegram layer is `telegram.py`, thin, and a
  chat id is opaque text everywhere else, so a second transport is a new module rather
  than a redesign. Not in this plan because nobody has asked for one.
- **Postgres, a queue, a second box.** See §2.7.
- **Per-person preferences inside a shared chat** ("grade the channel's cards by *my*
  criteria for me"). A card is one message; it cannot show two grades. The private chat
  is the answer to that wish, and the model gives everybody one.
