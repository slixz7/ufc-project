# Model cards

Four models make the predictions on the site. Each card says what the model predicts, what it is and is not for, what goes into it, how it is tested, where it is weak, and **when it would be retired**. The retirement rules are written here before the live results can tempt anyone to bend them, and `pipeline/model_health.py` applies them to the real record every day.

**Three things hold for every model.**

- **A prediction is locked before its event and never changed.** The public log in `picks_log/` records each one, so the live record cannot be flattered after the fact.
- **The backtest and the live record are different things.** The backtest predicts every past fight using only the fights before it, but it is generated after the results are known, and many choices were made while looking at the same history. Only the locked live record is clean, and it is still small.
- **The current numbers are not on this page.** They live in the README's results tables and on the site's Track record page, where they refresh themselves from the database. A number copied here would go stale.

None of these models is betting advice, and the site never shows betting odds.

## Winner model v2 (model-only)

*Code: `pipeline/model_v2.py`, `sql/features.sql`, `sql/model_v2.sql`.*

**What it predicts.** The chance that fighter A beats fighter B, for every UFC fight: past fights (the backtest) and upcoming bouts (locked when the card is announced).

**What it is for.** Showing a statistical view of a fight beside the reasons for it, and supplying the model half of the market-informed winner model. **What it is not for.** Betting decisions, or anything about a fighter's health, camp or style, none of which it knows.

**Inputs.** About 25 differences between the two fighters: age, win rate and streak, striking and takedown rates, reach, an Elo rating and the level of opponents faced, with age and layoff curves. Small samples are pulled toward the average in proportion to how few fights they rest on. Every input is computed from a fighter's *earlier* fights only (`sql/features.sql`).

**Method.** A logistic regression with no intercept on the scaled differences, so it has no built-in favourite side. For the first 300 fights of history it does not have enough to learn from, so it uses a simple rule (the higher Elo rating, then the younger fighter) and marks those picks `early_rule`. Every pick also carries a tier: **full** (both fighters have 3 or more UFC fights), **limited** (the less experienced has 1 or 2) or **debut** (a fighter has none).

**How it is tested.** Walk-forward: each backtest pick is made by a model trained only on the fights before it. It is compared with a coin flip and with the bookmakers' line, and scored on log loss and Brier score as well as on how often it picks the winner. Upcoming bouts are predicted and locked before the event, then graded.

**Known limits.** History is UFC-only, so a fighter's career before the UFC is not in the data, and a debutant who is not in the dataset has no age or size. Rankings were tested and added nothing. Injuries, camps and how two styles fit together are not inputs. The live record is short.

**Monitoring and retirement.** After at least 100 graded locked picks, its record is compared with what its own probabilities expected, and a record more than 3 standard deviations short is flagged for a person. **Retire it if, over at least 300 graded locked picks, its log loss is clearly worse than a coin flip's**: the 95% range of the per-fight difference lies entirely on the wrong side of zero.

## Winner model v3 (market-informed)

*Code: `pipeline/model_v3.py`, `sql/model_v3.sql`, with the live lines from `pipeline/auto_odds.py`.*

**What it predicts.** The same thing as v2, but it also uses what the betting market thinks.

**What it is for.** The pick shown on the site when a line exists. **What it is not for.** Betting advice, or finding bets: the market is a forecasting input here, and no odds are ever displayed.

**Inputs.** The v2 probability and the market's probability, each as log-odds. The market's probability is the bookmakers' line with the bookmaker's margin divided out evenly across both fighters (a simple method that makes heavy favourites look a little weaker than they are). In the backtest it is the opening line; for a live pick it is the consensus when the pick is locked, the median across bookmakers.

**Method.** A tiny stacker: a logistic regression with no intercept that combines those two log-odds, trained only on earlier fights.

**How it is tested.** Walk-forward, each year predicted only from the years before it, on the fights that have a line. The README's results table puts three predictors side by side on the same fights: the model alone, the line alone and the blend. **Read it honestly:** the market does most of the work, and the model adds a small amount on top. That small gain is why the live record cannot settle the question for years.

**Known limits.** It needs a line, so a bout without one gets no v3 pick. The historical lines come from a third-party dataset (credited in the README); the live ones from The Odds API. It knows nothing the market does not, apart from the model's own statistics.

**Monitoring and retirement.** The same calibration check as v2. **Retire it if, over at least 300 graded locked picks, its log loss is clearly worse than the bookmakers' opening line alone**: the 95% range of the paired difference lies entirely on the wrong side of zero, meaning the line alone would have done better.

## Goes the distance v1 (model-only)

*Code: `pipeline/model_distance.py`, `pipeline/distance_scores.py`, `pipeline/title_rules.py`.*

**What it predicts.** The chance that a fight ends in a decision rather than a finish, for fights since 2002 with 3 or 5 scheduled rounds, disqualifications left out.

**What it is for.** The "goes the distance" line on each upcoming bout. **What it is not for.** Predicting how a fight will be won, or its round.

**Inputs.** Only how long the two fighters' earlier fights lasted: the share of scheduled time they used, how often they went to a decision, how often they won or lost by a finish, and whether the fight is a title fight or five rounds. Small samples are pulled toward the average, and every input comes from earlier fights only. The scheduled rounds of an upcoming bout are not on the card, so a title fight or main event is taken to be five rounds and anything else three. That is a guess: `pipeline/title_rules.py` explains what counts as a title fight, and the model's own notes in `pipeline/model_distance.py` say how often the guess holds.

**Method.** A regularised logistic regression on the scaled inputs.

**How it is tested.** Trained and scored year by year, against the plain decision rate of the previous three years and against a coin flip, on log loss, Brier score and AUC, with a calibration table of predicted against actual.

**Known limits.** It sees how long fights lasted, not how they were fought, so it cannot see styles, a fighter's chin or a change in weight class.

**Monitoring and retirement.** The same calibration check, applied to the chance of going the distance. **Retire it if, over at least 300 graded locked predictions, its log loss is clearly worse than the plain decision rate of the previous three years.**

## Goes the distance v2 (market-informed)

*Code: `pipeline/model_distance_v2.py`, `pipeline/experiment_distance_market.py`.*

**What it predicts.** The same question as v1.

**Why it exists, and how sure we are.** It keeps v1's prediction and adds one thing: how strong the favourite is on the betting line (an even fight and a heavy favourite finish very differently). It is a small second stage, a logistic regression on v1's prediction, the favourite's strength and its square, fitted on earlier fights only. It passed a test whose rule was fixed in advance (`pipeline/experiment_distance_market.py`), but narrowly: the gain was small and the 95% range of it barely cleared zero. So it runs **alongside** v1, never instead of it, and both are locked before each event.

**Known limits.** It only exists for bouts with a line. A gain this small cannot be confirmed by the live record for a very long time, so for now it stays "cannot be told apart", and the README gives the number of fights that would take.

**Monitoring and retirement.** The same calibration check. **Retire it if, over at least 300 graded locked predictions, it is clearly worse than v1.** This rule is also implemented in `pipeline/model_distance_v2.py`, where `scoring` prints where it stands.

## The rules at a glance

| Model | Compared against | Retire if, over at least 300 graded locked predictions |
|---|---|---|
| Winner v2 (model-only) | A coin flip | Its log loss is clearly worse |
| Winner v3 (market-informed) | The bookmakers' opening line alone | Its log loss is clearly worse |
| Goes the distance v1 | The plain decision rate of the previous three years | Its log loss is clearly worse |
| Goes the distance v2 | Goes the distance v1 | It is clearly worse |

"Clearly worse" means the 95% range of the per-fight log-loss difference lies entirely on the wrong side of zero. With fewer fights than the rule needs, the answer is "keep", and the report says how many more are needed. For every model, a graded record more than 3 standard deviations below what its own probabilities expected is flagged for a person after at least 100 graded predictions. The daily job (`python pipeline/auto_update.py health`, and standalone `python pipeline/model_health.py`) lists anything a rule flags under "Needs your attention".

These rules are for retiring a model that is doing harm. They are not a test that a model is good: for gains as small as these, the live record would need years to show that.

**Changing a rule.** Change this page and `pipeline/model_health.py` in the same commit. A test fails if they disagree.
