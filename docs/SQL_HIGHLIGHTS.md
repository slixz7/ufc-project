# SQL highlights

The models never read the raw tables. They read SQL views in `sql/features.sql`, written so that **everything known about a fighter on fight night is computed from fights that happened before it**. Five ideas in that file are worth a closer look. Every code block below is copied from the real file, and a test fails if one stops matching it.

## 1. "Only earlier fights" is one line

```sql
WINDOW w    AS (PARTITION BY fighter_url ORDER BY event_date, fight_url COLLATE "C"),
       prev AS (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
```

`w` is one fighter's career in order. `prev` is the same career with the frame `ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING`: every earlier fight of this fighter, and never the current one. Anything computed over `prev` cannot see the result it is about to be used to predict. For example, the total of significant strikes landed before this fight:

```sql
COALESCE(SUM(sig_land)    OVER prev, 0) AS prev_sig_land,
```

The `COLLATE "C"` makes the tie-break order (two fights on the same date) identical to Python's string sort, so the SQL and the Python code agree on what "earlier" means.

## 2. A second frame for recent form

```sql
AVG(won::double precision) OVER (w ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS last3_win_rate,
LAG(streak_after, 1, 0)   OVER w AS streak,
event_date - LAG(event_date) OVER w AS days_since_last
```

The last three fights before this one, the streak coming into it, and the days since the previous fight. `LAG` looks one row back by definition, so it has the same "never the current fight" property.

## 3. Rates that cannot divide by zero or by an integer

```sql
prev_sig_land / NULLIF(prev_dur_sec / 60.0, 0)                          AS slpm,
prev_sig_absorbed / NULLIF(prev_dur_sec / 60.0, 0)                      AS sapm,
prev_sig_land::double precision / NULLIF(prev_sig_att, 0)               AS sig_acc,
```

Strikes landed per minute, strikes absorbed per minute, and striking accuracy, each from earlier fights only. `NULLIF(x, 0)` turns a zero denominator into NULL, so a fighter who has never attempted a strike has an unknown accuracy, not an error and not a made-up 0%. The `::double precision` cast matters because the counts are whole numbers (`bigint`) in the database, and dividing two whole numbers in Postgres gives a whole number; dividing by `60.0`, a decimal, avoids the same trap for the per-minute rates. Takedown defence is the same idea turned around:

```sql
1 - prev_opp_td_land::double precision / NULLIF(prev_opp_td_att, 0)     AS td_def,
```

## 4. The same SQL predicts fights that have not happened

The file builds a second set of rows for upcoming bouts. As its own comment says, a placeholder row "has no result and no stats, so the window functions below compute each fighter's features from all of their real fights BEFORE the event, exactly as they would be on fight night". Past fights are then separated out with one more view:

```sql
CREATE VIEW fighter_features AS
SELECT * FROM fighter_features_all WHERE won IS NOT NULL;
```

So the backtest and the live prediction run through one code path. There is no separate "serving" logic that could slowly drift away from what the model was evaluated on.

## 5. Which fighter is "A" is decided by a hash

```sql
CASE WHEN mod(get_byte(decode(md5(fight_url), 'hex'), 0), 2) = 0 THEN 1 ELSE 2 END AS a_side
```

In the raw data the first-listed fighter tends to be the winner, so a model that learns "pick fighter 1" would look brilliant and know nothing. Deciding which fighter is A from a hash of the fight's id gives a split that is about 50/50, the same on every run, and needs no random seed to reproduce.

## Where to look next

- `sql/features.sql`: the views above.
- `sql/predictions.sql`, `sql/model_v2.sql`, `sql/model_v3.sql`: where the picks and their records are stored.
- The README's results section, for how these features are used in a walk-forward backtest.
