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
| **Automation** | `pipeline/auto_update.py`, `.github/workflows/auto-update.yml` | Daily job: cards, lines, picks, results, odds history |
| **Goes the distance** | `pipeline/model_distance.py`, `pipeline/distance_scores.py`, `app/distance_ui.py` | The chance each fight goes to a decision: backtest, locked predictions for upcoming bouts, grading, and its display |
| **README numbers** | `pipeline/readme_numbers.py` | Keeps every live number and table in `README.md` equal to the database |
| **Pick log** | `pipeline/pick_log.py`, `picks_log/` | Public, time-stamped record of every locked pick and goes-the-distance prediction (two files), committed by the daily job |
| **Discord digest** | `pipeline/digest.py` | Posts newly locked picks and event results to a Discord channel, once each |
| Web app | `app/streamlit_app.py` | Read-only Streamlit app |
| Tests | `tests/test_auto_update.py` | Offline tests that run before every scheduled job |

## Run it yourself

All database commands below need a PostgreSQL database (a free hosted one such as Neon works). Use two connection strings: an **owner** string for anything that writes (everything except the web app) and a **read-only** string for the web app (`sql/readonly_role.sql`). Never commit a connection string.

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

## The pick log, and pushing code afterwards

After each real run the GitHub job commits `picks_log/locked_picks.csv` (only that folder). That means the repository on GitHub moves ahead of your computer, so **pull before you push**:

```
git pull --rebase
git push
```

Only the GitHub job writes the log (your own runs skip it, so the two never collide); `pick-log --write-pick-log` forces it. To make the history harder to rewrite, protect `main` against force pushes and deletions (Settings, Rules, New branch ruleset) and leave "Require a pull request" **off**, or the job's own push is blocked.

## Goes the distance

The model estimates the chance a fight ends in a decision from how long each fighter's earlier fights lasted (share of scheduled time used, decision rate, how often they won or lost by a finish) plus title or five rounds. Fights since 2002 with 3 or 5 scheduled rounds are used; disqualifications are left out. Scheduled rounds are not on the cards, so a title fight or main event is taken to be five rounds. Predictions are locked when a bout is announced (with the winner picks), never change, and are graded when the dataset has the result. The daily job runs `predict` in the v2 step and `grade` plus `backtest` with the results. `python pipeline/export_history.py` saves the history the models read to `data/history_export.csv` (read-only; not committed).

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
sql/readonly_role.sql        read-only database user for the web app
pipeline/                    predict.py, update_from_csv.py, upcoming.py, model_v2.py, model_v3.py, auto_update.py, pick_log.py, digest.py, requirements.txt
app/                         streamlit_app.py and its requirements
.streamlit/config.toml       the web app's theme
tests/                       offline tests
.github/workflows/           auto-update.yml (the schedule)
upcoming/                    an example card and aliases.csv (card name -> dataset name)
picks_log/                   the public pick log (written by the GitHub job) and an explainer
docs/                        this runbook, the Power BI guide, and screenshots
```
