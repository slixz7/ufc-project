"""
What counts as a championship fight.

The source data (UFCStats, through the Kaggle dataset) flags `title_fight` for TWO different things: championship bouts, and tournament
bouts: finals and semifinals of The Ultimate Fighter and of Road to UFC, and the early-1990s tournaments. Since 2001 a championship fight
is scheduled for five rounds, while those tournament bouts are three. UFC 325 (January 2026) shows both: Volkanovski vs Lopes 2 for the
featherweight title, and three Road to UFC finals (each worth a UFC contract) that the data also marks as "title" fights.

So, for events since 2001:
    championship = flagged and scheduled for five rounds
    tournament   = flagged and not five rounds
Before 2001 the old tournament era had no standard round counts, so the flag is left as the source gives it.

The website runs these as SQL (the CHAMPIONSHIP / TOURNAMENT / ORDER fragments below, for a `fights` table aliased `f`); the Python functions
say the same thing for pandas, and a test checks that the two agree on every edge case. The models do not use this: on the real history, replacing
the flag with the championship rule changes the goes-the-distance model's log loss by 0.00003, far inside the noise (the winner models do not use the flag).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MODERN_START = "2001-01-01"


def championship_sql(alias: str = "f") -> str:
    return f"COALESCE({alias}.title_fight AND ({alias}.num_rounds = 5 OR {alias}.event_date < '{MODERN_START}'), FALSE)"


def tournament_sql(alias: str = "f") -> str:
    return f"COALESCE({alias}.title_fight AND {alias}.event_date >= '{MODERN_START}' AND ({alias}.num_rounds IS NULL OR {alias}.num_rounds <> 5), FALSE)"


def order_sql(alias: str = "f") -> str:
    """Championship fights first, then the other five-round fights (main events), then tournament bouts, then everything else."""
    return (f"CASE WHEN {championship_sql(alias)} THEN 0 WHEN {alias}.num_rounds = 5 THEN 1 WHEN {tournament_sql(alias)} THEN 2 ELSE 3 END")


def _parts(title_fight, num_rounds, event_date):
    t = pd.Series(np.atleast_1d(title_fight)).fillna(False).astype(bool).to_numpy()
    n = pd.to_numeric(pd.Series(np.atleast_1d(num_rounds)), errors="coerce").to_numpy(dtype=float)
    d = pd.to_datetime(pd.Series(np.atleast_1d(event_date))).to_numpy() >= np.datetime64(MODERN_START)
    return t, n, d


def is_championship(title_fight, num_rounds, event_date) -> np.ndarray:
    t, n, modern = _parts(title_fight, num_rounds, event_date)
    return t & ((n == 5) | ~modern)


def is_tournament(title_fight, num_rounds, event_date) -> np.ndarray:
    t, n, modern = _parts(title_fight, num_rounds, event_date)
    return t & modern & (np.isnan(n) | (n != 5))
