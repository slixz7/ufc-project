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
   ```
   Exit code 0 = fine, 1 = a step failed, 3 = something needs your attention (listed at the top of the report).
7. **Schedule it on GitHub.** Add the repository secrets `DATABASE_URL` (owner string), `ODDS_API_KEY` and `KAGGLE_API_TOKEN`, run the "Auto update" workflow once by hand (it starts as a dry run), then set the repository variable `AUTO_ENABLED` to `true`. It runs daily at 14:17 UTC and emails you when something needs attention. Note that GitHub pauses scheduled jobs in a public repo after 60 days without repository activity.
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

## Fixing names the automation will not guess
When the daily report lists a held name, add one line to `upcoming/aliases.csv` as `Card Name,Dataset Name` (use `NEW` for a fighter with no UFC history) and push. The job reads the file from the repository.

## Project structure

```
01_load_and_clean.ipynb ... 04_postgres_sql.ipynb   the original analysis
sql/features.sql             feature views (window functions)
sql/predictions.sql          prediction table and dashboard views
sql/model_v2.sql, model_v3.sql   tables and views for the two model versions
sql/readonly_role.sql        read-only database user for the web app
pipeline/                    predict.py, update_from_csv.py, upcoming.py, model_v2.py, model_v3.py, auto_update.py, requirements.txt
app/                         streamlit_app.py and its requirements
.streamlit/config.toml       the web app's theme
tests/                       offline tests
.github/workflows/           auto-update.yml (the schedule)
upcoming/                    an example card and aliases.csv (card name -> dataset name)
docs/                        this runbook, the Power BI guide, and screenshots
```
