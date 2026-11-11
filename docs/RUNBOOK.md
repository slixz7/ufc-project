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
| **Goes the distance** | `pipeline/model_distance.py`, `pipeline/distance_scores.py`, `pipeline/distance_reasons.py`, `app/distance_ui.py`, `app/distance_why_ui.py` | The chance each fight goes to a decision: backtest, locked predictions for upcoming bouts, grading, and its display |
| **Model health** | `pipeline/model_health.py`, `docs/MODEL_CARDS.md` | Each model's live record against its own probabilities, and the retirement rules fixed in advance; the daily job's `health` step |
| **Evidence meter** | `pipeline/evidence.py`, `app/evidence_ui.py` | The Track record's "How much evidence is there?": each claim's live estimate, the backtest's, and the graded fights needed to be reasonably sure |
| **Insights page** | `pipeline/insights.py`, `pipeline/small_sample.py`, `app/insights_ui.py` | Plain findings from the fight data on the site's Insights tab: the maths, the shared small-sample statistics, and the HTML |
| **Title fights** | `pipeline/title_rules.py` | Decides what counts as a championship fight: the source also flags tournament bouts (Ultimate Fighter and Road to UFC finals) as "title" fights, so the Past events page tags only five-round flagged fights as Title and the rest as Tournament |
| **Read-only login** | `pipeline/db_roles.py` | Creates and audits the website's read-only database login, and retires an over-powered old one |
| **Friends' game** | `app/game_ui.py`, `pipeline/game_core.py`, `game_db.py`, `game_scores.py`, `game_admin.py`, `bout_results.py` | A private "beat the model" game on the site's Game page, with its own locked-down database user |
| **Backups** | `pipeline/backup.py`, `pipeline/recover.py`, `.github/workflows/backup.yml` | Weekly CSV backup of every table to a private repository, with a verifier and a safe restore |
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
   python pipeline/model_distance.py explain-upcoming   # store the "Why this distance prediction?" breakdown for locked bouts that have none (predict does this too)
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
   python pipeline/auto_update.py health --dry-run            # each model's live record against what it said, and the retirement rules
   python pipeline/auto_update.py digest --dry-run            # what would be posted to Discord (needs DISCORD_WEBHOOK_URL)
   python pipeline/auto_update.py discord-test                # send one test message to check the webhook
   python pipeline/pick_log.py verify                         # in a git clone: when did each logged pick and prediction first appear in the history?
   python pipeline/readme_numbers.py check                    # is README.md in step with the database? (any connection string works; it only reads)
   python pipeline/readme_numbers.py update                   # rewrite the generated numbers and tables in README.md
   ```
   Exit code 0 = fine, 1 = a step failed, 3 = something needs your attention (listed at the top of the report).
7. **Schedule it on GitHub.** Add the repository secrets `DATABASE_URL` (owner string), `ODDS_API_KEY`, `KAGGLE_API_TOKEN` and, optionally, `DISCORD_WEBHOOK_URL`, run the "Auto update" workflow once by hand (it starts as a dry run), then set the repository variable `AUTO_ENABLED` to `true`. It runs daily at 14:17 UTC and emails you when something needs attention. Note that GitHub pauses scheduled jobs in a public repo after 60 days without repository activity. GitHub starts scheduled runs when it has capacity, not at the stated time: this project's have started about five hours after the 14:17 UTC schedule (around 19:30 UTC, 12:30 PM Pacific), every day. Treat the time as "sometime that UTC day". It matters for the one-shot lock of a card's predictions: they can only be locked while the event date is still ahead, so a card's last chance is the last UTC day before it (a manual run, with the dry-run box unticked, locks them at once).
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

- `python pipeline/db_roles.py reader` creates (or repairs) the login `site_reader`: SELECT on the public tables only, nothing on the private ones (`odds_snapshots`, `odds_bout_summary`, `fight_odds`, `upcoming_odds` and the game's tables). It checks the result and writes its connection string to `data/site_database_url.txt` without printing it. Put that in the site's secrets as `DATABASE_URL`, then delete the file.
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

## Betting-feed names that do not match the card

When a run says `CHECK THE CARD: a line lists X vs Y on DATE, but one of them is on our card against someone else`, it is one of two things. Either the card really changed (a replacement opponent: re-run the cards step), or a fighter is spelled differently by the betting feed and by the card ("Seok Hyun Ko" and "Ko Seok-hyeon" score 0.64 where a match needs 0.88), so that bout gets no line, and therefore no market-informed pick or distance prediction. For the second case, add one line to `upcoming/odds_aliases.csv` (columns `feed_name,card_name`; the feed's spelling first, the card's spelling exactly as it appears on the card). The alias is used only to attach a line to a bout that is already on the card, on a nearby date, with the other fighter matching too, so it cannot invent a match or loosen anything. The next run's "Betting lines" section then says `feed spellings read through upcoming/odds_aliases.csv: ...`. A mistake in the file (wrong header, a half-filled row, one feed name with two card names) is reported under "Needs your attention" with the reason, and the job carries on without the aliases. The file must be saved as UTF-8 (names have accents): the loader accepts Notepad's UTF-8 with or without its start-of-file mark and says so if the encoding is wrong, but do not append to it with an old-style PowerShell command (`Add-Content` in Windows PowerShell 5 writes the wrong encoding); edit it in Notepad, or ask for the new line in an update zip. This is a different file from `upcoming/aliases.csv`, which maps card names to the fighter data.

## Updates from the assistant (one zip)

An update arrives as one `ufc-update-DATE-N.zip` plus a checksum in the message. Download it, then from the project folder run `python tools/apply_update.py` (or `python tools/apply_update.py PATH_TO_ZIP --sha256 CHECKSUM`; add `--dry-run` to see what it would do). Before it writes anything it checks that the zip is for this project, that every file in it is in its manifest and matches its SHA-256, and that every destination is inside the project (never `.git`); if any check fails it says `NOTHING WAS CHANGED` and why. Then each file it replaces is first copied to `.update_backup/<date-time>/` (git ignores that folder), a file that is already identical is not touched, nothing is ever deleted, and a failure part-way puts everything back. It never runs anything from the zip. Afterwards run `python -m pytest -q tests` and look at `git status`. The first time, place `tools/apply_update.py` by hand; every later update is a zip.

## Failure alerts

If the daily job or the weekly backup fails, its last step posts one short message (which workflow, the branch and commit, and a link to the run) to a private Discord channel. It uses its own webhook, the repository secret `ALERT_WEBHOOK_URL`, so a failure never appears in the channel your friends read; if the secret is not set, nothing is sent. To set it up:

1. In Discord, make a channel only you can see (for example `#alerts`). Open its settings, then **Integrations**, then **Webhooks**, then **New Webhook**, and copy the webhook address.
2. In GitHub, open the repository's **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**. Name it `ALERT_WEBHOOK_URL` and paste the address.
3. To test it from your computer, put the address in the variable, clear the screen, and run the script: `$env:ALERT_WEBHOOK_URL = "PASTE-THE-WEBHOOK-HERE"`, then `cls`, then `python pipeline/notify_failure.py "Test"`. The private channel should show "Test failed" with no link, and the terminal says `The failure alert was sent.` Never paste the address into chat or a file.

The script uses only Python's standard library (so it works even when the package install is what failed), only ever posts to Discord's own webhook addresses, switches mentions off so nothing can ping anyone, never prints the address (not even inside an error message), and always exits 0. GitHub's own email about a failed run still arrives as before.

## Locked dependencies

`requirements.txt` files name only the minimum each package needs. `constraints-ci.txt` records the exact version of every package that the GitHub workflows install, and the workflows install with `-c constraints-ci.txt`, so a new release of any library cannot change the daily job, the backup or the tests until the lock is refreshed. It was built by installing the requirements into a brand-new Python 3.12 environment and running the whole test suite against exactly those versions. `tests/test_pinned_dependencies.py` checks that the lock is well formed, covers everything the requirements name within their minimums, that every workflow installs through it, and (in the workflows) that the installed versions equal it. The lock is for the workflows only (Python 3.12 on Linux): your own computer keeps installing from the requirements files as before. It is refreshed automatically every month (next section). Do not edit it by hand or regenerate it on another Python version or operating system. Dependabot (`.github/dependabot.yml`) also opens a weekly pull request when a GitHub Action the workflows use has a new version, and the Tests workflow runs on it.

## The monthly lock refresh

`.github/workflows/refresh-lock.yml` runs on the 1st of every month at 06:23 UTC, and on demand (Actions, **Refresh lock**, **Run workflow**). It keeps `constraints-ci.txt` fresh without anyone asking, in three jobs.

1. **refresh** installs the newest versions the requirements files allow into a brand-new Python 3.12 environment (deliberately without the lock), then runs the static check, both credential scans and the whole test suite against those exact versions, including the check that what is installed equals the proposed lock. If the newest versions are the locked ones it says so and stops. Otherwise it hands the proposed lock to the next job, but only if the last pytest line is a clean pass.
2. **open-pr** commits the new lock to the branch `lock-refresh` and opens (or updates) one pull request, with a table of every changed package, major bumps and downgrades first, and what was checked.
3. **alert** posts to the private alert channel (the same `ALERT_WEBHOOK_URL` as the other workflows) if either job failed.

**One-time setting.** Job 2 can only open a pull request if you allow it: **Settings, Actions, General, Workflow permissions, tick "Allow GitHub Actions to create and approve pull requests"**, and save. Without it the first run fails at that step and the alert channel says so. Then do a first run by hand to see it work.

**What you do each month.** Nothing, unless a pull request appears. Read its table (the **MAJOR** and **DOWNGRADE** rows first), then merge. Merging changes only `constraints-ci.txt`; the daily job and the backup use the new versions on their next runs. GitHub does not run the Tests workflow on a pull request that another workflow opened, but the same checks already ran in job 1 and Tests runs on `main` when you merge. Closing and reopening the pull request makes GitHub run Tests on it now. The branch `lock-refresh` belongs to the job and is force-pushed each time.

**If the suite fails against the new versions,** the run fails (red badge and an alert), no pull request is opened and the old lock stays in force, so the daily job is unaffected. Read the log of the failed run to see which package broke what. To hold a package back, add an upper bound in the requirements file (for example `pandas<3` in `pipeline/requirements.txt`), push, and run the workflow again.

**Why it is built as two jobs.** The refresh job installs and runs brand-new packages nobody has reviewed, so it has read-only access, no secrets and no stored token: a bad release can break the refresh but cannot push, comment or read anything. The job that can write installs nothing and runs only `tools/refresh_lock.py` from `main`, which treats the proposed lock as data (it reads only exact `name==version` lines, writes the header and the pull request text itself, rejects a lock that has lost a core package or changed size wildly, and requires the test result to be a clean `N passed, M skipped`), and the job then refuses to continue unless exactly one file, `constraints-ci.txt`, changed. No expression is ever spliced into a shell script. What a compromised release could still do is propose a lock line you do not like, which is why a person merges it after reading the table.

`tests/test_refresh_lock.py` checks the tool (including that it reproduces the real lock exactly and that it uses only the standard library) and `tests/test_refresh_lock_workflow.py` checks the rules above, so they cannot erode quietly. The workflow file was also checked with actionlint, the standard static checker for workflows.

## Security settings to turn on

`SECURITY.md` is the public policy; these GitHub settings make it work. They are settings, not files, so nothing in the repository can check them, and I have not seen your repository's settings. Each is a few clicks on the repository page, under **Settings, then Code security** (GitHub has renamed that page over time, so look for these names): (1) **Private vulnerability reporting**: enable it, so that **Security, Report a vulnerability** exists for the policy to point to; (2) **Code scanning**: choose **Set up, Default** for CodeQL (the Python code and the workflow files). Default setup is a setting, so it needs no workflow file and no upkeep, and its results appear under **Security, Code scanning**; (3) **Secret scanning** and **push protection**: enable both, so GitHub itself refuses a push that contains a secret, on top of `tools/scan_secrets.py`. Optional: **Dependabot alerts**, which warn about known vulnerabilities in the packages named in the requirements files. If a setting is missing from the page, your plan or the repository's visibility may not offer it.

## Credential scans

`python tools/scan_secrets.py` reads every file in the project, and `python tools/scan_secrets.py --history` reads every commit on every branch, for Neon passwords, Discord webhook addresses, connection strings with a password that point at a real host, API keys assigned to this project's variables, an Odds API key inside an address, and files that should never be committed (`.env`, `secrets.toml`, `kaggle.json`, private keys). It prints where it found something and what kind, **never the text itself**, so its output is safe to paste; placeholders such as `USER:PASSWORD@HOST` and local addresses are not findings. The Tests workflow runs both on every push, with the whole history checked out; on a shallow clone the history scan refuses (exit 2) rather than give false comfort. If it ever reports something, treat that credential as exposed: reset the password, recreate the webhook or issue a new key, update the GitHub and Streamlit secrets, and only then clean up the history, because once a secret has been public, removing it from the history does not make it safe again.

## The public face of the repository

Set by hand in GitHub (they are settings, not files): on the repository page, the gear next to **About** holds the description, the website and the topics, and **Settings**, then **General**, then **Social preview** takes the 1280 by 640 image (`docs/images/social-preview.png`). Description: "Predicts UFC fights before they happen, locks every pick with a timestamp, explains it, and grades itself in public. Python, PostgreSQL, Streamlit, GitHub Actions. Honest evaluation: a backtest kept separate from the live record." Website: `https://ufc-predictor.streamlit.app/`. Topics: `ufc`, `mma`, `sports-analytics`, `machine-learning`, `scikit-learn`, `forecasting`, `postgresql`, `sql`, `streamlit`, `github-actions`, `data-science`, `python`.

## Backups

`.github/workflows/backup.yml` runs every Sunday (and on demand), dumps every table except the rebuildable `fight_reasons` cache to plain CSV in a stable order, checks the files against `MANIFEST.csv`, and commits them to a **private** repository named `ufc-project-backups`. A week with no new data produces identical files and no commit.

One-off setup:

1. Create a **private** repository `ufc-project-backups` on GitHub with **Add a README file** ticked (an empty repository cannot be checked out).
2. Create a fine-grained token (profile photo, Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate new token): resource owner = you, repository access = **Only select repositories** and choose `ufc-project-backups`, repository permission **Contents: Read and write**, expiry up to one year. Copy it once.
3. In this repository add the secret `BACKUP_REPO_TOKEN` (the token) and the variable `BACKUP_ENABLED` = `true`. `DATABASE_URL` is the same **owner** secret the daily job uses: the read-only user cannot read the odds history.
4. Run **Actions, Backup, Run workflow** once and check the private repository has a `data/` folder.

The token expires: put the date in your calendar. An expired token makes the weekly run fail and GitHub emails you.

On your own computer: `python pipeline/backup.py dump` (writes to `data/backup`, which git ignores), `python pipeline/backup.py verify data/backup` (needs no database).

**Practise a restore.** A backup you have never restored is only a hope. `python pipeline/restore_drill.py PATH_TO_DATA` rebuilds the whole database from a backup into a throwaway Postgres on your own computer, then compares every table row for row and column for column. It never reads `DATABASE_URL` and cannot touch the real database. `PATH_TO_DATA` is the `data` folder of a clone (or a downloaded zip) of `ufc-project-backups`; run `pip install -r demo/requirements.txt` once for the throwaway Postgres. It ends with `RESTORE OK`, or with the list of what did not come back. On Windows, git rewrites every line ending when it clones, which changes every file's checksum although nothing is damaged; the drill and the restore accept a file whose only difference is that (and say so), and the backup repository now tells git not to do it. Any other difference is refused. Do it once, then every few months, and after you add a table. A test fails until every new table has a step in `SCHEMA_STEPS` in `pipeline/restore_drill.py`, which is the tested recovery recipe. A table that is in the database but that nothing uses any more (today only `predictions`: the first version's table of predictions for a block of 2025 fights, an older design of `fight_predictions`, checked against the real table on 2026-10-30) is listed in `LEGACY_TABLES` there: it stays in the backup, is reported as "not restored, by design", and does not fail the drill. Check what a table is before adding it to that list.

**If the real database were ever lost,** use the one-command recovery below. What it automates is this manual order, into a new empty database with `DATABASE_URL` set to it:

1. `python pipeline/update_from_csv.py` rebuilds `fights` and `fighter_fights` from the Kaggle data. (The layout of these two tables, which no setup command creates, is `BASE_TABLES_DDL` in `pipeline/restore_drill.py`: the real column types and indexes, read from the real database on 2026-10-30, with the counts as `bigint`; a test pins it. They can also be restored from the backup.)
2. Create the other tables: `python pipeline/upcoming.py setup`, `python pipeline/model_v2.py setup`, `python pipeline/model_v3.py setup`, `python pipeline/model_distance.py setup`, `python pipeline/model_distance_v2.py setup`, then this one line for the tables the daily job and the friends' game create for themselves: `python -c "import sys; sys.path.insert(0, 'pipeline'); import model_v2, bout_results, digest, auto_odds, game_db; e = model_v2.make_engine(); [f(e) for f in (bout_results.ensure_table, digest.ensure, auto_odds.ensure_snapshot_tables, auto_odds.ensure_event_starts, game_db.ensure_tables)]"`
3. Restore the tables that cannot be rebuilt, each with `python pipeline/backup.py restore PATH_TO_DATA --table NAME`: `odds_snapshots` first (it cannot be downloaded again), then `upcoming_odds`, `upcoming_picks_v2`, `upcoming_picks_v3`, `upcoming_distance_v1`, `upcoming_distance_v2`, `event_starts`, `bout_results`, `digest_sent`, `game_players`, `game_picks` and `game_attempts`.
4. Rebuild the rest with their own commands: the backtests (`model_v2.py backtest`, `model_v3.py load-odds` then `backtest`, `model_distance.py backtest`, `model_distance_v2.py backtest`) and the reasons (`model_v2.py explain-past`, `model_v3.py explain-past`, `explain`).
5. Recreate the two restricted logins (`db_roles.py` for the website, `game_admin.py setup` for the game) and put the new connection strings in the GitHub secrets.

Restore only loads an empty table, refuses a damaged file, and never deletes. A blank cell comes back as NULL. Backups made before the 2026-10 fix wrote a whole number in a column that also has blanks as `1.0`; restore accepts those, and new backups write `1`.

## Two test environments

The scheduled daily job installs **only** `pipeline/requirements.txt` and runs the whole test suite before it writes anything; the **Tests** workflow installs the web app's packages as well (so the site's tests can run) and has a Postgres server. A test that needs a package the daily job does not have (for example Pillow, which Streamlit brings in) would pass in Tests and then stop the daily job the next morning, which is what happened once with the tab-icon tests (`No module named 'PIL'`). So the Tests workflow has a second job, **daily-environment**, that installs exactly what the daily job installs and runs the same command, and `tests/test_two_environments.py` keeps the two identical. **The rule:** a test that needs a package outside `pipeline/requirements.txt` must skip when it is missing (`pytest.importorskip`), never crash. If the daily job ever fails at "Offline tests", look at the failing test names: a `ModuleNotFoundError` means a test needs a package the daily job lacks.

## Packages the code needs but does not declare

On 2026-11-06 the daily job started failing with `Kaggle access: FAILED: No module named 'six'`. The `kaggle` package imports `six` in its own code but does not list it as a dependency; it had worked only because `python-dateutil` happened to depend on `six`. When the monthly lock refresh moved `python-dateutil` to 4.0, which no longer does, `six` stopped being installed. The refresh's tests all passed, because nothing tested the Kaggle client (it needs credentials), so a broken environment looked healthy.

The fix has three parts. `six` is now declared in `pipeline/requirements.txt` with the reason beside it. `python tools/refresh_lock.py pin six==1.17.0 --date YYYY-MM-DD --why "..."` is the sanctioned way to add one pin to the lock (it keeps the file sorted and well-formed, and records in the header what was added and why; the next monthly refresh regenerates the file, including it, so the note disappears). And `tests/test_installed_imports.py` reads the installed `kaggle` and `kagglesdk` packages' source and fails if any module they import at the top level is not installed, so a gap like this now fails the tests, which means it fails the monthly refresh (it runs them on the new versions and opens no pull request) and the daily job's offline tests (which stop the job before it writes anything), instead of breaking the job. **If a job ever reports `No module named 'X'`:** add `X` to the right requirements file with a comment saying who needs it, pin it with the command above, run the tests, push, and run **Auto update** with dry run ticked to confirm.

## Code scanning (CodeQL) alerts

GitHub's CodeQL scans the Python code and the workflow files on every push to `main` and weekly (Settings, Advanced Security, Code scanning, "Default" setup; it is a setting, not a file). Its findings are under **Security, Code scanning**. **How to triage one:** open it, read the flagged line, and decide whether it is real. If it is, fix it. If it is not, dismiss it with the honest reason (False positive, Used in tests, or Won't fix) and a one-line comment saying why, so that the list shows only what still needs a decision.

**The first scan raised three "Incomplete URL substring sanitization" alerts, and none was a vulnerability.** Two were in tests (`tests/test_repo_hygiene.py` and `tests/test_citation_and_badges.py`): assertions that the README and the citation file contain the live site address. They check the project's own documents and sanitize nothing, so they were dismissed as "Used in tests". The third, in `demo/build_demo.py`, was a safety guard that refused any host containing `neon.tech`, `amazonaws.com` or `supabase`. It failed safe (a deny-list), and the real protection is the next rule (only an empty database, or one the builder made, is ever written to), but a substring test is a clumsy way to recognise a host. It, the restore drill and the recovery rehearsal now share one function, `pipeline/hosted_db.py`, which matches whole domain labels (`x.neon.tech` is refused, `evilneon.tech` is not mistaken for Neon), and `tests/test_hosted_db.py` covers it. When a hosting provider needs adding, add it to the two tuples at the top of that file.

## Dependabot pull requests (the versions of GitHub Actions)

Once a week GitHub's Dependabot checks the actions the workflows use (checkout, setup-python, upload-artifact and download-artifact) and opens a pull request for each newer version; the two artifact actions come as ONE pull request, because they are used together. The Tests workflow runs on it, and Dependabot's own "github_actions in / for ..." runs in the Actions tab are just it doing its job. **What to do:** if Tests is green, merge it the same way as the lock-refresh pull request (Merge pull request, Confirm merge); if it is red, open the failed run and send it to me instead of forcing it. Tests only exercises some of the workflows, so after merging a bump of an action the other workflows use, run each of them once by hand (Actions, the workflow, Run workflow): **Auto update** with "dry run" ticked, **Backup**, **Refresh lock** and **Live site check**, and look for green. If a pull request's checks predate a fix on `main`, comment `@dependabot rebase` on it and they run again. The rules the tests enforce: every action is pinned to a major version tag, the same major in every workflow, upload and download artifact both on v4 or newer (they share one artifact service from v4, so their majors need not be equal; v3 cannot exchange artifacts with it), and no test names a literal version (`tests/test_dependabot_compat.py`). **Update zips and workflow files:** a zip that carries a workflow file used to bring the action versions it was written with, undoing what Dependabot had done to that file (this broke the "each action has one version everywhere" test once). `tools/apply_update.py` now moves any workflow file it places to the versions the rest of your workflows already use, prints each change as `ALIGNED`, and still checks the zip's checksum against the original bytes. If that test ever fails anyway, run `python tools/align_actions.py` (it makes every workflow file agree; `--check` only reports) and then the tests. The first Dependabot checkout bump failed because one of my tests named a literal version, and the first grouped artifacts bump failed because I had turned the real v3-to-v4 break into a rule that the two must have equal majors; both were fixed by testing the actual rule.

## Watching for silent failures

Every other alert fires when a job FAILS. These cover what stops without failing: a schedule that GitHub paused, a dataset that stopped updating, a database that fills up, a site that breaks while every job stays green, a token that quietly expires. Each is optional and does nothing, harmlessly, until you set what it needs.

**The live site check** (`.github/workflows/smoke-test.yml`, `tools/smoke_test.py`) opens every page of the live site in a real browser, with no sign-in and no secrets. It wakes a sleeping app, then checks that each page shows the text it always shows (the footer, and for Fighters, Track record, Insights and Game a heading that only appears when the page's data loaded; the Track record one includes "How much evidence is there?", the section the page deliberately hides if it fails), that no page shows "Couldn't load data", a traceback, a Streamlit error box or the host's error page, and that the header's "Results through" date is not more than 21 days old. A failure is retried once after a minute (a site that is only restarting is not an outage), then it posts to the private alert channel, and a screenshot of the failing page is kept as the run's `smoke-failure` artifact for a week. It runs daily at 15:20 UTC, on demand (Actions, Live site check, Run workflow), and after every push to `main` once Tests has passed (it waits four minutes for the host to redeploy). Set the repository variable `SITE_URL` only if the address ever changes. It checks that pages WORK, not that their numbers are right. The list of pages is `PAGES` in `tools/smoke_test.py`, and a test fails until it matches the real navigation. To try it by hand: `pip install -r tools/smoke-requirements.txt`, `python -m playwright install chromium`, then `python tools/smoke_test.py https://ufc-predictor.streamlit.app/ --out smoke_out`.

**Why the browser job runs on `ubuntu-24.04` and not `ubuntu-latest`:** the `ubuntu-latest` label moved to Ubuntu 26.04, which the pinned Playwright (1.56.0) cannot install a browser on ("Playwright does not support chromium on ubuntu26.04-x64"), and a monitor that cannot start is no monitor. Playwright 1.56.0 names Ubuntu up to 24.04 and the workflow had passed on it; a test insists on an explicit runner and on a Playwright and runner pair that has been confirmed. To move to a newer Ubuntu, change the runner and the Playwright version together (a release that names that Ubuntu: 1.64.1 does name 26.04, but I could not test it here), run the workflow by hand, then add the pair to the test. The alert distinguishes the two kinds of failure: **Live site check** means the site check ran and failed, and **Live site check (browser setup, not the site)** means the check could not start, which says nothing about your site. **When the live site check fails.** Do these in order. (1) Open the site in a browser and look: if it loads normally now, the failure was probably the host restarting after a push, a sleeping app that took long to wake, or a one-off; the next run will say. (2) Open the failed run in the Actions tab: the summary now quotes what the page showed instead of the site (for example a host's loading page, a sleep notice, a challenge page, or nothing), and a screenshot is saved at the bottom of the run under **Artifacts**, `smoke-failure`: download it and look at `failure.png`. (3) If the site is really broken, the screenshot and the quoted text say where; send them to me. The tool waits two minutes for a site to appear (a restarting host needs minutes) and retries once after 90 seconds before it counts as a failure, so a failure after that is rarely just a blip. A failure right after a push is the first thing to suspect, because the host redeploys on every push.

**The watchdog** (`pipeline/watchdog.py`, a step of the daily job after the health check; `python pipeline/auto_update.py watchdog` runs it alone) reports in the run summary and, like any other problem, fails the run and alerts when: an event finished at least five days ago and its locked picks are still not graded (the results come from one Kaggle dataset, so this is how you learn it stopped updating; cancelled cards are not counted), or the database is at 80% of its size limit. Every run prints the database size and the three biggest tables. It also lists any pick that ended with no result in the last 14 days (the event and the fighters): a draw, a no contest or a fight that fell through is the usual reason, so one or two are only reported, and you only need to check the real result if a fight is named that you know had a winner (then its names were not matched, and the fix is the usual alias line). **Three or more in 14 days fail the run,** because a draw or a no contest is rare and that many points at results not being matched to picks. Fights with no winner are left out of the training data and the backtest, and a pick on one is marked `no_result`: counted neither right nor wrong, and not flagged as waiting. The README bullet that states this rule was added once with `python tools/add_readme_bullet.py`, which does nothing if the bullet is already on a line of its own, **repairs it if it ended up glued to another line or there twice**, and **refuses a README that has lost its hidden number markers** (what happens if a copy from a web page or a document editor is pasted over the file; `git restore README.md` brings the real one back). Never paste text over the whole README: its `<!--n:...-->` markers are how the Monday job refreshes the live record. The limit defaults to 500 MB; look up your plan's real storage cap on the host's dashboard and, if it differs, set the repository variable `DB_SIZE_LIMIT_MB` (Settings, Secrets and variables, Actions, Variables).

**The heartbeat** (`pipeline/heartbeat.py`) tells an outside dead-man's-switch service that a job ran, so a job that never ran at all is noticed. Setup, once: make a free account at a service such as healthchecks.io; make one check with a daily period and a grace of a few hours, copy its ping address, and save it as the repository secret `HEARTBEAT_URL`; make a second check with a weekly period and a grace of a day, and save its address as `BACKUP_HEARTBEAT_URL`; set the service to email you. The daily job pings on success and sends `/fail` on failure (the convention of healthchecks.io; a service without it simply ignores the extra ping), so you get an email from outside GitHub even if GitHub itself is the problem. The address is a secret: it must be https and is never printed. A dry run does not ping. If the secrets are not set the steps say so and do nothing.

**The backup token's expiry** (`pipeline/token_expiry.py`, a step of the weekly backup that never fails it) makes one read-only request with the backup token and reads the expiry date that GitHub reports in the `github-authentication-token-expiration` header. It says in the run summary how long is left, and at 21 days or fewer (or if GitHub refuses the token) it posts to the alert channel and prints a warning. I could not confirm from outside whether GitHub reports that header for your kind of token, so the first run's summary tells you: a date, or "did not report an expiry date" (then the calendar reminder is still the only warning).

Each of these has its own tests (`tests/test_smoke_checks.py`, `tests/test_watchdog.py`, `tests/test_heartbeat.py`, `tests/test_token_expiry.py`, `tests/test_operations_workflows.py`); the smoke test was also run against the real demo site, passing, and against a demo with a page deliberately broken, which it caught.

## One-command recovery

If the real database were ever lost, `python pipeline/recover.py BACKUP_DATA_FOLDER --confirm NAME` rebuilds it from the backup into a **new, empty** database and leaves it ready, replacing the manual list under Backups. `BACKUP_DATA_FOLDER` is the `data` folder of your private `ufc-project-backups` repository (clone or download it first). It reads the new database's owner connection string from `RECOVER_DATABASE_URL` and **never reads DATABASE_URL**, so the old database (or one you forgot was still set) cannot be touched by mistake. It refuses any database that is not empty, so it can never overwrite anything, and `--confirm` must repeat the new database's own name (the command says what to type if you leave it out). Nothing it prints contains a password or a connection string, and an unexpected error is reported without a traceback (a failed `CREATE ROLE` statement contains the new password).

**Practise it first:** `python pipeline/recover.py BACKUP_DATA_FOLDER --rehearse` does the same on a throwaway Postgres on your own computer (`pip install -r demo/requirements.txt` once), keeps no files and ends with `RECOVERY OK (a rehearsal: nothing was kept)`. It is the same code path as the real run. The weekly `restore_drill.py` checks the restore alone; this also covers the logins and the cache.

What it does, in order: (1) checks every backup file against `MANIFEST.csv`, and a damaged backup stops everything before anything is created; (2) creates every table and restores each one through the drill's tested recipe, comparing every table with the backup; (3) moves every **auto-number counter** past the restored ids and checks that it did; (4) creates the website's read-only login and the game's login from nothing with new passwords, writing each connection string to a file (`data/site_database_url.txt` and `data/game_database_url.txt`, which git ignores) and never to the screen; (5) audits both logins (`db_roles.audit_reader` and `game_admin.audit`); (6) rebuilds the "Why this pick?" cache, which is not backed up (`--skip-reasons` leaves it for later and prints the two commands; a failure there never fails the recovery). It stops at the first problem and says where: a stop before step 4 created no login and changed nothing but the new database.

**Two protections against the mistakes a stressful day invites.** *A login that may still be in use:* Logins belong to the server, not to one database. Recovering into a new database on a server that already has the website's or the game's login would give those logins NEW passwords and cut off whatever still uses the old ones (the live site, if the same server hosts it). So the real run (and a rehearsal on your own server; the built-in throwaway one is always fresh) stops, before creating anything, if either login already exists on the server, and says which. The fix is a brand-new database server or project; `--replace-logins` overrides it, and is only for when the old database is gone and nothing uses those logins any more. *The wrong folder:* an extracted zip is usually nested one level deeper than you expect. If the folder you give it has no `MANIFEST.csv`, it looks a few levels inside and says where it found one ("Did you mean that folder?", with the command to run), explains a zip file or any other file, and never guesses on your behalf. Neither check starts a database or changes anything.

**When it says RECOVERY OK, change these secrets** (the report lists them again, as names and places, never values): in GitHub (Settings, Secrets and variables, Actions) `DATABASE_URL` = the new owner string; in Streamlit (your app, Settings, Secrets) `DATABASE_URL` = the line in `data/site_database_url.txt` and `GAME_DATABASE_URL` = the line in `data/game_database_url.txt`; `GAME_INVITE_CODE` stays as it is, because the players came back with the backup. Delete both files afterwards. Then run **Actions, Auto update, Run workflow** once and open every tab of the site. A backup runs weekly, so anything created after it (the newest odds snapshots, locked picks and game picks) is not in it; the public pick log still has every locked pick.

**An auto-number counter bug this found.** The odds history (`odds_snapshots`) numbers its rows with an auto-number counter. A restore loads rows with their old ids but a counter starts again at 1, so the first new snapshot would have failed with a duplicate key, and the drill could not see it because it compared rows and never inserted one. `backup.restore_table` now calls `fix_sequences` after loading each table, `check_sequences` is part of the drill (a restore that leaves a counter behind is a failure), and `tests/test_restore_sequences.py` shows the failure, the fix and that the drill catches it.

`tests/test_recover.py` runs a complete recovery in CI: a demo is backed up and recovered into an empty database owned by a limited login, the counters carry on, both logins are audited and behave (the site's cannot read the odds, the game's cannot read anything else), no secret reaches the output, and the real website opens against the recovered copy. It also covers the refusals (a non-empty target, a missing or wrong `--confirm`, a missing variable, `DATABASE_URL` alone, an unreachable database), a damaged backup, a failed audit, an unexpected error and a failed cache rebuild.

## Goes the distance

The model estimates the chance a fight ends in a decision from how long each fighter's earlier fights lasted (share of scheduled time used, decision rate, how often they won or lost by a finish) plus title or five rounds. Fights since 2002 with 3 or 5 scheduled rounds are used; disqualifications are left out. Scheduled rounds are not on the cards, so a title fight or main event is taken to be five rounds. Predictions are locked when a bout is announced (with the winner picks), never change, and are graded when the dataset has the result. The daily job runs `predict` in the v2 step and `grade` plus `backtest` with the results. `python pipeline/export_history.py` saves the history the models read to `data/history_export.csv` (read-only; not committed).

The market-informed version (`model_distance_v2.py`) keeps this model's prediction and adds the favourite's strength from the betting line (the favourite's chance above 50%, and its square) in a small second stage fitted on earlier fights only. It was added on a narrow pass of the rule in `experiment_distance_market.py`, so it runs **alongside** the model-only prediction and never replaces it: the website shows the model-only figure as the headline and the market-informed one beside it, and both are scored on the same fights. The daily job locks it in the same step as the market-informed winner picks (`step_lock`: within the lock window, using the latest line, once, never changed), grades it with the results, and extends its backtest after the Monday odds refresh. The friends' game still uses the model-only prediction. The live record will take years to separate it from the model-only prediction (the README gives the number of fights), so both keep running under a rule fixed in advance: **retire v2 only if, over at least 300 graded live fights, it is clearly worse than v1** (the 95% range of the log-loss difference lies entirely on v1's side). `python pipeline/model_distance_v2.py scoring` prints where that stands. To retire it, stop calling it in `step_lock` and leave the tables in place: nothing else depends on it.

## Why this distance prediction?

Each upcoming bout's goes-the-distance prediction has a panel on the Upcoming page, beside the distance line, that says what moved the prediction away from an average fight. It reuses the winner panel's look and its lessons.

**The arithmetic is exact.** The model-only prediction is a logistic regression on ten standardised inputs, so each input adds `coefficient x standardised value` to the log-odds, the intercept is the log-odds of an average fight, and `intercept + the contributions = the model's log-odds` (`pipeline/distance_reasons.py`; a test checks this on the real fitted model to 1e-12). The ten inputs are grouped into six readable factors: going to a decision, winning by a finish, being finished, how long fights last, the scheduled rounds and a title fight. The panel shows an average fight, this fight and (where there is one) the market-informed figure, then a bar per factor: a bar from the left means "goes the distance", from the right "ends early", longer meaning a bigger push, with each fighter's own counts beside it. A group can mix inputs that pull in different directions, so a bar can oppose the headline number next to it (the same trap the winner panel fell into once; the README's lessons list it). Every factor carries a `net` flag, set when its bar points against what its headline number suggests, and the page then says "net effect" beside it instead of contradicting itself.

**The market-informed prediction is told as one comparison,** not as factors, because its second stage does not split into tidy ones: what the model-only figure said, what the line makes of it, and how much of the change comes from the favourite's strength (the same fight with a typical favourite gives a second figure; the rest of the change is the second stage re-weighting the model-only number). The line's strength is shown as the favourite's chance with the margin removed, like the winner panel's market figure; the site still never shows odds.

**An explanation exists only if it reproduces the locked number.** `explain_missing` (in `model_distance.py` and `model_distance_v2.py`) rebuilds each locked, not yet graded prediction's model exactly as it was (it learned from the first `trained_on_fights` fights in date order, and a fight's inputs never use a later fight), checks that the rebuilt model gives the locked probability to a millionth, and only then stores the breakdown in `pick_reasons` under the versions `d1` (model-only) and `d2` (with the line), next to the winner picks' `v2` and `v3`. If the history was revised since, so the number no longer reproduces, nothing is stored for that bout and the log says so. The page checks again: a breakdown that differs from the number on the card by more than half a point is hidden, and a damaged betting-line comparison is left out on its own.

**It runs by itself.** Every `predict` (in the daily job's lock steps) ends by storing the breakdowns that are missing, so new bouts get theirs at lock time and any bout locked before this feature existed is filled in on the next run. `python pipeline/model_distance.py explain-upcoming` and `python pipeline/model_distance_v2.py explain-upcoming` do the same by hand and say how many were stored and skipped. A failure here is logged as a warning and never fails the run: the predictions stand. Only open bouts are explained; once graded they leave the Upcoming page.

`tests/test_distance_reasons.py` (the maths on hand-built models, and the pipeline on a real Postgres: storage, exact rebuilds, refusal of numbers that do not reproduce, failure isolation), `tests/test_distance_why_ui.py` (the panel) and three end-to-end tests in `tests/test_demo.py` cover it.

## Model health and the model cards

`docs/MODEL_CARDS.md` has one card per model (what it predicts, its inputs and method, how it is tested, where it is weak) and the **retirement rules, fixed before the live results could tempt anyone to bend them**. `pipeline/model_health.py` applies them, and the daily job runs it as the `health` step (after the results are graded, before the README numbers and the Discord digest). It only reads: two checks on the LOCKED, graded predictions of each of the four models.

1. **Calibration.** Each model's own probabilities add up to how many it expects to get right (or how many fights it expects to go the distance), with a known spread. With at least 100 graded predictions, a record more than 2 standard deviations below that is reported as "worse than it said", and **more than 3 is listed under "Needs your attention"** (a stricter bar for the alarm, because this runs every day and a check that cried wolf at 2 would be ignored).
2. **The retirement rules.** Over at least 300 graded predictions: v2 is retired if clearly worse than a coin flip, v3 if clearly worse than the bookmakers' opening line alone, distance v1 if clearly worse than the plain decision rate of the previous three years, distance v2 if clearly worse than v1 (that rule lives in `model_distance_v2.py`). "Clearly worse" means the whole 95% range of the per-fight log-loss difference is on the wrong side of zero. The rules are for retiring a model that is doing harm, not tests that it is good: for gains this small the live record would need years to show that.

Run it yourself with `python pipeline/model_health.py` (read-only connection string is enough; it exits 1 if something needs a person) or `python pipeline/auto_update.py health --dry-run`. A table that does not exist yet gives "no graded locked predictions yet", never an error. **Changing a rule means changing `docs/MODEL_CARDS.md` and `pipeline/model_health.py` in the same commit:** `tests/test_model_cards.py` fails if they disagree, and also checks the cards' claims about the code (the 300-fight warm-up, the no-intercept regressions, the 2002 start) against the code itself.

## The browser-tab icon

The small icon in the browser tab is `app/assets/favicon.png`: the letters UFC in the site's own typeface (Barlow Condensed) on its own ink, with its red accent bar, so it matches the page and is not an emoji. `app/streamlit_app.py` passes that file to `st.set_page_config(page_icon=...)`; if the file is ever missing the site simply falls back to the host's default icon. To change it, replace the file with a square PNG (256 pixels is plenty) and push. Keep it saved with an alpha (transparency) channel even if it is fully opaque: Streamlit turns an image without one into a softer JPEG before serving it. Browsers cache tab icons hard, so after a change look at the site in a private window or press Ctrl+F5. `tests/test_favicon.py` checks that the file exists, is a small square PNG, uses the site's colours, and that no emoji icon has crept back in. A two-letter FP mark is also a good fit if you prefer something crisper at 16 pixels.

## The About section

Under the navigation on every page there is a collapsed row, **About this project** (`app/about_ui.py`), for a first-time visitor who opened the live site before the repository: what it is, how to read the record honestly, what it will not do, what it is built with, and four links (the repository, the write-up, the model cards, the demo guide). Its text is deliberately free of numbers that go stale and says only what the site says elsewhere. `tests/test_about_ui.py` checks each claim against the app's own text and each link against the files in the repository, so changing the site's wording or renaming a document makes the test fail until the About text is updated, which is the point. The demo says it is the demo. To change the text, edit the constants at the top of `about_ui.py`.

## The evidence meter

The Track record page ends its live-record part with **How much evidence is there?**, one card for each claim the project makes: the model-only winner pick against a coin flip, the market-informed winner pick against the bookmakers' line alone, the goes-the-distance prediction against the plain decision rate of the previous three years, and the market-informed distance prediction against the model-only one. These are the comparisons the retirement rules in the model cards make. Each card shows the **live estimate** (the paired log-loss difference on graded locked predictions, with its 95% range), a **dashed line where the backtest landed**, a **progress bar** toward the number of graded fights needed, and one or two sentences built from the numbers.

"Reasonably sure" is the README's own convention: an 80% chance of seeing an effect of the backtest's size at the usual 5% level, which needs ((1.96 + 0.84) x sd / effect) squared graded fights, where sd is how much the per-fight difference varied in the backtest. `tests/test_evidence.py` computes the figure both ways on the same data and fails if the meter and the README ever disagree. A claim whose backtest found no gain shows no bar, because there is nothing to wait for. The maths is in `pipeline/evidence.py` (numpy and pandas only), the drawing in `app/evidence_ui.py` (pure HTML), and `evidence_data()` in `app/streamlit_app.py` loads the backtest frames and the live frames (the same queries as `model_health.py`) through the site's read-only login and caches them for 15 minutes. A test checks that the page reads no private table, and the section sits inside a guard so a failure in it can never take the Track record page down. To add a claim, add an entry to `CLAIMS` with the columns for its backtest and live frames, and a test.

## The Insights page

The site's **Insights** tab shows seven findings from `fights`, `fighter_fights` and `fight_picks_v3`: how fights end, which divisions go the distance, the trend by year, favourites and upsets, experience, age and reach. The maths is in `pipeline/insights.py` (numpy and pandas only, so a test can check every number against a hand-worked answer), the drawing is in `app/insights_ui.py` (pure HTML, no Streamlit), and `page_insights()` in `app/streamlit_app.py` loads the three tables through the site's read-only login and caches the result for an hour. It uses no private table and shows no odds: the market's price appears only as the average of a whole group of fights.

- **Same definitions as the distance model:** fights since 2002 with 3 or 5 scheduled rounds, disqualifications left out, "went the distance" means the result starts with "Decision". A test compares them with `model_distance.py`, so the page cannot disagree with the Track record.
- **Every sentence is built from the numbers,** never typed in. A group of fewer than 30 fights is shown but never quoted, and a sentence is simply left out (the page says "Not enough fights yet to say anything here") when there is too little to say. Wording such as "less often than not" is used only when the 95% range excludes 50%; otherwise it says "about even".
- **Experience counts a fighter's earlier UFC fights in the data,** so it uses ALL fights (including those before 2002) to count them, but only looks at fights from 2005 on, where the counts are not cut short by the start of the data.
- **Read it for what it says now, not only overall.** The year-by-year trend reports the whole span AND the last ten years alone with their lowest and highest year, because a change that happened long ago (the share of fights going the distance rose a lot in the 2000s and has been flat since) says nothing about today. When the real page first went live, the whole-span sentence alone said "the share rose", which was true and misleading.
- **Say what overlaps.** Fighters with many more UFC fights are mostly the older ones, so the experience finding also measures, from the data, how often the more experienced fighter was also the older one, and says so when it is most of the time, instead of leaving the reader to take two overlapping patterns for two effects.
- **How the market's price is made.** `model_v3.devig` divides the bookmaker's margin out evenly across both fighters, a simple method that makes heavy favourites look a little weaker than they are. So a favourite winning a few points more often than the price is expected from the method and does not by itself mean the line was wrong; the page says so, and a test checks the note against `devig`, so changing the method means changing the note.
- **Reach is in whole inches.** Every value in the real `reach_cm` column is a multiple of 2.54 (checked on the real database: 177.80, 180.34, 182.88 and so on), so the page buckets reach gaps in inches (1, 2 to 3, 4 to 5, 6 or more) and leaves out a gap under half an inch as a tie. The demo's invented reach is whole inches too, so it behaves like the real column.
- **To add a finding:** write the function in `insights.py` with a hand-checked test, add its sentence builder, add it to `compute_all`, and give it a section in `insights_ui.sections`. Look at the page in a browser as well as running the tests: a label that overwrote another one was caught only by looking.

## README numbers

Live numbers in `README.md` sit between markers: `<!--n:name-->65.4%<!--/n-->` for one number, and `<!--auto:name-->` ... `<!--/auto:name-->` around a table. `readme_numbers.py update` recomputes them from the database; `check` says what is stale. **Edit the prose freely, but never between the markers**, because the next update overwrites it. Numbers from one-off experiments (the leakage story, model comparisons) are plain text and are never touched. The generator also guards the claims the prose makes ("the blend beats the line", "the probabilities track the stated confidence", ...): if the data stops supporting one, `check` says `CLAIM NO LONGER HOLDS` and the daily report asks for attention. The GitHub job refreshes the file on Mondays and commits it only if something changed. To add a new generated number, add its name to `known_names()` and `compute()` in `readme_numbers.py`, then put the markers in the README.

The "Live record so far" row in the "At a glance" table is one such number: it counts every locked, graded pick across all cards (cancelled bouts, draws and games not yet played are not counted), with a 95% range and what the model's own stated confidences predicted, and it sits inside a table cell, so it uses the single-line `n` marker, never the `auto` block (a block is laid out on its own lines and would break the row). When that record reaches 300 graded picks, `readme_numbers.py update` reports that the README's "far too few to conclude anything" wording no longer holds. The two distance figures in the README ("live fights needed", "years") use the same convention as the winner model's: 80% power at the 5% level.

## Discord digest

Create a webhook for a channel (channel settings, Integrations, Webhooks), store its address as the repository secret `DISCORD_WEBHOOK_URL`, and check it with `python pipeline/auto_update.py discord-test`. The address is a secret: anyone holding it can post to the channel. If it leaks, delete the webhook in Discord and make a new one. The job posts picks for cards within 14 days and each finished event's results, each exactly once (tracked in the `digest_sent` table); a post that fails is retried on the next run.

## Fixing names the automation will not guess
When the daily report lists a held name, add one line to `upcoming/aliases.csv` as `Card Name,Dataset Name` (use `NEW` for a fighter with no UFC history) and push. The job reads the file from the repository.

## Project structure

The folder map is in the README, under **Where things are**, and a test fails if a folder is added without being listed there. What each module in `pipeline/` does is in the table at the top of this runbook, one row per feature.

