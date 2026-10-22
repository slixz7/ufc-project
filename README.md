# UFC Fight Predictor

Predicts UFC fight winners from each fighter's record, physical attributes, fighting style and recent form, then grades itself against the real results. Built end to end: **Python → PostgreSQL/SQL → logistic regression → Power BI**.

> Not affiliated with the UFC. Model predictions for fun and learning, not betting advice.

![Scoreboard](docs/images/scoreboard.png)
![Fight explorer](docs/images/fight-explorer.png)

## Headline results

Evaluated only on fights where **both fighters had at least 3 earlier UFC fights** (about 45% of all fights), because features are unreliable for fighters with little history. Every prediction uses only data from before the event.

| | Accuracy |
|---|---|
| Coin flip | 50.0% |
| Pick the younger fighter | 59.9% |
| Pick the fighter with the better career win rate | 61.8% |
| **Logistic regression, test set Jan 2025 to Oct 2026 (516 fights)** | **67.2%** (about 63% to 71% interval) |
| **Logistic regression, walk-forward 2016 to 2026 (2,740 fights)** | **62.8%** (single years range from about 57% to 69%) |

**How to read this honestly**
- The recent test window was a good stretch. The long-run figure (about 63%) is the more representative one.
- The model beats the simple "better record" rule by about 5 points on the test set, but the intervals overlap, so the edge is suggestive rather than proven.
- Confidence carries information: in the live scoreboard, picks the model was at least 65% confident about were right about 74-75% of the time, versus roughly 60-64% for lower-confidence picks (a step up, not a smooth climb).
- Fight outcomes are noisy. Published models for this problem typically land in the low-to-mid 60s.

## What the dashboard shows

- **Scoreboard:** running accuracy against a coin flip, accuracy by model confidence, last-event accuracy.
- **Fight explorer:** every scored fight with the model's pick, its confidence and whether it was right, filterable by weight class and confidence.

The Power BI file is not included (it contains a database address). `docs/powerbi_dashboard.md` explains how to rebuild it.

## How it works

1. **Clean** (`01_load_and_clean`): load the Kaggle dataset, drop fights with no winner, and randomise which fighter is "A" so the label isn't trivially "fighter 1".
2. **Features** (`02_features`): rebuild each fighter's stats from their *earlier* fights only (rolling win rates, streaks, days since last fight, age, striking and grappling rates, style), then take the A-minus-B differences.
3. **Model** (`03_model`): logistic regression with no intercept (swapping A and B should flip the prediction), tuned on a validation block, tested on later fights, compared against simple baselines and checked with a walk-forward evaluation.
4. **Database** (`04_postgres_sql`): load the data into Postgres and rebuild the same features as **SQL window functions** (`sql/features.sql`), verified to match the pandas version exactly.
5. **Scoring** (`pipeline/predict.py`): for each new fight, train on all earlier fights, predict, and log whether the pick was right (`sql/predictions.sql` defines the table and views Power BI reads).
6. **Updating** (`pipeline/update_from_csv.py`): append new fights from the latest copy of the dataset.
6b. **Model v2** (`pipeline/model_v2.py`): the current model. It is trained on every earlier fight (not just those where both fighters had 3+ UFC fights), pulls thin small-sample stats toward the average, adds an Elo rating and the level of opponents faced, and adds age and layoff curves. It gives a pick for **every** fight, past and upcoming, tagged `full`, `limited` (a fighter has 1-2 UFC fights) or `debut`. In walk-forward testing (each year predicted from earlier years only) it scored about 61.4% over 2014-2026 against 60.3% for v1, with better-calibrated probabilities.
7. **Upcoming cards** (`pipeline/upcoming.py`): load an announced card from a CSV, compute each fighter's features as of fight night with the same SQL, make the prediction **before the event**, lock it with a timestamp, and grade it once the real result arrives. Late card changes (an opponent swap, a cancelled bout) are handled without touching predictions already made.
6c. **Model v3** (`pipeline/model_v3.py`): model v2 plus the betting market. A small stacker combines v2's probability with the bookmaker's probability (margin removed), trained only on earlier fights. On 5,279 fights from 2016 to 2026 (each pick made using only earlier fights): v2 alone 61.8%, the opening line alone 64.9%, v2 + the opening line **65.4%**. The gain is largest for debutants (59% to 67%). Official point-in-time rankings were tested and added nothing. The line is used only as a forecasting input: this is not betting advice and the app never shows odds. Picks are two-stage: the v2 pick is locked when a card is announced, and the v3 pick is locked once a line is loaded. Neither changes.
8. **Web app** (`app/streamlit_app.py`): a shareable Streamlit app with an upcoming-card page (face-offs, locked picks, tale of the tape), past events, fighter profiles and comparison, and the track record. It only reads from the database, through a read-only user (`sql/readonly_role.sql`).

## Problems I found and fixed

- **Career stats in the dataset are snapshots from scrape time, not from the day of the fight.** A veteran's career stats were identical in 2008 and 2026 rows, so using them would leak the future into the model. I rebuilt every rolling stat from earlier fights only and wrote a brute-force check that recomputes features the slow way for 300 random rows.
- **The first fighter listed won every fight in the raw data.** A model would have learned "pick fighter 1" and scored a fake 100%.
- **Early UFC events were one-night tournaments,** with the same fighter appearing up to three times on one date and no fight order. Those rows are kept as history but not used as training rows.
- **A "reach unknown" flag looked significant but leaks information,** since a recorded reach depends partly on how long a fighter lasted in the UFC. Removing it didn't hurt accuracy, so it's out.
- **ufcstats.com blocks plain HTTP scraping** behind a JavaScript check, so live scraping wasn't possible. Updates come from a maintained public dataset instead.

## Limitations

- The scoreboard on the dashboard is still an *after-the-fact* backtest (every prediction uses only earlier data, but is generated after the result). The pre-fight pipeline (`upcoming.py`) builds a genuinely locked record from here on; cards are entered by hand because ufcstats.com can't be scraped.
- Fighter names differ between sources (for example "Cameron" vs "Cam"), so new cards need a small name-matching step. It stops and shows suggestions rather than guessing.
- History is UFC-only: a fighter's career before the UFC isn't in the data.
- Debuts and fighters with 1-2 UFC fights get picks, but they are less reliable (about 59-61% right in testing vs 62% for fighters with 3+ fights). For debutants who are not in the dataset yet, the model does not know their age or size.
- The dataset has no betting odds, so there is no comparison against the market.
- Small per-event samples: accuracy for a single card is very noisy.

## Run it yourself

1. Download the dataset from Kaggle (`scarekrow/ufc-data`) and put the fights CSV in `data/` (it is not included here; check its license on Kaggle).
2. Install:
   ```
   pip install -r requirements.txt
   pip install statsmodels jupyter matplotlib
   ```
3. Run notebooks `01`, `02` and `03` in order. They need no database.
4. Create a PostgreSQL database (a free hosted one such as Neon works) and set the connection string. In PowerShell:
   ```
   $env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
   ```
   Then run notebook `04`, and:
   ```
   python pipeline/predict.py
   ```
5. After each new card, download the updated dataset into `data/new/` and run:
   ```
   python pipeline/update_from_csv.py --csv data/new --dry-run
   python pipeline/update_from_csv.py --csv data/new
   python pipeline/predict.py
   ```

6. **Predict an announced card before it happens** (one-time `python pipeline/upcoming.py setup` first):
   ```
   python pipeline/upcoming.py load --csv upcoming/ufc-333.csv --dry-run
   python pipeline/upcoming.py load --csv upcoming/ufc-333.csv
   python pipeline/upcoming.py predict
   ```
   After the event, once the dataset includes it: run `update_from_csv.py` as above, then `python pipeline/upcoming.py grade`.
   Names that don't match your data are listed with suggestions; add a line to `upcoming/aliases.csv` (use `NEW` for a fighter with no UFC history).

7. **Run the web app locally** (with a read-only `DATABASE_URL` set, see `sql/readonly_role.sql`):
   ```
   pip install -r app/requirements.txt
   streamlit run app/streamlit_app.py
   ```

8. **Model v2** (use the owner database string; these write to the database):
   ```
   python pipeline/model_v2.py setup
   python pipeline/model_v2.py backtest        # a pick for every past fight (a few minutes the first time)
   python pipeline/model_v2.py predict         # picks for loaded upcoming cards, locked before the event
   python pipeline/model_v2.py grade           # after the dataset includes the event
   ```

9. **Model v3** (owner database string). Download the historical odds once (it is not stored in this repo):
   ```
   git clone --depth 1 https://github.com/nlbair/ufc-data.git data/nlbair
   python pipeline/model_v3.py setup
   python pipeline/model_v3.py load-odds --csv data/nlbair/data/ufc_all_fights_wide.csv
   python pipeline/model_v3.py backtest
   ```
   For an announced card, once its lines are out, write a small file (American odds, names as in the card file):
   ```
   fighter_a,fighter_b,odds_a,odds_b
   Name One,Name Two,-150,+130
   ```
   then check it and save it, and lock the market-informed picks:
   ```
   python pipeline/model_v3.py odds --csv upcoming/odds-CARD.csv --dry-run
   python pipeline/model_v3.py odds --csv upcoming/odds-CARD.csv
   python pipeline/model_v3.py predict
   ```
   After the event (and after `model_v2.py grade`): `python pipeline/model_v3.py grade`.

Never commit a connection string. This repo's `.gitignore` excludes `data/`, `.env` and Power BI files.

## Project structure

```
01_load_and_clean.ipynb     clean the raw data
02_features.ipynb           leakage-free features
03_model.ipynb              model and evaluation
04_postgres_sql.ipynb       load into Postgres, rebuild features in SQL
sql/features.sql            feature views (window functions)
sql/predictions.sql         prediction table and dashboard views
pipeline/predict.py         score fights, log results
pipeline/update_from_csv.py add new fights from the dataset
pipeline/upcoming.py        predict announced cards before they happen, lock and grade them
pipeline/model_v2.py        model v2: a pick for every fight (backtest, upcoming, grading)
pipeline/model_v3.py        model v3: v2 plus the betting market
sql/model_v2.sql            tables and views for model v2
sql/model_v3.sql            tables and views for model v3
app/streamlit_app.py        the shareable web app
app/requirements.txt        packages the web app needs
.streamlit/config.toml       the web app's theme settings (paper and ink)
sql/readonly_role.sql       read-only database user for the web app
upcoming/ufc-333.csv        an example announced card
upcoming/aliases.csv        card name -> dataset name fixes
docs/powerbi_dashboard.md   how the dashboard is built
```

## Roadmap

- Publish the pre-fight predictions and track record on a shareable page.
- Deploy the web app, and add a head-to-head predictor for any two fighters.
- Automated updates.

## Data and credits

Fight data: the Kaggle dataset `scarekrow/ufc-data`, originally collected from ufcstats.com. It is independently collected and not endorsed by the UFC or ufcstats.com. Its ranking columns are snapshots at scrape time, so this project doesn't use them.

Betting lines (model v3): the UFC Data set by N. Bair, version 0.1.1, licensed CC BY 4.0 (https://github.com/nlbair/ufc-data). It was built from ufcstats.com, MMA Decisions, BestFightOdds, the Kaggle dataset `martj42/ufc-rankings` and the UFCalendar rankings API; those sources should be credited as well. Cite: Bair, N. (2026). *UFC Data: scorecards, round statistics, and betting odds, 2013 to 2026* (Version 0.1.1) [Data set]. Zenodo. https://doi.org/10.5281/zenodo.23458636. Changes made here: only the fight id, fighter ids and names, and the opening and closing win probabilities were used. This project is independent and is not affiliated with or endorsed by the UFC.

Two checks worth knowing about: the `ranking` column in the Kaggle file used for fights is a snapshot of today's rankings (it leaks future results, so it is not used), and its `odds` columns are empty. The Bair dataset's rankings are the official board from before each event, and were tested: they add nothing once the model has Elo and records.
