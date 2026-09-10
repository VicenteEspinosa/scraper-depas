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

A stored value is validated on the way in, so the only way it can fail to parse later is
the code changing under it — a commune slug leaving the enum, a disposition renamed. That
case is a warning, not an exception: `connect` skips mirroring the lease income and the
bot keeps the last `Preferences` that did load. Raising would have been the crash loop
`config check` exists to prevent for `.env`, only worse: `depas config unset`, the
repair, needs a `connect` that does not raise before it can run.

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

The prompt being read back by its first line has a cost: the first line is text, and
anybody in the group can post text. So the message being answered also has to have been
written by the bot — its user id is the part of the token before the colon, which is why
no second credential or lookup is needed to know it. Without the check, an imitation
prompt for `DEPAS_ADMINS` and an admin who replied to it would have been an admin added.

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

## A pool per subscriber

`interest` and `notified_at` were columns of `listings`, which made them everybody's: one
person's `/dislike` took the flat out of every reader's pool, and "already announced" was
a property of the listing rather than of the chat it was announced in. With one reader
those are the same thing. With two they are not, and the second reader cannot be added
until they come apart.

**They come apart along two different keys, and that is the whole design.** A verdict
belongs to a *person* — it is an opinion, and two people may disagree about the same
flat. An announcement belongs to a *destination* — a card posted in a channel has been
posted, and marking it per person would repeat it in the same chat once per reader. So
`user_interest` is keyed by Telegram user id and `subscriber_notifications` by chat.

`Subscriber` is one place cards go, and `Subscriber.view()` is `listings_ranked` as that
subscriber sees it: `interest`, `rated_by`, `rated_at` and `notified_at` come back as
columns with the names they had, so the cards, the ⭐ list and the browser go on reading
`row["interest"]` and simply get an answer that is about somebody. That is why the change
is smaller than it sounds — the shape stayed and only the source moved.

**`owner` is what makes a shared destination work.** A channel — with its linked
discussion group, which is how this repo's own instance runs — has no owner, and anybody's
verdict counts for it. That is exactly what a couple reading one channel together already
had, so it is what the existing chat becomes on upgrade, and nothing about their setup
changes. A private conversation carries its owner, and only that person's opinion shapes
what it is shown.

The enrichment queue had to be re-decided rather than translated. `COALESCE(interest, 0)
>= 0` used to mean "nobody has turned this down", and with two readers the question is
whose. It now gives up on a detail page only when *everybody* who has an opinion has
turned it down — with one shared reader that is the old column exactly, and with two it
stops one person's `/dislike` from deciding whether the other ever gets to see the flat.
It also had to leave the partial index: a partial index may not contain a subquery, so
the index covers `delisted_at` and `is_project` and the rest rides in the query.

Two smaller consequences. The pinned ⭐ list was two integers in `settings`, so there
could only ever be one of it; it is a row per chat now. And a verdict syncs *every*
subscriber's list rather than the chat it came from, because a shared subscriber counts
anybody's verdict — one person's `/like` genuinely belongs on more than one list.

A chat id reaches SQL as a literal, since a view cannot take a bound parameter, so
`_chat_sql` refuses anything that is not a number before it gets there.

**A new chat starts on what happens next, not on the backlog.** Everything already
stored is written off as told when it subscribes, because the alternative is what the
first version of this did: a private chat drawing on every listing ever kept, ten a pass,
which is days of cards nobody asked for. `--catch-up` is how to actually want that. Only
for a chat that was not subscribed before — re-adding one must not silence the listings
it was legitimately still waiting on.

**And the migration renames rather than drops.** This is the one migration in the set
that moves data a person typed — verdicts, and the announcements that stop a card being
posted twice — and a DROP is unrecoverable on a database that has already run it. So
`interest` and the rest become `legacy_*`: four dead columns bought in exchange for being
able to check the backfill against the original, or redo it.

    SELECT COUNT(*) FROM listings WHERE legacy_interest IS NOT NULL;  -- what there was
    SELECT COUNT(*) FROM user_interest;                               -- what came across

That mattered more than it looked. The announcement backfill can only place a row under
the chat it was announced in, so a database with no `TELEGRAM_CHAT_ID` configured has
nowhere to put them — with a DROP that was every card silently eligible to be posted
again. Renaming turns it into something recoverable, and the "starts on what happens
next" rule above means the re-announcement never fires in the first place.

One invariant is held by the schema rather than by a test: `subscriber_notifications
.notified_at` is NOT NULL, so a backfill that tried to write off a listing that had
never been announced inserts nothing rather than a made-up timestamp.

**What this does not do is per-reader preferences.** Every subscriber is graded and
filtered by the one set of settings, so they all receive the same cards — useful when you
and somebody else each want your own copy and your own verdicts, and not yet "two people
with different criteria". That is the next change, and it is the reason the budget is per
subscriber rather than shared: `DEPAS_ALERTS_LIMIT` exists to keep one chat from being
flooded, and two chats are not one chat.

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

## A card that stops being a one-off

Everything above makes the database notice a listing moving. What it did not do was tell
anybody: a card was posted once and left there saying whatever the rent was that day,
while `price` was refreshed hourly underneath it. The card and the truth drifted apart
with nothing in between.

The shape of the telling follows from who the reader is, which is why there are two
messages and not one. **A card already posted** is a message that is now wrong, so it is
corrected — edited in place, with the diff in its thread where whoever is looking at it
will be. But an edit notifies nobody, and a rebaja nobody hears about is a rebaja nobody
acts on, so one digest per pass names everything that moved with a link back to each
card. One message rather than one per listing: ten notifications about ten rebajas is how
a chat gets muted, and muting the chat costs the alerts too.

**A card about to be posted** is not wrong about anything; the question it raises is
different. A flat first seen three weeks ago arriving today looks like the criteria
changed, and the reader has no way to tell that it did not. The honest answer is
recoverable, because announcing is gated on requirements the *listing* can cross on its
own — a rebaja, a gasto común finally published, a walk that got computed — and those are
exactly what `listing_changes` records. When none of them moved, the note says so: the
wait was ours, and the detail queue being newest-first is the likeliest reason of the
set. Naming the real cause beats asserting a plausible one.

Both readings need a floor, and the floor is different for each. What has already been
told is `COALESCE(update_notifications.through, subscriber_notifications.notified_at)` —
the newest change already in a message, or failing that when the card went out, since a
card is not stale about anything that happened before it. That also settles the ordering
inside a pass for free: a listing announced minutes ago has a floor of now, so nothing
about it is old enough to also report as a correction. What a *first* card explains is
everything since `first_seen`, because the reader has seen none of it.

The watermark is written after the message rather than before. A pass that dies in
between repeats a notice next time, which is the failure worth having: a swallowed one
never comes back.

Three fields are excluded from counting as changes, and it is worth saying why they are
excluded rather than filtered at the source. `published_days_ago` and `published_label`
move on every re-read by the passing of time alone, and `zone_price_per_m2_uf` is the
comuna's median — the neighbourhood changing, not the apartment. They stay in
`detail_changes`, because they are true and someone reading the history wants them; they
just are not news. That is a difference between a log and a notification, and the place
to draw it is at the notification.

The same distinction, in a different shape, is what `DEPAS_PRICE_CHANGE_MIN` draws. A
flat published in UF has no CLP price of its own: the number the portal shows is today's
UF times a constant, so it is rewritten every single day without a landlord touching
anything, and a digest of «el arriendo subió de $635.567 a $635.694» four times over is
the whole feature turned into noise. Converting back to UF before comparing would fix
the pretty case and only that one — the portals also round, restate a gasto común to the
peso, and quote in pesos flats that are really priced in UF — so the floor is put on the
size of the move rather than on the currency it was written in.

It **folds** rather than drops, and that is the part that matters. A hundred pesos a day
dropped one at a time is fifteen thousand a reader never hears about, so an under-floor
move is held and the next one is measured from where the last *told* figure was — not
from the last one seen. When the arrears cross the floor they are reported as the single
move they add up to, dated at the reading that crossed it. That works without any state
of its own because the fold runs over the whole trail before `since` is applied: the
baseline is recomputed from scratch on every pass, so the watermark moving for some
other change cannot lose it. Nothing is stamped for a listing whose only movement was
held, which is exactly right — it has not been told yet.

## Whether a conditional GET would pay

`http_cache` records the `ETag` and `Last-Modified` each url offered — validators only,
never a body, so 20 000 listings come to about 4 MB. Nothing is sent back yet.

The blocker is not the storage, it is the portal interface. `fetch_detail` both fetches
and parses, and assetplan and toctoc read two urls per listing: a 304 on one of them
would leave the parser with no body and no way to rebuild the rest. Doing it properly
means splitting fetching from parsing across all six portals, which is its own change —
and one worth justifying on evidence rather than on the hope that these portals emit
validators at all. `validator_coverage` is that evidence, gathered from production.

## Spending the budget on the right rows

Three things fell out of one question: why a card for a flat first seen three weeks ago
arrives today. The queue explained it, and each of its parts wanted a different fix.

**Reading in parallel raises the ceiling; it does not spend it.** The budget still caps
what a run does, so the parallel read on its own changes nothing about throughput — what
it changes is that the cap stopped being the ten minutes between runs, which is what made
raising it from 60 to 250 affordable. Worth stating plainly, because the two look like the
same improvement and only one of them is a decision about politeness.

**Rounds are the adaptive version of a bigger budget.** Running again while the unread
queue is still full is arithmetically identical to a limit three times larger: the polite
delay lives inside `Fetcher`, so the requests per hour are the same either way. The
difference is that it only asks for that rate while there is a backlog, where a standing
limit asks for it always. That is the whole justification, and it is why the condition is
measured on the unread half alone — there are always re-reads due, so counting the batch
as a whole would read as "behind" every pass and spend every round of every hour on work
nobody was waiting for.

**Newest first is not a queue at all.** It is the right order — a flat published this
morning is what somebody is waiting for — but every arrival goes in *front* of what is
waiting, so an old row does not advance as time passes, it falls back. Draining faster
shortens the window without closing it: if the inflow ever outruns the throughput for
long enough, the oldest still never get read. A fifth of every unread batch is the floor
under that, and it has to be a floor rather than a reserved slice: reserving slots that
no old row claims shrinks the batch, and a batch that comes back short reads as having
caught up, so a backlog would stop draining while it was still there. That one was found
by simulating a flood rather than by reading the code.

The lock is what makes all three safe together. A run may now outlast its window on
purpose, and nothing stopped a second one starting — WAL and `busy_timeout` mean that is
one process dying with «database is locked» rather than corruption, but it is a pass
silently lost. `taken_at` is what keeps the cure from being worse than the disease: a lock
nobody released would wedge the stage forever, so one older than the staleness limit is
not a lock, and the failure mode is one skipped window instead of a stalled stage.

## The rate limit is per chat

Telegram's limits are about a message a second to any one chat and twenty a minute to a
group or a channel, which is where the three-second wait after every card came from. It
was a global `time.sleep`, and a global sleep cannot express a per-chat limit: the card
goes to the channel and its breakdown to the linked discussion group, which are two chats
with two budgets, so a card cost six seconds of waiting for one message in the channel.

Pacing per chat is the whole fix, and which limit applies is read off the id rather than
asked — Telegram numbers a private conversation with its user's own positive id and
anything with more than one reader negatively. `getChat` would be a request to learn what
the id already says, and it would sit behind every plain card, which is a cost the
verdict keyboard deliberately avoids paying.

The other half is the 429. It used to surface as a `RuntimeError` that `_announce` caught
per subscriber, so a burst tripping the flood control cost that chat the rest of its
pass. Telegram says how long to wait in `parameters.retry_after`; honouring it — plus a
second, because the limit is a window and landing on its edge trips it again — is what
makes it safe to aim at the limit rather than hide well under it.

## Six portals at once

The sweep was a `for` over six portals, each with a polite delay between requests, so a
pass took the *sum* of six portals however idle the machine was. They are six different
hosts: one worker each asks no host for more requests per second than the sequential
version did — `Fetcher` keeps its own delay inside each thread — and the pass stops
taking as long as the slowest arrangement of them.

**The workers fetch and parse; the caller writes.** Nothing in a worker touches the
database, so SQLite keeps the single writer it is happiest with and none of this code has
to think about transactions or `SQLITE_BUSY`. A sweep comes back as a `Swept` — plain
objects — and the main thread saves it and records the evidence.

The UF is read from the indicator once a day and cached in `uf_daily`. The indicator
being down used to fail the whole sweep, because a listing quoted in UF cannot be stored
without one — but the UF moves by a fraction of a percent a day, so the last cached value
is a fine price for today. `stored_uf` falls back to it, and deliberately does not write
it down under today's date, so tomorrow's pass asks again. With nothing cached at all the
outage is still an error: a wrong UF would misprice every UF listing, and there is no
right one to be had.

Each worker gets its own `Fetcher`, because a `curl_cffi` session is not built to be
shared. That is also why `normalize` now takes the UF value rather than the fetcher:
`uf_in_clp` caches per Fetcher, so six fetchers meant six requests to the indicator, and
normalising was doing network inside what reads like arithmetic. Handing the number down
also fixed `scrape`, which was passing a `Fetcher` where a float was wanted and would
have raised on the first UF-priced listing — nothing tested that command.

**One portal failing no longer aborts the pass.** It used to re-raise, which cost every
other portal's alerts for that hour to spite one flaky host; now the error goes into
`scrape_runs`, prints a warning, and the other five carry on. Every sweep failing is a
different thing and still raises: nothing was scraped at all.

## Four stages instead of one pass

`watch` did everything in one strict sequence, so the slowest phase set the pace for
every phase after it. A scrape that grew to forty minutes delayed an alert for a listing
that had been enriched an hour earlier, and the enrichment got one go per hour because
that was how often the scrape finished — not because sixty pages was the right amount.

The stages were already separated by columns: `detail_due_at <= now` *is* a queue, and so
is `notified_at IS NULL`. Making them separate commands only stops them queueing behind
each other. On the split crontab the enrichment runs six times an hour in small batches —
the same requests, spread out, clearing a backlog six times faster — and `announce` runs
four times, so a listing enriched at :12 goes out at :15 rather than waiting for a whole
pass. `depas watch` still runs all four in order for anyone who would rather have one
entry, and keeps stamping the original two keys so an upgrading box does not read as
having never completed a pass.

Each stage now stamps its own heartbeat, which is the point. The 404 that started all of
this got past every freshness signal there was; what it would still have got past is a
single stamp saying "the pass ran", because the phase that was broken was not the phase
being watched. A stalled enrichment is no longer hidden behind a scrape that keeps
succeeding. Each stage has its own patience too — discovery feeds everything downstream
and gets four hours, routing is somebody else's server and gets a day.

What the watchdog checks is those four, unstamped included: a deploy whose pass has
never finished is the case it was built for, and a stage that has never once completed is
the loudest form of that.

`watch` is not among them, and the first version of this had that backwards. It checked
`watch` always and skipped a stage that had never been stamped — which read as careful
and was the opposite twice over. A box whose crontab drives the four stages separately
runs no `watch` at all, so its stamp froze at the last single-entry pass and the admins
were warned about "la pasada horaria" every four hours while all four stages were running
on time; meanwhile the four stages, on a box where one had never completed, were the ones
being skipped. Both halves came from treating `watch` as the real signal. It is not: it
runs the same four stage functions, each of which stamps its own heartbeat on the way
through, so the four stamps are written under either crontab and `watch`'s own stamp says
nothing they have not. It is still written, as the record of how a single-entry pass
ended, and no longer alerted on.

The per-stage patience was declared and then not used: `--stale-hours` had a default of
four, so every stage was held to four hours and `route`'s day never applied. The flag
now defaults to nothing, which is what lets `STALE_HOURS` speak, and passing a number
still means what it did — one patience for everything. The test reads the parser rather
than calling the function, because the bug was in a default nobody passed.

## Cutting the pagination short without trusting the portal

Portal Inmobiliario and Chilepropiedades are the only two that paginate at all — houm's
API already stops on its own `next`, and toctoc and assetplan answer with everything at
once. Both *appear* to return the newest listings first, and neither documents it.

That gap matters more than it sounds, because the failure is silent. If the order is by
relevance instead, a listing published an hour ago can sit on page three behind ones we
already have; stopping at the first quiet page never sees it, raises nothing, and leaves
nothing in the log. A flat lost with no trace is worse than the requests it saves.

So the cutoff is built to be correct whether or not the assumption holds, along three
lines.

**It takes consecutive quiet pages, not one.** `DEPAS_SWEEP_QUIET_PAGES` defaults to two,
so a single stale page in the middle of a run of finds does not end a sweep. That is
cheap insurance against an ordering that is *mostly* by date rather than strictly.

**A deep sweep is the floor under it.** Every `DEPAS_DEEP_SWEEP_HOURS` a portal is read
to the bottom regardless, so the worst the cutoff can cost is latency — a listing hiding
behind known ones is found within a day rather than never. Both settings at 0 give back
exactly the pre-cutoff behaviour, which is the escape hatch if any of this goes wrong.

**And it measures the assumption instead of trusting it.** Each listing carries the page
it came from in `Listing.extra`, which is not among `FIELDS` and so never reaches the
database. A sweep records `deepest_new_page`: of the listings it had never seen, the
deepest page one turned up on. On a deep sweep that is exactly the question the cutoff
depends on — while it stays below `DEPAS_SWEEP_QUIET_PAGES`, stopping early could not
have dropped anything. `cutoff_safety` reports the portals where it does not, and
`discover` prints that every pass. Only deep sweeps count as evidence: a shallow one
never looked past the cutoff, so it can say nothing about what is behind it.

The known ids reach the portal as a plain `frozenset` on the `Query` rather than as a
callback. A portal has no database and should not grow one to tell a page of finds from a
page it has seen before, and `_discover` reads them once per portal on the main thread —
the workers still touch nothing.

`Query.nothing_new` lives on the dataclass rather than in `depas.portals` because that
package imports every portal module, so a portal importing back out of it is a cycle.

What this does *not* save much of any more: the sweep going parallel already cut the
pass's wall clock by about six, so what is left is requests — roughly half of the listing
sweep. Worth having, not worth being wrong about, which is why the safety net is bigger
than the optimisation.

## A copy before the code changes

`depas backup`, `scripts/deploy-remote.sh`.

Every migration so far has been additive or has renamed rather than dropped, and the one
that moved data — the pool per subscriber — kept the originals as `legacy_*` columns so
the backfill could be checked against them. That is the discipline; a backup is what
stands behind it when the discipline slips. The deploy takes one after the image is
built and before the containers restart, which is the last moment the file still has the
schema the old code left.

Through SQLite's own backup API rather than `cp`: the database is in WAL mode and the old
containers are still writing, so a plain copy could catch the main file and the `-wal`
mid-checkpoint. And deliberately not through `connect()`, which would apply the very
migrations the copy is meant to predate — the command opens the file raw and touches
nothing but the destination.

## Knowing the pass still runs

Each stage stamps `watch_completed_at:<stage>` in `settings` as its last act, and
records what stopped it in `watch_error:<stage>` on the way out. `healthcheck` warns the
admins when any of those stamps is more than that stage's patience old. `watch` keeps the
unprefixed pair for the pass as a whole, which is a record rather than an alert.

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

A pasted link is recognised by a per-portal pattern, and the pattern is strict about the
host: the portal's domain, or a subdomain of it, and nothing that merely *ends* in it.
The bot fetches what it recognises and posts the result as a card with a «Ver aviso»
link, so a pattern that let `miportalinmobiliario.com` through would have let anybody in
the group have the bot vouch for a page of their own.

Telegram's own failures come back as JSON with `ok: false`, which `call` turns into a
`RuntimeError` the polling loop survives. An outage does not: it answers with an HTML
error page, and the `JSONDecodeError` that followed was not on the list of things the
loop caught, so a bad hour at Telegram was a restart of the bot on every poll. `call`
now reports that as the same `RuntimeError`.

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
