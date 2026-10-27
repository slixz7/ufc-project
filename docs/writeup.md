# Write-up (draft)

Two versions of the same story: a longer article (for a blog, LinkedIn article, dev.to or Medium) and a short LinkedIn post. Check every number against your live site before you publish, and swap in newer results if you have them. Add the screenshots from `docs/images/` where marked.

---

## Article

# I caught my own model cheating: a +14 point jump, a betting-market surprise, and a first card that went 8 of 14

A few weeks into a UFC prediction project, I added one column to my model: each fighter's ranking. Accuracy on ranked fights jumped from 65.4% to 79.3%. That is far above the 65 to 67% the betting market manages across all fights, so instead of celebrating I went looking for the leak.

It took one query. Khabib Nurmagomedov, a retired champion, had no ranking in any of his title fights. Alexander Volkanovski showed up as the champion in his first UFC fights in 2016. The column was a snapshot of today's rankings, so it knew who would become a champion years later. I threw it out. If I had not been suspicious of a result that good, it would be on my CV as an achievement.

*[Screenshot: the live app's Upcoming page]*

### Two quieter leaks

The raw dataset's career statistics were also snapshots of today. A veteran's 2008 row carried his 2026 numbers. I rebuilt every statistic from earlier fights only, wrote a brute-force check that recomputes features the slow way for random rows, and then rebuilt the same features as SQL window functions and verified they matched exactly across all 17,122 fighter-fights.

And in the raw data the first-listed fighter always won. A model would have learned "pick fighter 1" and scored a fake 100%. I randomised which fighter sits in which slot and built the model with no intercept, so swapping the two fighters flips its answer exactly.

### What honest looks like

With the leaks gone, a simple logistic regression got about 61 to 62% over 2014 to 2026, evaluated year by year using only earlier fights. I tried gradient boosting (worse, at 60.6%), fighter-style matchup features, momentum features, weight-class interactions and recalibration. None beat the simple model. The algorithm was not the bottleneck. Information was.

### Adding the betting market

So I added information: opening betting lines, from a public dataset, used only as a forecasting input. On 5,279 fights from 2016 to 2026:

| Predictor | Right |
|---|---|
| Coin flip | 50% |
| My model alone | 61.8% |
| The bookmakers' opening line alone | 64.9% |
| Model + market | 65.4% |

Accuracy cannot separate the last two, so I scored the probabilities instead, with log loss. The blend beat the opening line by 0.0065 per fight (95% interval 0.0036 to 0.0093). That is small, but it is statistically clear, and it comes from fights where both fighters have UFC history. For debutants the blend and the line cannot be told apart: there, the market is doing all the work. That is a more honest claim than "my model beats the bookmakers", and it is the one the data supports.

*[Screenshot: the Track record page's scoring section]*

### The first live card

The model locks every pick before the event, with a timestamp it cannot change. UFC 333 was the first card. The model-only picks went 8 of 14 and the market-informed picks 8 of 14, each about what its own stated confidences predicted (8.5). The final betting line's favourites went 11 of 14 on a card of mostly near-even fights.

Fourteen fights prove nothing. A coin flip gets 8 or more of 14 about 40% of the time, and telling a 62% model from a coin flip takes roughly 130 fights. I would rather publish that than a flattering story.

### Running it unattended

A daily GitHub Actions job reads announced cards from Wikipedia, betting lines from an odds API, and results from Kaggle, then locks picks, grades finished events and keeps a public, time-stamped log of every pick. An unattended job that writes to a database needs guardrails, so it never guesses a fighter's name (it held "Zachary Reese" until I confirmed he is "Zach Reese"), refuses a card that suddenly shrinks, runs 48 tests before it touches anything, and connects through a read-only database user for the public website.

Even the explanations needed honesty checks. My first "Why this pick?" panel showed an Elo line pointing at the lower-rated fighter every single time, because the fitted model splits credit between overlapping statistics. I measured the problem, merged the overlapping factors, and made the panel flag any bar that still contradicts the numbers beside it.

### What I would do next

Retrain the blend on the betting line actually served two days before an event, using the line snapshots the job now collects; test round-by-round fight statistics; and model whether a fight goes the distance, a different question the data may answer better than who wins.

*Live app: https://ufc-predictor.streamlit.app/ and code: https://github.com/slixz7/ufc-project. Independent project, not affiliated with the UFC, and not betting advice.*

---

## LinkedIn post (short)

I built a UFC fight predictor, and the most useful thing it did was catch me cheating.

Adding a ranking column jumped my accuracy on ranked fights from 65% to 79%. Too good to believe. Khabib had no ranking in his title fights, and Volkanovski showed as champion in his debut. The column was a snapshot of today's rankings. I threw it out.

What survived:
- Honest, walk-forward testing: about 62% from statistics alone
- Adding betting-market data: 65.4%, a small but statistically clear improvement over the bookmakers' opening line, mostly where fighters have a history
- A live, timestamped record. The first card went 8 of 14, about what the model expected, and I say plainly that 14 fights prove nothing

It runs on its own: a daily job loads cards, locks picks, grades results and publishes a public pick log. Python, SQL, Postgres, Streamlit, GitHub Actions.

Live app and code: https://ufc-predictor.streamlit.app/ and https://github.com/slixz7/ufc-project

#datascience #python #sql #machinelearning
