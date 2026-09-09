# Design

Why the code is shaped the way it is. [README.md](../README.md) says what it does
and [SELF-HOSTING.md](SELF-HOSTING.md) how to run your own; this file records the
decisions behind the source, so nobody has to rediscover them from it.

## Configuration

`depas/preferences.py`, `depas/config.py`.

Settings used to be read straight out of `os.environ` wherever they happened to be
needed, which meant the environment *was* the configuration: changing a preference was
editing a file on the box and restarting. They live in the `preferences` table now, and
`preferences.py` is the single place that knows what a setting is called, how its text
is parsed, what it means and what it falls back to when nobody has said.

Three readers share that one declaration — the `seed.env` file the table is seeded
from, the `depas config` commands, and the `/config` chat menu — so adding a knob is
adding a `Setting`. Nothing reaches for a global: a `Preferences` is a snapshot
somebody hands you, which is what lets one process hold several at once.

Settings are named `DEPAS_<PARAMETER>_<SLOT>`, so every knob for one parameter sorts
together and the slot says what it does to a listing:

| slot | meaning |
| --- | --- |
| `MIN` / `MAX` | a hard bound — outside it there is no alert at all |
| `TARGET` | an ideal — being short of it costs score and nothing else |
| `WEIGHT` | how much that component moves the final grade |
| `WANTED` | a value to match, scored on equality |
| `TIERS` | a ranked preference, best first |

Two languages, on purpose and along one line: `help` is copy, shown to whoever is
editing a setting from the chat, so it reads the way the bot's replies do. Everything
raised is an error, which surfaces in a log or a traceback, so it reads the way the
rest of the codebase does.

`DEPAS_DB_PATH` and `TELEGRAM_BOT_TOKEN` are not settings — one is read before a
database can be opened, the other is too sensitive to sit in it. `BOOTSTRAP` names them
so a check over `.env` can tell them apart from a key somebody misspelled.

Seeding happens once. After that the database is the configuration and `.env` is
history, or a preference cleared from the chat would come back on the next restart;
`depas config import-env --force` is the deliberate re-import.

`depas config check` parses every setting `.env` declares and touches nothing else. It
is worth its own pass because a value only reaches the table through a parser: a `.env`
that no longer validates stops the process at `connect`, which on a box that restarts
its containers is a crash loop rather than an error somebody reads. The deploy runs it
after building the image and before restarting anything, so the deploy fails instead of
the bot.

Every write goes through `store_preference`, chat and CLI alike, because
`net_monthly_clp` is a column of a view: the sublet income that column subtracts has to
be reachable from SQL, so it is mirrored into the `settings` table on connect and again
on every edit. Skip the mirror and a long-running bot keeps grading on the figure it
started with.

`DEPAS_ADMINS` holds numeric Telegram user ids rather than `@usernames`, because a
username can be changed and, once freed, claimed by somebody else — a whitelist keyed
on one is a whitelist that quietly changes hands. It is deliberately not "anybody in
the alert chat" either: a channel's discussion group is joinable, so being able to
reach the bot is not the same as being trusted with what it looks for. A channel post
is signed by the channel rather than by a person, so it has no author to authorise at
all.

## The settings menu

`depas/configure.py`.

The registry already knows what a setting is called, how its text is parsed and what it
means. The menu adds the one thing a keyboard needs and a parser cannot say: how you
would rather type it. A weight is six presets, a commune is a list you open one at a
time to score, a metro line is a tier, and only the handful that are genuinely open —
an address, somebody's user id — are typed at all.

The editor is chosen from the parser's own name rather than from a table of settings,
which is what keeps the promise the registry makes: adding a knob is adding a `Setting`,
and it arrives in the menu with a keyboard already. A parser with no `KIND` entry is a
mistake the tests catch, not a setting that quietly falls back to a text box. `LABELS`
and `MENU` are copy and running order, checked by a test against the registry so a new
setting cannot go unreachable.

The weights all sit in one «Pesos» group rather than each beside the parameter it
scales, because a weight only means anything against the other twelve.

Telegram caps `callback_data` at 64 bytes and silently rejects the whole keyboard past
it, so a button whose data would not fit is dropped and any row it emptied goes with it.

There is no pending-edit state anywhere. A value no keyboard can offer is asked for
with `force_reply`; that is what makes the answer carry `reply_to_message`, and the
setting being edited is read back out of the prompt's own first line. Nothing to go
stale, nothing to clean up.

`DEPAS_CURRENT_HOME` is built one press at a time, but the setting refuses a half-filled
home — correctly, since it is what `/compare` reads. An incomplete one is parked in
`settings` under `config_home_draft` and promoted to the setting once it has everything.

Authorisation is checked on every press rather than only when the menu is opened: the
menu is a message, and in a group anybody can reach the buttons on somebody else's.

## Grading

`depas/grade.py`.

A grade used to be a percentile: a listing was ranked against every other listing in
the pool, so `A 94` meant "beats 94% of what is listed right now" and moved whenever
the market did. That is a good way to shop and a bad way to decide — the best of a bad
week still graded A, and the same flat graded differently tomorrow. So the pool is gone.

Every component scores against the numbers you configured, on one curve with three
anchors: `MET` (80) where the listing hits your target, `BREACHED` (40) where it sits on
the hard bound you set, and `BEST` (100) a full span the other side of the target.
Beating a target still earns score, which is the whole point — a 70 m² flat where you
asked for 50 should read better than one at exactly 50, not tie with it.

Meeting a target is deliberately not full marks: the last fifth is only ever earned by
beating one. So the score reads as "percent of what this could be worth to me", where
80 is everything you asked for. The curve is steeper on the wrong side, too — falling
short of a target costs more than beating it pays.

A component that can only ever be *matched* scores `BEST` rather than `MET` when it
matches. There is nothing better than the conserjería you asked for, so capping it at
the target would tax each listing 20 points it has no way to earn back.

Coverage is the only thing that punishes silence. Averaging just the components that
scored would renormalise missing data away, letting a listing that answers four
questions tie with one that answers all thirteen. The perfect bonus needs both: meeting
every target on half the axes is a promise, not a proof.

Which components are live is decided by the preferences alone. The pool used to answer
that — a component nobody could score produced no values — and the preferences are the
more honest reading anyway: an unset target is not missing data, it is an opinion you
never had.

The comuna is the one component you score yourself. A list of places you would live is
not a list of places you would live *equally*, so `DEPAS_COMMUNES` carries a number per
commune — `nunoa,providencia=90,santiago=40` — and the setting answers two questions
that used to need one answer: where to look, and where you would rather live. The score
is never a cutoff. Every commune named is scraped and filtered on exactly as the plain
list always was; a commune the setting does not name is not looked at at all, and scores
zero when something that is not the alert puts one in front of you, which a pasted link
does.

It was first built as tiers, the way the metro lines are, and that was the wrong shape.
Tiers say *more than* and *less than* but never *how much*: the bottom tier was pinned
to `BREACHED` whatever it held, so "that commune is a 10" had no way to be said, and the
only knob was the weight, which moves every commune at once. Numbers say it directly.

Which makes this the one place a person writes points on the grading curve, and that is
deliberate rather than an exception that slipped in. Everywhere else you state a target
— 50 m², 25 minutes — and the curve derives the points, because those parameters have a
direction: more m² is better, more minutes is worse. A commune has no direction. It is
not more or less of anything, so there is no target to state and nothing for a curve to
measure. The anchors still mean what they mean — 100 is `BEST`, 80 `MET`, 40 `BREACHED`
— which is what makes `santiago=40` readable as a sentence: an aviso there starts at the
bottom of my range.

With every commune at 100 there is no preference to score, so the component stays off —
which is exactly what every configuration written before this parses to, since a commune
with no number is worth full marks.

The entrega is the one two-sided component, and the two sides are not the same shape.
Everything free between today and your date is a flat you could actually take, so the
whole of that window is one span and anything in it reads as met or better, the closer
the better. Past the date there is nowhere to live, so a week is a whole span on its own.

## Traits

`depas/traits.py`.

A trait is a thing a listing either is or is not, so it has no `MIN`/`MAX`/`TARGET`.
What varies is only what it does to a listing that has it, and that is the setting:
`exclude` drops the listing, `penalise` only costs it score, `ignore` does neither —
because whether amoblado is a deal-breaker or a mild dislike is a preference, not
something the code should decide. Excluding is the heavier of the two: it takes the
listing out of the pool everything else is measured against.

Every trait carries the same question twice: `keeps` as a SQL clause and `holds` as a
function of a row. The two dispositions read it in different places — excluding is a
`WHERE` over the pool, penalising is a component scored per listing — and the two must
agree on every row, or a listing excluded for a trait would not be the same listing
penalised for it.

A penalty lands in `component` and costs `penalty` points off that component's score.
Most traits have no natural home and share `traits`; one that belongs to an existing
component says so and is docked there, competing against whatever that component already
measured. `DEPAS_TOP_FLOOR` is docked inside `floor`, on top of whatever the height
already cost, so a penthouse stays worse than the identical unit one floor down.

## The listing pool

`depas/store.py`.

`listings_ranked` is a view: derived rather than state, and rebuilt on every `connect`
so it tracks the code rather than whichever migration last touched it. A listing's `id`
is its `rowid` — nothing deletes rows or `VACUUM`s, so it is stable for the life of a
listing, which is what lets a card print it and a button carry it.

Three of the view's columns are guesses the portals force:

- **Antigüedad** is published either as a number of years or as the year the building
  went up, and no portal says which it means. A flat over a century old is far rarer
  than that second habit, so a big number reads as a year; a year still to come is a
  typo, hence the floor at zero rather than a negative age.
- **UF/m²** is only published by Portal Inmobiliario. For everyone else it is derived
  from the cached UF, which matches the published figure to well under a percent.
- **Gastos comunes** are simply omitted by most publishers, and treating that as zero
  makes a listing look cheaper than any building it could actually be in. A typical
  Santiago figure is assumed instead, and every card that used the assumption says so.

The pool is enriched listings only: an unenriched one would be graded on two components
and beat everything. A `/dislike` leaves it for good — never announced again, and out of
what the others are measured against. That is not a preference, unlike a trait: there is
no reading of a `/dislike` that means "rank it lower".

That is also why a detail page that 404s is skipped rather than fatal. Listings are taken
down between the search and the fetch, and one of them used to end the whole pass before
it reached the alerts — the row stays unenriched, which already keeps it out of the pool.
Any other status is the portal, not the listing, and still fails loudly.

`FIELDS` is only what a search card carries. The detail-page columns — gastos comunes,
coordinates, specs — are owned by `save_detail`, because listing them there would blank
them on the next re-scrape, the card having nothing to put in their place.

## What the pass is allowed to spend

The search phase re-reads every card on every portal every hour, and has to: it is the
only way to hear about a new listing or a moved price. The detail page is the expensive
request — a whole page against one flat — and it is fetched exactly once, which is what
`detail_fetched_at IS NULL` means. That column is the queue.

So the queue is where the saving is, and it now refuses two things it used to buy. A
project and a listing somebody turned down are both excluded by `KEPT`, and neither can
stop being: no length of wait turns a proyecto into a unit or un-says a `/dislike`. The
page was being fetched for a row that could never be announced. `PENDING_DETAIL` is that
predicate, written once in `store.py` rather than twice in `cli.py`, and the partial
index carries the same text so SQLite can still use it.

The queue also hands over the newest first. The budget runs out most passes, and when it
does the listings left waiting should be the stale ones rather than the finds — the point
of the pass is to announce what just appeared. Insertion order did the opposite.

Reading the stored descriptions had the same shape of waste in a different place. Prose
that the current `infer_from_description` has already read yields exactly what it yielded
last time, so scanning every description every hour bought nothing; only teaching the
reader something new does. `INFERRED_VERSION` records which release read a row, and the
pass looks at the rows behind it. Bumping the constant is what backfills, once. The stamp
is written after the reading rather than before, so a pass that dies half way scans those
rows again instead of marking them read on the strength of work it never did.

The budgets themselves moved out of the command line and into the settings, because the
right number moves with how many comunas are watched and changing it should not need a
redeploy. The flags stayed as a one-off override.

`price_history` was recording `price` and `currency` but not what they were worth. A
series that runs in UF for a while and in CLP afterwards cannot be read without the UF of
each of those days, and `uf_daily` only keeps the days the bot happened to be up. The
trail now stores `price_clp` alongside. The backfill converted what it could and left the
rest NULL rather than pick a rate: a wrong number in a price history is worse than a gap.

## Knowing what is still for rent

`last_seen` was written on every scrape from the first migration and read by nothing, so
nothing ever noticed a listing going away. An apartment rented three weeks ago stayed in
the pool, kept voting in its comuna's median UF/m², and kept sitting in the pinned ⭐
list. `delisted_at` is the column that was missing.

Deciding it is the delicate part, because the evidence is an absence. A listing is
delisted once `DEPAS_DELIST_AFTER` **believable** sweeps of its portal have started since
the last one that turned it up, and a sweep is believable only if it finished without
raising *and* actually saw listings. Both halves matter: a sweep that raised proves
nothing, and neither does one that came back empty — which is exactly what a portal whose
markup moved looks like from here, indistinguishable from a comuna with nothing for rent.
So neither delists anybody. That leaves stale rows around longer than strictly necessary,
which is the direction to err in: the cost of a false positive is dropping a flat
somebody starred.

The counting compares `scrape_runs.started_at` against `listings.last_seen`, which works
because `_sweep` takes the timestamp before scraping and `save` writes `last_seen` during
it — so the sweep that saw a listing never counts against it.

Two things make it safe to be wrong. **A sweep seeing a listing clears `delisted_at`
unconditionally**, so a portal outage, or a comuna dropped from the config and later
restored, heals itself with no intervention. And a 404 on the detail page delists on its
own, which is the one signal that needs no counting: the portal is saying the page is
gone. That is also why the 404 branch changed — leaving the row unenriched used to be the
only way to keep it out of the pool, which did nothing for a row already in it.

`scrape_runs` pays for itself twice. `quiet_portals` compares a portal's latest sweep
against its best ever and warns when it has gone from plenty to zero, which is the
failure the healthcheck could never see: `_parse_card` returning None for every card
raises nothing, saves nothing, and lets the pass stamp `watch_completed_at` and read as
healthy. Sweeps are recorded per comuna rather than per portal for the same reason one
comuna's markup breaking must not make the portal's others look swept.

## Reading a detail page twice

The detail page used to be fetched exactly once, ever, so a listing whose rent moved kept
the `price_per_m2_uf` computed from the old one — while `price` itself was refreshed from
the search card every hour. The row was being ranked on two prices at once, and worst of
all on Portal Inmobiliario, the only portal that publishes the figure and the one with
the most listings.

`price_at_detail` records what the price was when the page was read, so the disagreement
is visible in SQL. `detail_due_at` is when the page is next worth reading, defaulting to
the empty string so that a row nobody has read is always due and sorts first.

The two budgets are counted separately on purpose. A listing nobody has read yet must
never wait behind a re-read, because a month where a thousand rows come due at once would
otherwise starve the finds — which are the only reason the pass exists. Within the
re-reads, a price that moved goes before one merely due.

How long a row waits is not a fixed interval but a backoff: `REFRESH_DAYS` doubling for
each consecutive reading that found nothing new, up to a month. A flat idle for two
months is worth a monthly look; one that moved yesterday is worth one in three days. The
same request budget then covers far more listings, and covers the ones that move sooner.

What "found nothing new" means is a digest of the **parsed detail**, not of the HTML. A
portal page carries CSRF tokens, view counters and render timestamps, so hashing the
markup would answer "did anything change" with yes every single time. The parsed fields
are what we care about having moved, and they survive a redesign that moves no data.

`detail_changes` keeps one row per field that actually moved, rather than a snapshot per
fetch: a listing revisited twenty times with one changed gasto común is twenty readings
and one row. A first reading is not a change — every column goes from NULL to whatever
the portal published, and logging forty of those per listing would bury the real ones.
`price_history` predates this table and migrating it buys nothing, so the `listing_changes`
view unions the two and readers do not have to know there are two.

What this makes visible for the first time: an entrega date that slips three times means
the aviso has been unrented for months, and a column that used to be filled and now is
not is what a broken parser looks like from the inside.

## Whether a conditional GET would pay

`http_cache` records the `ETag` and `Last-Modified` each url offered — validators only,
never a body, so 20 000 listings come to about 4 MB. Nothing is sent back yet.

The blocker is not the storage, it is the portal interface. `fetch_detail` both fetches
and parses, and assetplan and toctoc read two urls per listing: a 304 on one of them
would leave the parser with no body and no way to rebuild the rest. Doing it properly
means splitting fetching from parsing across all six portals, which is its own change —
and one worth justifying on evidence rather than on the hope that these portals emit
validators at all. `validator_coverage` is that evidence, gathered from production.

## Knowing the pass still runs

`watch` stamps `watch_completed_at` in `settings` as its last act, and records what
stopped it in `watch_error` on the way out. `healthcheck` warns the admins when that
stamp is more than a few hours old.

The stamp is written at the *end* on purpose. The 404 above got past every freshness
signal further up — `last_seen` on listings was minutes old, the UF cache current, both
containers up for days — while no alert had been posted for 44 hours, because the scrape
phase succeeded every time. Only completing the pass proves it completed.

What it deliberately does not watch is silence. Hours with nothing to announce are
normal, so alerting on "no cards lately" would cry wolf far more often than it caught
anything. The stale warning repeats every four hours until a pass completes: there is no
acknowledgement state, and for a bot on one box a reminder that keeps arriving is the
point. Nothing here survives the box itself dying — the watchdog runs in the same
container as the thing it watches, and catching that needs something outside it.

## Telegram

`depas/telegram.py`, `depas/bot.py`.

**A channel post's «Comentarios» button and a bot's inline keyboard share the one slot
under the message, and the keyboard wins.** Attach a keyboard to a card in a channel
with a linked discussion group and the thread can no longer be opened from the channel
at all ([bugs.telegram.org/c/41803](https://bugs.telegram.org/c/41803)). So a card
posted there carries no keyboard, and the verdict buttons are posted into the thread
instead. `hides_comments` is the one place that decides this and `_markup` the one place
that applies it — which is why the config menu, only ever answered to a person, attaches
its keyboard directly instead.

An edit that omits `reply_markup` drops the keyboard: there is no such thing as editing
only the text. That is also the cure — `depas redraw` re-renders old cards, `_markup`
withholds the keyboard wherever it would cost the comments, and a card posted before any
of this was understood gets its «Comentarios» button back.

Telegram publishes the channel-post ↔ discussion-group pairing in exactly one update,
the automatic forward, and nowhere else. The copy's own message id is the
`message_thread_id` every later comment on that card will carry, so that update is the
only chance to record it. A discussion group is not a forum, so `message_thread_id`
alone leaves a message loose in the group; what puts it under the card is replying to
Telegram's copy of the card, whose id is the thread's.

`callback_data` is capped at 64 bytes, so what travels in a verdict button is the
listing's `rowid` rather than the portal and its external id.

A typed command reaches its listing three ways, in order: the thread it was left in, the
message it replied to, and the `[id]` the card's own header prints — which is what
covers a card posted before any of this was recorded. Only the header is searched: a
bracketed number in a title or a description would otherwise rate some unrelated
listing.

Alert requirements are re-applied even where the scrape already checked them, because
enrichment overwrites card values (bedrooms among them) with the detail page's and a
listing can stop qualifying after it was stored. Floor is deliberately not among them —
it grades rather than excludes, since the portals that publish the most listings never
publish a floor number at all — and neither is the entrega date, which is scored on how
close it lands to the date you want rather than bounded.

Every candidate is stamped `notified_at` whether or not it cleared `DEPAS_GRADE_MIN`, so
a listing below the bar is never reconsidered later. The bot long-polls and reloads the
preferences once per poll, so a setting edited from the chat takes effect without a
restart, and it advances the offset even when answering failed — an update that cannot
be answered must not be redelivered on every restart forever.

## Seeing the pool

`depas/shortlist.py`, `depas/browse.py`, and `format_breakdown` in `depas/telegram.py`.

Everything the project could show you was pushed at you, and everything you could ask it
needed a shell on the box. Three surfaces close that, and all three are built out of
what was already there rather than beside it: the grade's own `parts`, the pool query
the alerts use, and the cards `card_messages` already remembers.

**The breakdown is posted, not asked for.** A `/porque` command would have been cheaper,
and nobody would type it — the moment you want to know why a grade is what it is, is the
moment you are looking at the card. So every card gets one, in the same place the
verdict keyboard goes and after it, because the verdict is what the thread is for. It is
remembered on `card_messages` beside the card, which is what lets a redraw re-render it:
an explanation that outlives the grade it explains is worse than none.

It is sorted worst last rather than by weight or by the order the components are
declared in. A ranked list is read from the top, so the row worth acting on has to be
where the eye stops, not where it starts.

**The pinned list is one message, never a second one.** A shortlist posted again on every
verdict would be a chat full of shortlists, each of them wrong the moment the next
verdict lands. So the message id lives in `settings` beside the poll offset, and every
verdict edits that message. It re-grades on every write rather than storing what it
rendered, so a weight edited from `/config` moves the pinned list too.

Telegram rejects a message past 4096 characters rather than truncating it, which would
turn a long shortlist into no shortlist at all. Entries are budgeted against the longest
footer they could need, so adding one is never what loses the message, and the overflow
is counted rather than dropped.

None of it may cost a verdict. `sync` is total — it catches its own failures and logs
them — because the star is the thing that had to be recorded and the pinned copy of it
is a convenience. Pinning is the same: the message is remembered *before* it is pinned,
so a bot without pin rights in a channel still keeps a working list.

**The browser is text, so that it can be one message.** A card is sent as a photo when
the listing has one, and Telegram will not convert a text message into a photo message
or back — so a screen that carried the photo would have no way to render a listing whose
portal published none, short of deleting the message and posting a new one, which is a
browser that walks down the chat. Editing the media in place is possible for a message
that is already a photo, but it re-uploads on every press and caps the card at a
caption's 1024 characters rather than a message's 4096. The photo is one tap away on the
card; the browser's job is scanning.

**It stores nothing between presses.** `/top` addresses a screen rather than
describing one: the button carries where to render next, so there is no session to go
stale, a keyboard left open across a restart still works, and an index into a pool that
has since shrunk clamps to the last listing instead of raising. It is the same
`pool_query` the alerts draw from, deliberately — a browser that disagreed with the
alerts about what counts as a candidate would be a second opinion nobody asked for.

It is private-chat-only and behind `DEPAS_ADMINS`, for the reason the settings menu is:
a keyboard in a group is reachable by anybody who can see the group, and a channel's
discussion group is joinable. In a group it says so rather than degrading, because the
pinned list already answers the same question there.

## Commutes

`depas/commute.py`.

Routing goes through [Transitous](https://transitous.org), which covers Santiago's whole
Red network, buses included, from the DTPM feed. It is community-run and best-effort, so
every answer is cached and an offline estimate — the faster of walking and the Metro,
blind to every bus — stands in whenever it cannot answer. The same service geocodes, so
an address can be typed instead of coordinates and no second provider has to be trusted,
rate-limited or credentialed.

An address is a way of typing coordinates rather than a second thing to store and keep
fresh: it is resolved on the way in, and the table keeps what routing actually wants.
What the geocoder matched comes back with it, because a street number that does not
exist still resolves — to the nearest one that does.

A commute is priced as a weekday-morning trip, and a fixed one keeps listings
comparable. Coordinates never move, so an answer is kept for the life of the listing and
only a change to the configured locations makes a stored one stale. Routing is a call to
somebody else's server per listing per location, which is why a pass is capped rather
than a full recompute.
