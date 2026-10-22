-- =====================================================================
-- Model v3: v2 plus the betting market. Safe to re-run.
--   fight_odds         historical opening/closing lines (nlbair/ufc-data, CC BY 4.0), 2013-2026
--   upcoming_odds      lines you enter for announced bouts
--   fight_picks_v3     after-the-fact picks (each uses only earlier fights), where a line exists
--   upcoming_picks_v3  picks made before the event once a line is loaded, never changed
--   v3_*               views the web app reads
-- Betting lines are used only as a forecasting input. This is not betting advice.
-- =====================================================================
CREATE TABLE IF NOT EXISTS fight_odds (
    fight_id       TEXT PRIMARY KEY,            -- ufcstats fight id (the end of fights.fight_url)
    event_date     DATE,
    f1_id          TEXT,
    f2_id          TEXT,
    f1_name        TEXT,
    f2_name        TEXT,
    open_prob_f1   DOUBLE PRECISION,            -- opening line as a probability, bookmaker margin removed
    close_prob_f1  DOUBLE PRECISION,
    source         TEXT
);

CREATE TABLE IF NOT EXISTS upcoming_odds (
    bout_id        TEXT PRIMARY KEY,
    odds_a         DOUBLE PRECISION,            -- American odds for fighter A (card order)
    odds_b         DOUBLE PRECISION,
    market_prob_a  DOUBLE PRECISION,            -- margin removed
    source         TEXT,
    entered_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fight_picks_v3 (
    fight_url         TEXT PRIMARY KEY,
    event_date        DATE NOT NULL,
    event_name        TEXT,
    weight_class      TEXT,
    title_fight       BOOLEAN,
    fighter_a         TEXT,
    fighter_b         TEXT,
    tier              TEXT,
    p_v2_a            DOUBLE PRECISION,
    market_prob_a     DOUBLE PRECISION,
    p_a_wins          DOUBLE PRECISION,
    predicted_winner  TEXT,
    confidence        DOUBLE PRECISION,
    actual_winner     TEXT,
    correct           INTEGER,
    v2_correct        INTEGER,
    market_correct    INTEGER,
    trained_on_fights INTEGER,
    scored_at         TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upcoming_picks_v3 (
    bout_id           TEXT PRIMARY KEY,
    event_name        TEXT,
    event_date        DATE,
    bout_order        INTEGER,
    weight_class      TEXT,
    title_fight       BOOLEAN,
    fighter_a         TEXT,
    fighter_b         TEXT,
    tier              TEXT,
    p_v2_a            DOUBLE PRECISION,
    market_prob_a     DOUBLE PRECISION,
    p_a_wins          DOUBLE PRECISION,
    predicted_winner  TEXT,
    confidence        DOUBLE PRECISION,
    trained_on_fights INTEGER,
    status            TEXT DEFAULT 'locked',    -- locked | cancelled | no_result
    predicted_at      TIMESTAMPTZ DEFAULT now(),
    actual_winner     TEXT,
    correct           INTEGER,
    graded_at         TIMESTAMPTZ
);

CREATE OR REPLACE VIEW v3_event_scoreboard AS
SELECT event_date, event_name, COUNT(*) AS fights, SUM(correct) AS correct
FROM fight_picks_v3 GROUP BY event_date, event_name;

CREATE OR REPLACE VIEW v3_scoreboard_running AS
SELECT event_date, event_name, fights, correct,
       SUM(fights)  OVER w AS total_fights,
       SUM(correct) OVER w AS total_correct,
       ROUND(SUM(correct) OVER w::numeric / SUM(fights) OVER w, 4) AS running_accuracy
FROM v3_event_scoreboard
WINDOW w AS (ORDER BY event_date);

CREATE OR REPLACE VIEW v3_summary AS
SELECT COUNT(*) AS fights,
       MIN(event_date) AS first_date,
       ROUND(AVG(v2_correct::numeric), 4)     AS v2_accuracy,
       ROUND(AVG(correct::numeric), 4)        AS v3_accuracy,
       ROUND(AVG(market_correct::numeric), 4) AS market_accuracy
FROM fight_picks_v3;

CREATE OR REPLACE VIEW v3_year_accuracy AS
SELECT EXTRACT(YEAR FROM event_date)::int AS year, COUNT(*) AS fights,
       ROUND(AVG(v2_correct::numeric), 4)     AS v2_accuracy,
       ROUND(AVG(correct::numeric), 4)        AS v3_accuracy,
       ROUND(AVG(market_correct::numeric), 4) AS market_accuracy
FROM fight_picks_v3 GROUP BY 1;

CREATE OR REPLACE VIEW v3_tier_accuracy AS
SELECT tier, COUNT(*) AS fights,
       ROUND(AVG(v2_correct::numeric), 4)     AS v2_accuracy,
       ROUND(AVG(correct::numeric), 4)        AS v3_accuracy,
       ROUND(AVG(market_correct::numeric), 4) AS market_accuracy
FROM fight_picks_v3 GROUP BY tier;

CREATE OR REPLACE VIEW v3_confidence_buckets AS
SELECT CASE WHEN confidence < 0.55 THEN '1) 50-55 pct'
            WHEN confidence < 0.60 THEN '2) 55-60 pct'
            WHEN confidence < 0.65 THEN '3) 60-65 pct'
            WHEN confidence < 0.70 THEN '4) 65-70 pct'
            ELSE '5) 70+ pct' END AS confidence_bucket,
       COUNT(*) AS fights,
       ROUND(AVG(correct::numeric), 4) AS accuracy
FROM fight_picks_v3 GROUP BY 1;
