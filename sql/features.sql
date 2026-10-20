-- =====================================================================
-- UFC fight predictor: feature engineering in SQL (PostgreSQL)
-- Reproduces the pandas features from notebook 2, using window functions.
--
-- Input tables (loaded by notebook 4):
--   fights(fight_url PK, event_date, event_name, weight_class, title_fight,
--          num_rounds, result, finish_round, finish_time)
--   fighter_fights(fight_url, fighter_url, fighter_name, side, won, <raw stats>)
--        one row per fighter per fight
--
-- Output views:
--   fighter_fights_enriched     -> adds fight duration and KO/sub win flags
--   fighter_fights_enriched_all -> the same, plus one placeholder row per fighter in each upcoming bout
--   fighter_features_all        -> each fighter's stats using ONLY their earlier fights (real + upcoming rows)
--   fighter_features            -> the real fights only (what the notebooks, model and Power BI use)
--   model_rows                  -> one row per past fight: fighter A vs B, diffs, label
--   upcoming_model_rows         -> one row per upcoming bout, same columns, no label
-- Safe to re-run (drops and recreates the views).
-- =====================================================================

-- Announced-but-not-yet-fought bouts (loaded by pipeline/upcoming.py). Created here because the views below read it.
CREATE TABLE IF NOT EXISTS upcoming_fights (
    bout_id        TEXT PRIMARY KEY,
    event_name     TEXT NOT NULL,
    event_date     DATE NOT NULL,
    bout_order     INTEGER,
    weight_class   TEXT,
    title_fight    BOOLEAN DEFAULT FALSE,
    fighter_a_name TEXT NOT NULL,           -- name as listed on the card
    fighter_a_url  TEXT NOT NULL,           -- dataset fighter_url, or 'new:<name>' for a fighter not in the dataset
    fighter_b_name TEXT NOT NULL,
    fighter_b_url  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | cancelled
    loaded_at      TIMESTAMPTZ DEFAULT now()
);

DROP VIEW IF EXISTS upcoming_model_rows;
DROP VIEW IF EXISTS model_rows;
DROP VIEW IF EXISTS fighter_features;
DROP VIEW IF EXISTS fighter_features_all;
DROP VIEW IF EXISTS fighter_fights_enriched_all;
DROP VIEW IF EXISTS fighter_fights_enriched;

-- ---------------------------------------------------------------------
-- 1. Enrich: fight duration in seconds, KO/sub win flags
-- ---------------------------------------------------------------------
CREATE VIEW fighter_fights_enriched AS
SELECT
    ff.*,
    f.event_date,
    f.event_name,
    f.weight_class,
    f.title_fight,
    f.num_rounds,
    f.result,
    ((f.finish_round - 1) * 300
       + split_part(f.finish_time, ':', 1)::int * 60
       + split_part(f.finish_time, ':', 2)::int)::double precision AS dur_sec,
    (ff.won = 1 AND f.result = 'KO/TKO')::int    AS ko_win,
    (ff.won = 1 AND f.result = 'Submission')::int AS sub_win
FROM fighter_fights ff
JOIN fights f USING (fight_url);

-- ---------------------------------------------------------------------
-- 1b. Real fights plus placeholder rows for upcoming bouts.
--     A placeholder has no result and no stats, so the window functions below compute each
--     fighter's features from all of their real fights BEFORE the event, exactly as they
--     would be on fight night. Only bouts after the latest loaded event are included.
-- ---------------------------------------------------------------------
CREATE VIEW fighter_fights_enriched_all AS
SELECT
    fight_url,
    fighter_url,
    fighter_name,
    side,
    won,
    sig_land,
    sig_att,
    td_land,
    td_att,
    sub_att,
    kd,
    ctrl_sec,
    dist_att,
    clinch_att,
    ground_att,
    sig_absorbed,
    opp_td_land,
    opp_td_att,
    height_cm,
    reach_cm,
    stance,
    dob,
    event_date,
    event_name,
    weight_class,
    title_fight,
    num_rounds,
    result,
    dur_sec,
    ko_win,
    sub_win
FROM fighter_fights_enriched
UNION ALL
SELECT
    'upcoming:' || u.bout_id,
    s.fighter_url, s.fighter_name, s.side,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    NULL, NULL, NULL, NULL, NULL, NULL,
    a.height_cm, a.reach_cm, a.stance, a.dob,
    u.event_date, u.event_name, u.weight_class, u.title_fight, NULL, NULL, NULL, NULL, NULL
FROM upcoming_fights u
CROSS JOIN LATERAL (VALUES (1, u.fighter_a_url, u.fighter_a_name),
                           (2, u.fighter_b_url, u.fighter_b_name)) AS s(side, fighter_url, fighter_name)
LEFT JOIN LATERAL (
    SELECT ff.height_cm, ff.reach_cm, ff.stance, ff.dob
    FROM fighter_fights ff JOIN fights f USING (fight_url)
    WHERE ff.fighter_url = s.fighter_url
    ORDER BY f.event_date DESC
    LIMIT 1
) a ON TRUE
WHERE u.status = 'scheduled'
  AND u.event_date > (SELECT MAX(event_date) FROM fights);

-- ---------------------------------------------------------------------
-- 2. Per-fighter features from PREVIOUS fights only
--    Key idea: window frame  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
--    = every earlier fight of this fighter, never the current one.
--    COLLATE "C" makes the tie-break order identical to Python's string sort.
-- ---------------------------------------------------------------------
CREATE VIEW fighter_features_all AS
WITH base AS (
    SELECT
        fight_url, fighter_url, fighter_name, side, event_date, won, ko_win, sub_win,
        COALESCE(dur_sec, 0)       AS dur_sec,
        COALESCE(sig_land, 0)      AS sig_land,
        COALESCE(sig_att, 0)       AS sig_att,
        COALESCE(sig_absorbed, 0)  AS sig_absorbed,
        COALESCE(td_land, 0)       AS td_land,
        COALESCE(td_att, 0)        AS td_att,
        COALESCE(opp_td_land, 0)   AS opp_td_land,
        COALESCE(opp_td_att, 0)    AS opp_td_att,
        COALESCE(sub_att, 0)       AS sub_att,
        COALESCE(kd, 0)            AS kd,
        COALESCE(ctrl_sec, 0)      AS ctrl_sec,
        COALESCE(dist_att, 0)      AS dist_att,
        COALESCE(clinch_att, 0)    AS clinch_att,
        COALESCE(ground_att, 0)    AS ground_att,
        height_cm, reach_cm, stance, dob,
        -- numbering used for the "streak" trick below
        ROW_NUMBER() OVER (PARTITION BY fighter_url
                           ORDER BY event_date, fight_url COLLATE "C") AS rn,
        ROW_NUMBER() OVER (PARTITION BY fighter_url, won
                           ORDER BY event_date, fight_url COLLATE "C") AS rn_same
    FROM fighter_fights_enriched_all
),
streaks AS (
    -- "Gaps and islands": rn - rn_same is constant within a run of identical results,
    -- so numbering inside that run gives the streak length after each fight.
    SELECT *,
        (CASE WHEN won = 1 THEN 1 ELSE -1 END)
        * ROW_NUMBER() OVER (PARTITION BY fighter_url, won, rn - rn_same
                             ORDER BY event_date, fight_url COLLATE "C") AS streak_after
    FROM base
),
agg AS (
    SELECT *,
        COUNT(*)                  OVER prev AS n_prev,
        COALESCE(SUM(won)         OVER prev, 0) AS prev_won,
        COALESCE(SUM(ko_win)      OVER prev, 0) AS prev_ko_win,
        COALESCE(SUM(sub_win)     OVER prev, 0) AS prev_sub_win,
        COALESCE(SUM(dur_sec)     OVER prev, 0) AS prev_dur_sec,
        COALESCE(SUM(sig_land)    OVER prev, 0) AS prev_sig_land,
        COALESCE(SUM(sig_att)     OVER prev, 0) AS prev_sig_att,
        COALESCE(SUM(sig_absorbed) OVER prev, 0) AS prev_sig_absorbed,
        COALESCE(SUM(td_land)     OVER prev, 0) AS prev_td_land,
        COALESCE(SUM(td_att)      OVER prev, 0) AS prev_td_att,
        COALESCE(SUM(opp_td_land) OVER prev, 0) AS prev_opp_td_land,
        COALESCE(SUM(opp_td_att)  OVER prev, 0) AS prev_opp_td_att,
        COALESCE(SUM(sub_att)     OVER prev, 0) AS prev_sub_att,
        COALESCE(SUM(kd)          OVER prev, 0) AS prev_kd,
        COALESCE(SUM(ctrl_sec)    OVER prev, 0) AS prev_ctrl_sec,
        COALESCE(SUM(dist_att)    OVER prev, 0) AS prev_dist_att,
        COALESCE(SUM(clinch_att)  OVER prev, 0) AS prev_clinch_att,
        COALESCE(SUM(ground_att)  OVER prev, 0) AS prev_ground_att,
        AVG(won::double precision) OVER (w ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS last3_win_rate,
        LAG(streak_after, 1, 0)   OVER w AS streak,
        event_date - LAG(event_date) OVER w AS days_since_last
    FROM streaks
    WINDOW w    AS (PARTITION BY fighter_url ORDER BY event_date, fight_url COLLATE "C"),
           prev AS (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
)
SELECT
    fight_url, fighter_url, fighter_name, side, event_date, won,
    n_prev,
    prev_won::double precision / NULLIF(n_prev, 0)                          AS prev_win_rate,
    last3_win_rate,
    streak,
    days_since_last,
    ((event_date - dob) / 365.25)::double precision                         AS age,
    height_cm::double precision                                             AS height,
    reach_cm::double precision                                              AS reach,
    stance,
    prev_sig_land / NULLIF(prev_dur_sec / 60.0, 0)                          AS slpm,
    prev_sig_absorbed / NULLIF(prev_dur_sec / 60.0, 0)                      AS sapm,
    prev_sig_land::double precision / NULLIF(prev_sig_att, 0)               AS sig_acc,
    prev_td_land / NULLIF(prev_dur_sec / 900.0, 0)                          AS td_per15,
    prev_td_land::double precision / NULLIF(prev_td_att, 0)                 AS td_acc,
    1 - prev_opp_td_land::double precision / NULLIF(prev_opp_td_att, 0)     AS td_def,
    prev_sub_att / NULLIF(prev_dur_sec / 900.0, 0)                          AS sub_att_per15,
    prev_kd / NULLIF(prev_dur_sec / 900.0, 0)                               AS kd_per15,
    prev_ctrl_sec / NULLIF(prev_dur_sec, 0)                                 AS ctrl_share,
    (prev_clinch_att + prev_ground_att)::double precision
        / NULLIF(prev_dist_att + prev_clinch_att + prev_ground_att, 0)      AS grappling_share,
    prev_ko_win::double precision  / NULLIF(n_prev, 0)                      AS ko_win_rate,
    prev_sub_win::double precision / NULLIF(n_prev, 0)                      AS sub_win_rate
FROM agg;

-- Real fights only: identical to the old fighter_features, so notebooks, the model and Power BI are unaffected.
CREATE VIEW fighter_features AS
SELECT * FROM fighter_features_all WHERE won IS NOT NULL;

-- ---------------------------------------------------------------------
-- 3. One row per fight, fighter A vs fighter B
--    Which fighter is "A" is decided by a deterministic hash of the fight id
--    (about 50/50). Without this, slot 1 would always be the winner.
-- ---------------------------------------------------------------------
CREATE VIEW model_rows AS
WITH assign AS (
    SELECT fight_url,
           CASE WHEN mod(get_byte(decode(md5(fight_url), 'hex'), 0), 2) = 0 THEN 1 ELSE 2 END AS a_side
    FROM fights
)
SELECT
    a.fight_url,
    a.event_date,
    a.fighter_name AS fighter_a,
    b.fighter_name AS fighter_b,
    fe.weight_class,
    fe.title_fight,
    fe.num_rounds,
    a.won AS a_wins,
    LEAST(a.n_prev, b.n_prev) AS min_prev_fights,
    a.stance AS a_stance,
    b.stance AS b_stance,
    a.n_prev AS a_n_prev,
    a.prev_win_rate AS a_prev_win_rate,
    a.last3_win_rate AS a_last3_win_rate,
    a.streak AS a_streak,
    a.days_since_last AS a_days_since_last,
    a.age AS a_age,
    a.height AS a_height,
    a.reach AS a_reach,
    a.slpm AS a_slpm,
    a.sapm AS a_sapm,
    a.sig_acc AS a_sig_acc,
    a.td_per15 AS a_td_per15,
    a.td_acc AS a_td_acc,
    a.td_def AS a_td_def,
    a.sub_att_per15 AS a_sub_att_per15,
    a.kd_per15 AS a_kd_per15,
    a.ctrl_share AS a_ctrl_share,
    a.grappling_share AS a_grappling_share,
    a.ko_win_rate AS a_ko_win_rate,
    a.sub_win_rate AS a_sub_win_rate,
    b.n_prev AS b_n_prev,
    b.prev_win_rate AS b_prev_win_rate,
    b.last3_win_rate AS b_last3_win_rate,
    b.streak AS b_streak,
    b.days_since_last AS b_days_since_last,
    b.age AS b_age,
    b.height AS b_height,
    b.reach AS b_reach,
    b.slpm AS b_slpm,
    b.sapm AS b_sapm,
    b.sig_acc AS b_sig_acc,
    b.td_per15 AS b_td_per15,
    b.td_acc AS b_td_acc,
    b.td_def AS b_td_def,
    b.sub_att_per15 AS b_sub_att_per15,
    b.kd_per15 AS b_kd_per15,
    b.ctrl_share AS b_ctrl_share,
    b.grappling_share AS b_grappling_share,
    b.ko_win_rate AS b_ko_win_rate,
    b.sub_win_rate AS b_sub_win_rate,
    a.n_prev - b.n_prev AS diff_n_prev,
    a.prev_win_rate - b.prev_win_rate AS diff_prev_win_rate,
    a.last3_win_rate - b.last3_win_rate AS diff_last3_win_rate,
    a.streak - b.streak AS diff_streak,
    a.days_since_last - b.days_since_last AS diff_days_since_last,
    a.age - b.age AS diff_age,
    a.height - b.height AS diff_height,
    a.reach - b.reach AS diff_reach,
    a.slpm - b.slpm AS diff_slpm,
    a.sapm - b.sapm AS diff_sapm,
    a.sig_acc - b.sig_acc AS diff_sig_acc,
    a.td_per15 - b.td_per15 AS diff_td_per15,
    a.td_acc - b.td_acc AS diff_td_acc,
    a.td_def - b.td_def AS diff_td_def,
    a.sub_att_per15 - b.sub_att_per15 AS diff_sub_att_per15,
    a.kd_per15 - b.kd_per15 AS diff_kd_per15,
    a.ctrl_share - b.ctrl_share AS diff_ctrl_share,
    a.grappling_share - b.grappling_share AS diff_grappling_share,
    a.ko_win_rate - b.ko_win_rate AS diff_ko_win_rate,
    a.sub_win_rate - b.sub_win_rate AS diff_sub_win_rate
FROM assign s
JOIN fighter_features a ON a.fight_url = s.fight_url AND a.side = s.a_side
JOIN fighter_features b ON b.fight_url = s.fight_url AND b.side = 3 - s.a_side
JOIN (SELECT DISTINCT fight_url, weight_class, title_fight, num_rounds
      FROM fighter_fights_enriched) fe ON fe.fight_url = s.fight_url
WHERE a.event_date >= DATE '2000-01-01';   -- skip 1994-1999 one-night tournaments

-- ---------------------------------------------------------------------
-- 4. One row per upcoming bout, with the same feature columns as model_rows.
--    Fighter A is the first name listed on the card. The model is symmetric, so the order
--    doesn't change the probabilities.
-- ---------------------------------------------------------------------
CREATE VIEW upcoming_model_rows AS
SELECT
    u.bout_id, u.event_name, u.event_date, u.bout_order, u.weight_class, u.title_fight,
    u.fighter_a_name AS fighter_a,
    u.fighter_b_name AS fighter_b,
    LEAST(a.n_prev, b.n_prev) AS min_prev_fights,
    a.n_prev AS a_n_prev,
    a.prev_win_rate AS a_prev_win_rate,
    a.last3_win_rate AS a_last3_win_rate,
    a.streak AS a_streak,
    a.days_since_last AS a_days_since_last,
    a.age AS a_age,
    a.height AS a_height,
    a.reach AS a_reach,
    a.slpm AS a_slpm,
    a.sapm AS a_sapm,
    a.sig_acc AS a_sig_acc,
    a.td_per15 AS a_td_per15,
    a.td_acc AS a_td_acc,
    a.td_def AS a_td_def,
    a.sub_att_per15 AS a_sub_att_per15,
    a.kd_per15 AS a_kd_per15,
    a.ctrl_share AS a_ctrl_share,
    a.grappling_share AS a_grappling_share,
    a.ko_win_rate AS a_ko_win_rate,
    a.sub_win_rate AS a_sub_win_rate,
    b.n_prev AS b_n_prev,
    b.prev_win_rate AS b_prev_win_rate,
    b.last3_win_rate AS b_last3_win_rate,
    b.streak AS b_streak,
    b.days_since_last AS b_days_since_last,
    b.age AS b_age,
    b.height AS b_height,
    b.reach AS b_reach,
    b.slpm AS b_slpm,
    b.sapm AS b_sapm,
    b.sig_acc AS b_sig_acc,
    b.td_per15 AS b_td_per15,
    b.td_acc AS b_td_acc,
    b.td_def AS b_td_def,
    b.sub_att_per15 AS b_sub_att_per15,
    b.kd_per15 AS b_kd_per15,
    b.ctrl_share AS b_ctrl_share,
    b.grappling_share AS b_grappling_share,
    b.ko_win_rate AS b_ko_win_rate,
    b.sub_win_rate AS b_sub_win_rate,
    a.n_prev - b.n_prev AS diff_n_prev,
    a.prev_win_rate - b.prev_win_rate AS diff_prev_win_rate,
    a.last3_win_rate - b.last3_win_rate AS diff_last3_win_rate,
    a.streak - b.streak AS diff_streak,
    a.days_since_last - b.days_since_last AS diff_days_since_last,
    a.age - b.age AS diff_age,
    a.height - b.height AS diff_height,
    a.reach - b.reach AS diff_reach,
    a.slpm - b.slpm AS diff_slpm,
    a.sapm - b.sapm AS diff_sapm,
    a.sig_acc - b.sig_acc AS diff_sig_acc,
    a.td_per15 - b.td_per15 AS diff_td_per15,
    a.td_acc - b.td_acc AS diff_td_acc,
    a.td_def - b.td_def AS diff_td_def,
    a.sub_att_per15 - b.sub_att_per15 AS diff_sub_att_per15,
    a.kd_per15 - b.kd_per15 AS diff_kd_per15,
    a.ctrl_share - b.ctrl_share AS diff_ctrl_share,
    a.grappling_share - b.grappling_share AS diff_grappling_share,
    a.ko_win_rate - b.ko_win_rate AS diff_ko_win_rate,
    a.sub_win_rate - b.sub_win_rate AS diff_sub_win_rate
FROM upcoming_fights u
JOIN fighter_features_all a ON a.fight_url = 'upcoming:' || u.bout_id AND a.side = 1
JOIN fighter_features_all b ON b.fight_url = 'upcoming:' || u.bout_id AND b.side = 2;
