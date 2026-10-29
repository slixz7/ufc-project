# Runbook: running and maintaining the project

This is the hands-on guide: what each piece does, the commands to run it, and the project layout. For the story, results and design decisions, see the [README](../README.md).

## Components

| Piece | File | What it does |
|---|---|---|
| Clean | `01_load_and_clean.ipynb` | Loads the Kaggle dataset, drops fights with no winner, randomises which fighter is "A" |
| Features | `02_features.ipynb` | Rebuilds each fighter's stats from their *earlier* fights only (no leakage) |
| First model | `03_model.ipynb` | Logistic regression, baselines, walk-forward evaluation |
| Database | `04_postgres_sql.ipynb`, `sql/features.sql` | Loads Postgres and rebuilds the same features as SQL window functions, checked to match pandas exactly |
| Scoring (v1) | `pipeline/predict.py`, `sql/predictions.sql` | After-the-fact scoreboard that the Power BI dashboard reads |
| Updating | `pipeline/update_from_csv.py` | Adds new fights from the latest dataset download |
| Announced cards (manual) | `pipeline/upcoming.py` | Loads a card from a CSV, locks picks before the event, grades them after |
| **Model v2** | `pipeline/model_v2.py`, `sql/model_v2.sql` | The statistical model: a pick for every fight, past and upcoming |
| **Model v3** | `pipeline/model_v3.py`, `sql/model_v3.sql` | v2 plus the betting market |
| **Automation** | `pipeline/auto_update.py` (entry point) and `pipeline/auto_*.py` (see below), `.github/workflows/auto-update.yml` | Daily job: cards, lines, picks, results, odds history |
| **Goes the distance, with the betting market** | `pipeline/model_distance_v2.py` (and `pipeline/experiment_distance_market.py`, the test that justified it) | The same question with the favourite's strength from the betting line, added alongside the one below: backtest, locked predictions once a line exists, grading |
| **Goes the distance** | `pipeline/model_distance.py`, `pipeline/distance_scores.py`, `app/distance_ui.py` | The chance each fight goes to a decision: backtest, locked predictions for upcoming bouts, grading, and its display |
| **Title fights** | `pipeline/title_rules.py` | Decides what counts as a championship fight: the source also flags tournament bouts (Ultimate Fighter and Road to UFC finals) as "title" fights, so the Past events page tags only five-round flagged fights as Title and the rest as Tournament |
| **Read-only login** | `pipeline/db_roles.py` | Creates and audits the website's read-only database login, and retires an over-powered old one |
| **Friends' game** | `app/game_ui.py`, `pipeline/game_core.py`, `game_db.py`, `game_scores.py`, `game_admin.py`, `bout_results.py` | A private "beat the model" game on the site's Game page, with its own locked-down database user |
| **Backups** | `pipeline/backup.py`, `.github/workflows/backup.yml` | Weekly CSV backup of every table to a private repository, with a verifier and a safe restore |
| **README numbers** | `pipeline/readme_numbers.py` | Keeps every live number and table in `README.md` equal to the database |
| **Pick log** | `pipeline/pick_log.py`, `picks_log/` | Public, time-stamped record of every locked pick and goes-the-distance prediction (two files), committed by the daily job |
| **Discord digest** | `pipeline/digest.py` | Posts newly locked picks and event results to a Discord channel, once each, plus the friends' game reminder and leaderboard |
| Web app | `app/streamlit_app.py` | Read-only Streamlit app |
| Tests | `tests/test_auto_update.py` | Offline tests that run before every scheduled job |

## Run it yourself

All database commands below need a PostgreSQL database (a free hosted one such as Neon works). Use two connection strings: an **owner** string for anything that writes (everything except the web app) and a **read-only** string for the web app, made with `python pipeline/db_roles.py reader` (see *The website's read-only login* below; **never create it in Neon's web console**). Never commit a connection string.

1. **Data.** Download the fights CSV from Kaggle (`scarekrow/ufc-data`) into `data/` (not included here; check its licence on Kaggle).
2. **Install.**
   ```
   pip install -r requirements.txt
   pip install -r pipeline/requirements.txt
   pip install statsmodels jupyter matplotlib
   ```
3. **Notebooks.** Run `01`, `02` and `03` in order (no database needed), then `04` after setting the connection string:
   ```
   $env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
   ```
4. **Model v2** (owner string):
   ```
   python pipeline/model_v2.py setup
   python pipeline/model_v2.py backtest         # a pick for every past fight (a few minutes the first time)
   python pipeline/model_v2.py explain-past     # one-off: "Why this pick?" reasons for every past pick
   python pipeline/model_v2.py predict          # locks picks for loaded upcoming cards
   python pipeline/model_v2.py explain          # reasons for locked picks that lack them
   python pipeline/model_v2.py grade            # after the dataset includes the event
   ```
5. **Model v3** (owner string). Download the historical odds once (not stored in this repo):
   ```
   git clone --depth 1 https://github.com/nlbair/ufc-data.git data/nlbair
   python pipeline/model_v3.py setup
   python pipeline/model_v3.py load-odds --csv data/nlbair/data/ufc_all_fights_wide.csv
   python pipeline/model_v3.py backtest
   python pipeline/model_v3.py explain-past
   python pipeline/model_v3.py scoring          # log loss and Brier score with 95% intervals (every figure in the README's scoring table)
   ```
   **Goes the distance** (owner string; the daily job then keeps it going):
   ```
   python pipeline/model_distance.py setup       # create its two tables
   python pipeline/model_distance.py backtest    # score every past fight, each with a model trained on earlier fights only (about a minute)
   python pipeline/model_distance.py predict     # lock a prediction for every announced upcoming bout
   python pipeline/model_distance.py grade       # after the results are in the dataset
   python pipeline/model_distance.py scoring     # the report behind the README section and the Track record page
   ```
   **Goes the distance, with the betting market** (needs the winner model's lines loaded, `model_v3.py load-odds`, and the distance backtest above; the daily job then keeps it going):
   ```
   python pipeline/model_distance_v2.py setup       # create its two tables
   python pipeline/model_distance_v2.py backtest    # a market-informed prediction for every past fight that has a line (seconds)
   python pipeline/model_distance_v2.py predict     # lock one for every bout that has a line and a model-only prediction (the daily job does this near the event)
   python pipeline/model_distance_v2.py grade       # after the results are in the dataset
   python pipeline/model_distance_v2.py scoring     # the report, against the model-only prediction on the same fights
   python pipeline/readme_numbers.py update         # fills the README's market-informed numbers from the backtest
   ```
   By hand for one card (the daily job does this for you): write `fighter_a,fighter_b,odds_a,odds_b` (American odds, names as on the card), then
   ```
   python pipeline/model_v3.py odds --csv upcoming/odds-CARD.csv --dry-run
   python pipeline/model_v3.py odds --csv upcoming/odds-CARD.csv
   python pipeline/model_v3.py predict
   python pipeline/model_v3.py explain
   python pipeline/model_v3.py grade            # after model_v2.py grade
   ```
6. **Automation** (owner string): look first, then let it run.
   ```
   python pipeline/auto_update.py cards --dry-run --show      # what it read from Wikipedia and how each name was matched
   python pipeline/auto_update.py odds --dry-run --show       # matched betting lines
   python pipeline/auto_update.py all --dry-run               # the whole run, writes nothing
   python pipeline/auto_update.py all                         # the real thing
   python pipeline/auto_update.py odds-history                # what the daily line snapshots have collected
   python pipeline/auto_update.py pick-log --dry-run          # how many locked picks are not in the public log yet
   python pipeline/auto_update.py digest --dry-run            # what would be posted to Discord (needs DISCORD_WEBHOOK_URL)
   python pipeline/auto_update.py discord-test                # send one test message to check the webhook
   python pipeline/pick_log.py verify                         # in a git clone: when did each logged pick and prediction first appear in the history?
   python pipeline/readme_numbers.py check                    # is README.md in step with the database? (any connection string works; it only reads)
   python pipeline/readme_numbers.py update                   # rewrite the generated numbers and tables in README.md
   ```
   Exit code 0 = fine, 1 = a step failed, 3 = something needs your attention (listed at the top of the report).
7. **Schedule it on GitHub.** Add the repository secrets `DATABASE_URL` (owner string), `ODDS_API_KEY`, `KAGGLE_API_TOKEN` and, optionally, `DISCORD_WEBHOOK_URL`, run the "Auto update" workflow once by hand (it starts as a dry run), then set the repository variable `AUTO_ENABLED` to `true`. It runs daily at 14:17 UTC and emails you when something needs attention. Note that GitHub pauses scheduled jobs in a public repo after 60 days without repository activity.
8. **Web app locally** (read-only string):
   ```
   pip install -r app/requirements.txt
   python -m streamlit run app/streamlit_app.py
   ```
   It is deployed on Streamlit Community Cloud: main file `app/streamlit_app.py`, Python 3.12, and a `DATABASE_URL` secret holding the **read-only** string.
9. **Tests:**
   ```
   python -m pytest -q tests
   ```

## How the daily job is organised

`pipeline/auto_update.py` is only the entry point: it reads the command line, runs the steps in order (one failing never blocks the others) and prints the report. The work lives in six small modules. Each may import only from the ones listed against it, so there are no circular dependencies (a test enforces this):

| Module | What it holds | May import |
|---|---|---|
| `auto_common.py` | the logger, the project root, the `Report` printed at the end | nothing |
| `auto_names.py` | fighter-name matching: resolving a card name without guessing, and matching betting-site names (the most delicate logic) | nothing |
| `auto_cards.py` | Wikipedia: the schedule, fight cards, `step_cards` | common, names |
| `auto_odds.py` | The Odds API, the private odds history, `step_odds`, `odds_history`, the Monday odds-dataset refresh | common, names |
| `auto_picks.py` | locking picks: `step_v2` and `step_lock` | common |
| `auto_results.py` | the Kaggle download, grading and scoring: `step_results` | common |

`auto_update.py` re-exports every name it has ever exposed, so `import auto_update` and `python pipeline/auto_update.py <command>` work exactly as before. To add a step, write it in the module it belongs to, then register it in `run()` and in the `choices` list in `main()`; a test checks that the two stay in step.

## The pick log, and pushing code afterwards

After each real run the GitHub job commits `picks_log/locked_picks.csv` (only that folder). That means the repository on GitHub moves ahead of your computer, so **pull before you push**:

```
git pull --rebase
git push
```

Only the GitHub job writes the log (your own runs skip it, so the two never collide); `pick-log --write-pick-log` forces it. To make the history harder to rewrite, protect `main` against force pushes and deletions (Settings, Rules, New branch ruleset) and leave "Require a pull request" **off**, or the job's own push is blocked.

## The website's read-only login

The public site must connect with a login that can only read, so that a leaked password could not change your data. **Make it with `python pipeline/db_roles.py reader`, never in Neon's web console.** A login made in the console is automatically given extra powers (it joins Neon's `neon_superuser` role, which can read and write every table and create other logins), so it is not read-only whatever grants are added. A login made with SQL has none of that.

With your **owner** connection string loaded:

- `python pipeline/db_roles.py reader` creates (or repairs) the login `site_reader`: SELECT on the public tables only, nothing on the private ones (`odds_snapshots`, `odds_bout_summary` and the game's tables). It checks the result and writes its connection string to `data/site_database_url.txt` without printing it. Put that in the site's secrets as `DATABASE_URL`, then delete the file.
- `python pipeline/db_roles.py audit` checks that login (no extra roles or powers, no write access, no private tables, cannot create anything) and flags an over-powered old login if one still exists. Run it after any change to database permissions.
- `python pipeline/db_roles.py retire-old` strips the old login `app_reader` of every grant and, where Neon allows it, stops it logging in and replaces its password. Then delete it in the Neon console (Roles & Databases).
- A leaked or lost read-only connection string: `python pipeline/db_roles.py reader --rotate-password`, then update the secret.
- Anything else that used the old login (for example Power BI) must be switched to the new string.

## The friends' game

A private "beat the model" game on the site's **Game** page (the rules are in its *How it works* tab and in the README). It writes to the database through **its own database user**, which can touch only `game_players`, `game_picks` and `game_attempts`; the site's read-only user cannot read them.

One-off setup, with your **owner** connection string loaded:

1. `python pipeline/game_admin.py setup` creates the tables and the game's database user, runs an audit, and writes the new connection string to `data/game_database_url.txt` (git ignores `data/`). It never prints the password.
2. `python pipeline/bout_results.py record` records how past bouts ended, so cards that have already happened can be scored. The daily job does this from then on.
3. In Streamlit (your app, Settings, Secrets) add two lines: `GAME_DATABASE_URL = "<the line from the file>"` and `GAME_INVITE_CODE = "<a code you choose>"`. Then **delete the file**.
4. Reboot the app, open **Game**, join, and give your friends the invite code privately.

Running it:

- `python pipeline/game_admin.py players` lists who has joined and how many picks each has made.
- A forgotten PIN: `python pipeline/game_admin.py reset-pin "Nickname"` prints a new one once; tell them privately. It also clears a lockout. `delete "Nickname"` removes a player and their picks.
- `python pipeline/game_admin.py audit` checks that the game user can reach the game tables and nothing else, and that the read-only user cannot read them. Run it after any change to database permissions.
- A leaked invite code: change the `GAME_INVITE_CODE` secret. Existing players are unaffected. A new database password: `python pipeline/game_admin.py setup --rotate-password`, then update `GAME_DATABASE_URL`.
- Picks close **6 hours before the card's first fight**, the same for every card (the number is `PICK_BUFFER` in `pipeline/game_core.py`; the page's wording follows it). The first-fight time comes from the odds feed: each daily run saves the earliest start time it lists for every card in the public table `event_starts`. While a card is open its recorded time follows the best current estimate; once its picks have closed it is never changed, so a closed card cannot reopen. If the feed lists only part of a card so far (weeks out it shows just the top fights), or gives every fight one time, a 5-hour margin is taken off the earliest listed time, because the early prelims may not be listed. If a card has no recorded time, picks close at 18:00 UTC the day before its event date, which is safe for any card in the world. `SELECT * FROM event_starts ORDER BY event_date` shows what is recorded. You can correct a time by hand with `UPDATE event_starts SET first_start = '2026-11-14 18:00+00' WHERE event_date = '2026-11-14'`, but while the card is open the next daily run replaces it with the feed's estimate, so only correct a card that is about to close. The daily report warns when a card within three days has no start time or is listed only in part.
- **Discord.** Once at least one player has joined, the daily digest posts (a) a reminder within 30 hours before a card's picks close, with how many players have picked so far and never who picked what, and (b) a leaderboard after every result for a card is in (top eight, scores only). Each is posted once and recorded in `digest_sent`; to post one again, delete its row, for example `DELETE FROM digest_sent WHERE key = 'game_close:2026-10-31'` (reminders are keyed `game_close:<date>`, leaderboards `game_results:<date>:<event>`). The scoring is `pipeline/game_scores.py`, shared with the website so the two cannot disagree.
- Wrong PINs lock a nickname (5 in 15 minutes, 20 a day); wrong invite codes pause joining (10 an hour); at most 100 players.
- The weekly backup includes the game tables (nicknames, hashed PINs and picks) in the private backup repository.
- The tests that check the permissions need a Postgres server: the Tests workflow starts one. To run them yourself, set `TEST_PG_ADMIN_URL` to a server where that user may create databases and roles; without it they skip.

## The demo

`python demo/run_demo.py` runs the whole project on invented fighters with no accounts (see `demo/README.md`). Three files do the work: `demo/synth.py` invents the base tables (`fights`, `fighter_fights`), a historical-lines table and five cards; `demo/build_demo.py` fills an empty Postgres and then runs the REAL pipeline commands in order, replaying two played cards with each stage's `--today` option; `demo/run_demo.py` starts a private Postgres (the `pixeltable-pgserver` package), builds or reuses the demo, and starts the website with `DEMO_MODE=1`.

- **The demo is also a build-from-nothing test.** `tests/test_demo.py` builds whole demos on a real Postgres and opens every page of the website on them. If you change a table layout, add a pipeline stage or a page query, that test fails until `demo/build_demo.py` (or `synth.py`) is updated. That is intended: it proves the project can still be built from scratch.
- **It can only ever build into an empty database** (or one it built before) and refuses hosted providers. Its database logins are `demo_site_<database>` and `demo_game_<database>`, so it never touches `site_reader` or `game_app`.
- **`DEMO_MODE` must never be set on the real site.** It switches on the DEMO banner and a sign-in hint; the launcher sets it, and a test checks that the daily workflow does not.
- **The demo's database lives in your system's cache folder, never in the project** (a database inside OneDrive gets damaged by the syncing). `--data-dir` or `UFC_DEMO_DIR` changes it; deleting the folder removes the demo.
- **A model change that needs more history** (for example a larger minimum for the market model) may need a bigger `normal` size in `synth.py`; the builder skips the market model, and says so, when the history is too short for it.

## Experiments

An experiment is a read-only script that answers one question and states, in its own header, the rule for what counts as a yes, written before any real result was seen. It writes nothing to the database or the site.

- `python pipeline/experiment_distance_market.py` asks whether the opening betting line adds anything to the goes-the-distance model, beyond what that model already knows. It needs `DATABASE_URL`, the distance backtest (`model_distance.py backtest`) and the historical lines (`model_v3.py load-odds`) already in place, and it prints a report ending in `VERDICT: ADOPT` or `VERDICT: DROP`.
- **Either way, the result goes in the README.** A DROP is recorded like the other retired ideas; an ADOPT is only the start: the feature is then built properly, with tests, and still has to earn its place on live fights.
- To ask for help with a result, paste the whole printed report, not just the verdict.

## Backups

`.github/workflows/backup.yml` runs every Sunday (and on demand), dumps every table except the rebuildable `fight_reasons` cache to plain CSV in a stable order, checks the files against `MANIFEST.csv`, and commits them to a **private** repository named `ufc-project-backups`. A week with no new data produces identical files and no commit.

One-off setup:

1. Create a **private** repository `ufc-project-backups` on GitHub with **Add a README file** ticked (an empty repository cannot be checked out).
2. Create a fine-grained token (profile photo, Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate new token): resource owner = you, repository access = **Only select repositories** and choose `ufc-project-backups`, repository permission **Contents: Read and write**, expiry up to one year. Copy it once.
3. In this repository add the secret `BACKUP_REPO_TOKEN` (the token) and the variable `BACKUP_ENABLED` = `true`. `DATABASE_URL` is the same **owner** secret the daily job uses: the read-only user cannot read the odds history.
4. Run **Actions, Backup, Run workflow** once and check the private repository has a `data/` folder.

The token expires: put the date in your calendar. An expired token makes the weekly run fail and GitHub emails you.

On your own computer: `python pipeline/backup.py dump` (writes to `data/backup`, which git ignores), `python pipeline/backup.py verify data/backup` (needs no database).

To restore: clone `ufc-project-backups`, create the empty tables with the project's setup commands (`model_v2.py setup`, `model_v3.py setup`, `model_distance.py setup`, `model_distance_v2.py setup`; the odds history table is created by the odds step's first real run, or by `python -c "import sys; sys.path.insert(0, 'pipeline'); import auto_update, model_v2; auto_update.ensure_snapshot_tables(model_v2.make_engine())"`), then run `python pipeline/backup.py restore PATH_TO_CLONE/data --table odds_snapshots` for each table you need. Restore only loads into an empty table, refuses a damaged file, and never deletes. Tables that can be rebuilt (`fights`, the backtests, the reasons) are quicker to rebuild with their own commands. A blank cell comes back as NULL.

## Goes the distance

The model estimates the chance a fight ends in a decision from how long each fighter's earlier fights lasted (share of scheduled time used, decision rate, how often they won or lost by a finish) plus title or five rounds. Fights since 2002 with 3 or 5 scheduled rounds are used; disqualifications are left out. Scheduled rounds are not on the cards, so a title fight or main event is taken to be five rounds. Predictions are locked when a bout is announced (with the winner picks), never change, and are graded when the dataset has the result. The daily job runs `predict` in the v2 step and `grade` plus `backtest` with the results. `python pipeline/export_history.py` saves the history the models read to `data/history_export.csv` (read-only; not committed).

The market-informed version (`model_distance_v2.py`) keeps this model's prediction and adds the favourite's strength from the betting line (the favourite's chance above 50%, and its square) in a small second stage fitted on earlier fights only. It was added on a narrow pass of the rule in `experiment_distance_market.py`, so it runs **alongside** the model-only prediction and never replaces it: the website shows the model-only figure as the headline and the market-informed one beside it, and both are scored on the same fights. The daily job locks it in the same step as the market-informed winner picks (`step_lock`: within the lock window, using the latest line, once, never changed), grades it with the results, and extends its backtest after the Monday odds refresh. The friends' game still uses the model-only prediction. If the live record ever shows it is not helping, stop calling it in `step_lock` and leave the tables in place: nothing else depends on it.

## README numbers

Live numbers in `README.md` sit between markers: `<!--n:name-->65.4%<!--/n-->` for one number, and `<!--auto:name-->` ... `<!--/auto:name-->` around a table. `readme_numbers.py update` recomputes them from the database; `check` says what is stale. **Edit the prose freely, but never between the markers**, because the next update overwrites it. Numbers from one-off experiments (the leakage story, model comparisons) are plain text and are never touched. The generator also guards the claims the prose makes ("the blend beats the line", "the probabilities track the stated confidence", ...): if the data stops supporting one, `check` says `CLAIM NO LONGER HOLDS` and the daily report asks for attention. The GitHub job refreshes the file on Mondays and commits it only if something changed. To add a new generated number, add its name to `known_names()` and `compute()` in `readme_numbers.py`, then put the markers in the README.

## Discord digest

Create a webhook for a channel (channel settings, Integrations, Webhooks), store its address as the repository secret `DISCORD_WEBHOOK_URL`, and check it with `python pipeline/auto_update.py discord-test`. The address is a secret: anyone holding it can post to the channel. If it leaks, delete the webhook in Discord and make a new one. The job posts picks for cards within 14 days and each finished event's results, each exactly once (tracked in the `digest_sent` table); a post that fails is retried on the next run.

## Fixing names the automation will not guess
When the daily report lists a held name, add one line to `upcoming/aliases.csv` as `Card Name,Dataset Name` (use `NEW` for a fighter with no UFC history) and push. The job reads the file from the repository.

## Project structure

```
01_load_and_clean.ipynb ... 04_postgres_sql.ipynb   the original analysis
sql/features.sql             feature views (window functions)
sql/predictions.sql          prediction table and dashboard views
sql/model_v2.sql, model_v3.sql   tables and views for the two model versions
sql/readonly_role.sql        explains the read-only login (created by pipeline/db_roles.py)
pipeline/                    predict.py, game_core.py, game_db.py, game_admin.py, bout_results.py, update_from_csv.py, upcoming.py, model_v2.py, model_v3.py, auto_update.py (entry point), auto_common/names/cards/odds/picks/results.py, pick_log.py, digest.py, requirements.txt
app/                         streamlit_app.py and its requirements
.streamlit/config.toml       the web app's theme
tests/                       offline tests
.github/workflows/           auto-update.yml (the schedule)
upcoming/                    an example card and aliases.csv (card name -> dataset name)
picks_log/                   the public pick log (written by the GitHub job) and an explainer
docs/                        this runbook, the Power BI guide, and screenshots
```
