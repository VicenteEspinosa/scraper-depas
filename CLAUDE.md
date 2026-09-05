# Working on this repo

Notes for Claude Code sessions. [README.md](README.md) says what the project does,
[docs/DESIGN.md](docs/DESIGN.md) why the code is shaped the way it is; this file is
about how to land a change in it.

## Branches

**A session starts on a `claude/<something>-<suffix>` branch you did not choose.** The
harness names it at session start, before anyone knows what the work will turn out to
be, so it is a scratch name rather than a decision — `claude/apartment-visibility-ideas-bg9ctd`
for what became three Telegram surfaces.

Work on it. But **the moment the change has a shape, before opening a pull request,
branch to a properly named one and open the PR from there** — the branch name outlives
the session in the merge commit, and `Merge pull request #36 from
VicenteEspinosa/claude/thread-replies-broken-wm55g5` tells whoever reads the log in a
year nothing at all. Three merges on `main` already read like that.

```bash
git checkout -b feat/see-the-pool-from-the-chat   # same commits, a name that says what they are
git push -u origin feat/see-the-pool-from-the-chat
# open the PR from this branch, then delete the scratch one:
git push origin --delete claude/<the-one-you-started-on>
```

Check the two are identical before deleting anything —
`git diff claude/<scratch> <new-branch> --stat` should print nothing.

**Naming.** `feat/` for a new capability, `fix/` for a bug, kebab-case, and a *phrase in
words* that says what changed rather than which files did. The repo's own precedent:

```
feat/track-when-the-flat-frees-up
feat/no-amoblado-and-building-age
feat/settings-in-the-database
fix/deploy-eats-its-own-script
```

**The base is `main`.** There is no `master` and no `development`, so a dual-PR workflow
does not apply here — one PR, into `main`.

**Never push to `main` directly.** `.github/workflows/deploy.yml` fires on every push to
it and deploys to the box over SSH. Changes land through a PR.

## Commits

A gitmoji, then one line that says the intent — not the files, and not the diff restated.
What the log actually uses, most to least: ✨ feature, ✅ tests, 🐛 fix, 📝 docs,
👷 CI, ♻️ refactor, 🎨 structure, 🔧 config.

```
✨ Grade a listing against your preferences, not the pool
🐛 Two settings that could reach SQL and HTML unescaped, plus ruff in CI
```

The body is for what the subject cannot carry: the problem that made the change
necessary, and the decisions inside it that a reader would otherwise have to
reverse-engineer. Several small commits that each stand on their own beat one large one.

## Before you push

```bash
uv run pytest      # must be green
uv run ruff check . # must be clean; CI runs both
```

Ruff is configured in `pyproject.toml` — 100 columns, `E W F I UP B C4 SIM`.

## House rules that are easy to get wrong

- **Never edit an applied migration.** `migrations/*.sql` run in filename order and are
  recorded in `schema_migrations`; a column is added by adding `014_*.sql`. Editing `001`
  changes nothing on a database that already ran it.
- **A new setting is a `Setting` in `depas/preferences.py`, and nothing else.** The seed,
  the `depas config` commands and the `/config` chat menu all read that one declaration,
  so a knob added there arrives everywhere with a keyboard already. A parser with no
  `KIND` entry in `configure.py` is a mistake the tests catch.
- **The docs are part of the change.** README describes behaviour, DESIGN records why.
  A PR that changes what the bot does and leaves them saying the old thing is not done.
- **Parsers are tested against real saved HTML** in `tests/fixtures/`, so a portal
  changing its markup fails loudly instead of silently returning nothing. Add a fixture
  rather than a mock.
- **`callback_data` caps at 64 bytes** and Telegram silently drops the whole keyboard
  past it. Buttons carry a `rowid`, never a portal and an external id.
- **Settings live in the database, not the environment.** Only `TELEGRAM_BOT_TOKEN` and
  `DEPAS_DB_PATH` stay outside it. `seed.env` is a starting point that runs once.

## Writing

Comments and docstrings say *why*, in one line, and only where the reason is not on the
face of the code. Two languages on purpose: anything a person reads in the chat is
Spanish, everything else — comments, docstrings, errors, logs — is English.
