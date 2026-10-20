-- =====================================================================
-- Prediction log + dashboard views (safe to re-run)
--   fight_predictions   one row per scored fight (written by pipeline/predict.py)
--   event_scoreboard    accuracy per event
--   scoreboard_running  running (cumulative) accuracy after each event
--   confidence_buckets  is the model more accurate when it is more confident?
-- =====================================================================

CREATE TABLE IF NOT EXISTS fight_predictions (
    fight_url         TEXT PRIMARY KEY,
    event_date        DATE NOT NULL,
    event_name        TEXT,
    weight_class      TEXT,
    fighter_a         TEXT,
    fighter_b         TEXT,
    p_a_wins          DOUBLE PRECISION,   -- model's probability that fighter A wins
    confidence        DOUBLE PRECISION,   -- probability of the predicted winner (0.5 to 1)
    predicted_winner  TEXT,
    actual_winner     TEXT,
    correct           INTEGER,            -- 1 = prediction was right
    trained_on_fights INTEGER,            -- how many earlier fights the model saw
    scored_at         TIMESTAMPTZ DEFAULT now()
);

CREATE OR REPLACE VIEW event_scoreboard AS
SELECT event_date,
       event_name,
       COUNT(*)                          AS fights,
       SUM(correct)                      AS correct,
       ROUND(AVG(correct::numeric), 3)   AS accuracy
FROM fight_predictions
GROUP BY event_date, event_name;

CREATE OR REPLACE VIEW scoreboard_running AS
SELECT event_date,
       event_name,
       fights,
       correct,
       accuracy,
       SUM(fights)  OVER w AS total_fights,
       SUM(correct) OVER w AS total_correct,
       ROUND(SUM(correct) OVER w::numeric / SUM(fights) OVER w, 3) AS running_accuracy
FROM event_scoreboard
WINDOW w AS (ORDER BY event_date);

CREATE OR REPLACE VIEW confidence_buckets AS
SELECT CASE WHEN confidence < 0.55 THEN '1) 50-55 pct'
            WHEN confidence < 0.60 THEN '2) 55-60 pct'
            WHEN confidence < 0.65 THEN '3) 60-65 pct'
            WHEN confidence < 0.70 THEN '4) 65-70 pct'
            ELSE '5) 70+ pct' END           AS confidence_bucket,
       COUNT(*)                          AS fights,
       ROUND(AVG(correct::numeric), 3)   AS accuracy
FROM fight_predictions
GROUP BY 1;
