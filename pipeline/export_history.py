"""
Save the fight history the models use to data/history_export.csv: one row per fighter per fight, with each fighter's raw
statistics, the fight's weight class, scheduled rounds, result, finishing round and time. Read-only (any connection string works).

  python pipeline/export_history.py

The file holds public fight statistics only (names, dates, results, stats). It contains no connection strings or secrets.
The data/ folder is not committed to git.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "history_export.csv"
FIGHTER_COLS = ["fight_url", "fighter_url", "fighter_name", "side", "won", "sig_land", "sig_att", "td_land", "td_att", "sub_att", "kd", "ctrl_sec",
                "dist_att", "clinch_att", "ground_att", "sig_absorbed", "opp_td_land", "opp_td_att", "height_cm", "reach_cm", "stance", "dob"]
FIGHT_COLS = ["event_date", "event_name", "weight_class", "title_fight", "num_rounds", "result", "finish_round", "finish_time"]
ZIP_ABOVE_MB = 20


def export(engine, out: Path = OUT) -> tuple[int, float, Path]:
    have = {c["name"] for c in sa.inspect(engine).get_columns("fights")}
    fight_cols = [c for c in FIGHT_COLS if c in have]                    # num_rounds is included when the table has it
    select = ", ".join([f"ff.{c}" for c in FIGHTER_COLS] + [f"f.{c}" for c in fight_cols])
    df = pd.read_sql(sa.text(f"SELECT {select} FROM fighter_fights ff JOIN fights f USING (fight_url) ORDER BY f.event_date, ff.fight_url, ff.side"), engine)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    size = out.stat().st_size / 1e6
    final = out
    if size > ZIP_ABOVE_MB:                                               # keep the upload small if it is large
        final = out.with_suffix(".zip")
        with zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(out, out.name)
    return len(df), size, final


def main() -> int:
    import model_v2
    rows, size, final = export(model_v2.make_engine())
    print(f"Saved {rows:,} rows ({size:.1f} MB) to {final}")
    if final.suffix == ".zip":
        print(f"The CSV was over {ZIP_ABOVE_MB} MB, so a smaller zip was made too: upload {final.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
