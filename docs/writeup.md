# I caught my own model cheating

*A case study of a UFC fight-prediction project. The figures are a snapshot from early November 2026. The README's numbers refresh themselves from the database, so they are the current ones.*

I built a system that predicts UFC fights, publishes every prediction before the fight, and grades itself in public. It covers the data, the models, a live website, a daily automated job and a record nobody can edit afterwards. The most useful thing it did was catch me cheating, and most of what follows is about what went wrong, what held up, and what I would and would not claim.

## The result that was too good

A few weeks in, I added one column to the model: each fighter's ranking. Accuracy on ranked fights jumped from 65.4% to 79.3%. The betting market itself is right about 65 to 67% of the time across all fights, so instead of celebrating I went looking for the leak.

It took one query. Khabib Nurmagomedov, a retired champion, had no ranking in any of his title fights. Alexander Volkanovski showed as champion in his first UFC fights in 2016. The column was a snapshot of today's rankings, so it knew who would become a champion years later. I threw it out. If I had not distrusted a result that good, it would be on my CV as an achievement.

## Two quieter leaks

The raw dataset's career statistics were also snapshots of today: a veteran's 2008 row carried his 2026 numbers. I rebuilt every statistic from earlier fights only, wrote a brute-force check that recomputes the features the slow way for random rows, then rebuilt them as SQL window functions and verified that they match exactly. The one-line idea in those window functions is a frame that stops one row before the current fight, so nothing computed over it can see the result it is about to predict. The [SQL highlights](SQL_HIGHLIGHTS.md) page shows the real code.

And in the raw data the first-listed fighter always won. A model would have learned "pick fighter 1" and scored a fake 100%. I randomised which fighter sits in which slot and built the model with no intercept, so swapping the two fighters flips its answer exactly.

## What honest looks like

With the leaks gone, a simple logistic regression got about 62% over 2014 to 2026, evaluated year by year using only earlier fights. I tried gradient boosting (worse), fighter-style matchup features, momentum features, weight-class interactions and recalibration. None beat the simple model. The algorithm was not the bottleneck. Information was.

## Adding the betting market

So I added information: opening betting lines from a public dataset, used only as a forecasting input. On 5,279 fights from 2016 to 2026, each predicted from earlier fights only:

| Predictor | Right |
|---|---|
| Coin flip | 50% |
| My model alone | 61.8% |
| The bookmakers' opening line alone | 64.9% |
| Model plus market | 65.4% |

Accuracy cannot separate the last two, so I scored the probabilities with log loss. The blend beat the opening line by 0.0065 per fight (95% range 0.0036 to 0.0093). That is small, but it is statistically clear, and it comes from fights where both fighters have a UFC history. For debutants and thin histories the blend and the line cannot be told apart, so there the market is doing the work. That is a more honest claim than an edge over the market, and it is the one the data supports. The site never shows betting odds; the line is a forecasting input, and none of this is betting advice.

## A second question: will the fight go the distance?

Then I asked a different question the data may answer better than who wins: does a fight go to the judges? A model built from how long each fighter's earlier fights lasted, and whether the fight is a title fight or five rounds, beats the plain decision rate of the previous three years by about 0.023 log loss per fight, in 19 of the 21 years tested.

I then tried to improve it with the betting line, using only how lopsided the line says the fight is. I wrote the rule for judging that experiment before running it. It passed, but narrowly: a gain of about 0.003 per fight whose range barely reaches zero, so the improved version runs *alongside* the first, never instead of it. Detecting a gain that small in live results would take decades of fights, and the README says so. The project's rule is to retire the improved version only if, over at least 300 graded fights, it is clearly worse than the first.

## What the data says

Because the models learn from the same fights, I put the plain findings on the site as an Insights page, each with its sample size and a 95% range. As of November 2026, across 8,497 fights since 2002:

- Women's strawweight fights went the distance most often (66% of 381). Heavyweights went the least (33% of 704).
- The share of fights going the distance rose from the mid-2000s to around 2010, then stayed flat. The page reports both, because "the share rose" alone would be true and misleading.
- When the market made a favourite at 80% or more, that favourite won 89% of the time (the market implied 84%), and only 31% of those fights went the distance. The gap is partly the simple way the bookmaker's margin is removed, which makes heavy favourites look weaker than they are, so the page says that rather than hint that the line was wrong.
- The older fighter won 33% of the fights where the gap was eight or more years. Fighters with many more UFC fights behind them also won less often, and the page measures how often those are the same people, because two overlapping patterns are easy to mistake for two effects.

## The live record

The system locks every pick before the event with a timestamp it cannot change, and a public log keeps each one. After two graded cards, the model-only picks have gone 17 of 26 and the market-informed picks 18 of 26, each close to what its own stated probabilities expected (16.0 and 16.9). Twenty-six fights prove nothing. A coin flip often does as well, and telling a 62% model from a coin flip takes on the order of a hundred and thirty fights. I would rather say that than publish a flattering story.

## Running it unattended

A daily GitHub Actions job reads announced cards, betting lines and results, then locks picks, grades finished events and updates the public log. An unattended job that writes to a database needs guardrails. It never guesses a fighter's name: a near-miss is held for a human. It refuses a card that suddenly shrinks. It runs the whole test suite before it touches anything, and the website connects through a read-only database login.

Even that login was wrong. I had checked early on that it looked read-only. When I later built a permissions audit for the friends' game, it showed that the login belonged to a built-in role that could write every table and read the private odds table I thought I had hidden. Nothing had gone wrong, but it should not have been possible, and it is now closed and audited.

The weekly backup had been verified and never restored. So I built a restore drill that rebuilds the whole database into a throwaway one and compares every table, column by column. Its first real run failed: an integer column with a blank had been written as `1.0`, which Postgres refuses, so a real restore would have failed. I found it on a quiet day instead of a bad one.

Around that sit hundreds of automated tests that run on every push, a dependency lock proven by running the whole suite against exactly those versions, scans for credentials in the files and the whole git history, failure alerts to a private channel, and a daily health check. The health check compares each model's live record with what its own probabilities expected, and applies retirement rules that are written down in the [model cards](MODEL_CARDS.md) before the live results could tempt me to bend them.

## What I would and would not claim

I claim an honest evaluation: walk-forward testing, simple baselines, intervals, and a live record that cannot be edited afterwards. I claim a system that runs on its own, tells me when it needs a person, and has been restored from its own backup. I claim that the biggest gains in this project came from distrusting good results.

I do not claim an edge over the betting market, which does most of the work wherever it is involved. I do not claim that 26 live fights show skill. I do not claim that the small gains from the market-informed models are confirmed; the live record would need years to show them.

## Try it

- **Live site:** <https://ufc-predictor.streamlit.app/>
- **Code:** <https://github.com/slixz7/ufc-project>
- **Try it yourself:** `python demo/run_demo.py` runs the whole project on invented fighters, with no accounts.

This is an independent project, not affiliated with or endorsed by the UFC, and it is not betting advice.
