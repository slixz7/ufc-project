-- =====================================================================
-- Model v2: a pick for EVERY fight (past and upcoming). Safe to re-run.
--   fight_picks_v2     one row per past fight (backtest: each pick uses only earlier fights)
--   upcoming_picks_v2  picks for announced bouts, made before the event and never changed
--   v2_*               views the web app reads
-- tier:   full (both fighters have 3+ earlier UFC fights) | limited (the less experienced has 1-2) | debut (a fighter has none)
-- method: model (trained on earlier fights) | early_rule (before the model had enough fights to learn from)
-- =====================================================================
CREATE TABLE IF NOT EXISTS fight_picks_v2 (
    fight_url         TEXT PRIMARY KEY,
    event_date        DATE NOT NULL,
    event_name        TEXT,
    weight_class      TEXT,
    title_fight       BOOLEAN,
    fighter_a         TEXT,
    fighter_b         TEXT,
    a_prior_fights    INTEGER,
    b_prior_fights    INTEGER,
    tier              TEXT,
    method            TEXT,
    p_a_wins          DOUBLE PRECISION,
    predicted_winner  TEXT,
    confidence        DOUBLE PRECISION,
    actual_winner     TEXT,
    correct           INTEGER,
    trained_on_fights INTEGER,
    scored_at         TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upcoming_picks_v2 (
    bout_id           TEXT PRIMARY KEY,
    event_name        TEXT,
    event_date        DATE,
    bout_order        INTEGER,
    weight_class      TEXT,
    title_fight       BOOLEAN,
    fighter_a         TEXT,
    fighter_b         TEXT,
    a_prior_fights    INTEGER,
    b_prior_fights    INTEGER,
    tier              TEXT,
    method            TEXT,
    p_a_wins          DOUBLE PRECISION,
    predicted_winner  TEXT,
    confidence        DOUBLE PRECISION,
    trained_on_fights INTEGER,
    status            TEXT DEFAULT 'locked',      -- locked | cancelled | no_result
    predicted_at      TIMESTAMPTZ DEFAULT now(),
    actual_winner     TEXT,
    correct           INTEGER,
    graded_at         TIMESTAMPTZ
);

CREATE OR REPLACE VIEW v2_event_scoreboard AS
SELECT event_date, event_name, COUNT(*) AS fights, SUM(correct) AS correct
FROM fight_picks_v2 WHERE method = 'model'
GROUP BY event_date, event_name;

CREATE OR REPLACE VIEW v2_scoreboard_running AS
SELECT event_date, event_name, fights, correct,
       SUM(fights)  OVER w AS total_fights,
       SUM(correct) OVER w AS total_correct,
       ROUND(SUM(correct) OVER w::numeric / SUM(fights) OVER w, 4) AS running_accuracy
FROM v2_event_scoreboard
WINDOW w AS (ORDER BY event_date);

CREATE OR REPLACE VIEW v2_confidence_buckets AS
SELECT CASE WHEN confidence < 0.55 THEN '1) 50-55 pct'
            WHEN confidence < 0.60 THEN '2) 55-60 pct'
            WHEN confidence < 0.65 THEN '3) 60-65 pct'
            WHEN confidence < 0.70 THEN '4) 65-70 pct'
            ELSE '5) 70+ pct' END AS confidence_bucket,
       COUNT(*) AS fights,
       ROUND(AVG(correct::numeric), 4) AS accuracy
FROM fight_picks_v2 WHERE method = 'model'
GROUP BY 1;

CREATE OR REPLACE VIEW v2_tier_accuracy AS
SELECT tier, method, COUNT(*) AS fights, SUM(correct) AS correct, ROUND(AVG(correct::numeric), 4) AS accuracy
FROM fight_picks_v2
GROUP BY tier, method;

CREATE OR REPLACE VIEW v2_year_accuracy AS
SELECT EXTRACT(YEAR FROM event_date)::int AS year, COUNT(*) AS fights, ROUND(AVG(correct::numeric), 4) AS accuracy
FROM fight_picks_v2 WHERE method = 'model'
GROUP BY 1;
