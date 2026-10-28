"""Step `results`: download the latest results from Kaggle, add them, grade the locked picks and score the new fights."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import sqlalchemy as sa

import bout_results
import model_distance
import model_v2
import model_v3
import update_from_csv
from auto_common import Report


KAGGLE_DATASET = "scarekrow/ufc-data"


# ----------------------------------------------------------------------------------------------
# step: results
# ----------------------------------------------------------------------------------------------
def kaggle_configured() -> bool:
    """Kaggle's current single token (KAGGLE_API_TOKEN), or the older username + key pair."""
    return bool(os.getenv("KAGGLE_API_TOKEN", "").strip() or (os.getenv("KAGGLE_USERNAME", "").strip() and os.getenv("KAGGLE_KEY", "").strip()))


def kaggle_api():
    """An authenticated Kaggle client. The Kaggle package calls exit() when it cannot sign in, which would end the whole
    run, so that is turned into an ordinary error that fails only this step."""
    from kaggle.api.kaggle_api_extended import KaggleApi            # imported here so the rest works without it
    api = KaggleApi()
    try:
        api.authenticate()
    except (Exception, SystemExit) as exc:
        raise RuntimeError(f"Kaggle did not accept the token ({type(exc).__name__}). Check the KAGGLE_API_TOKEN secret.") from None
    return api


def step_results(engine, today: date, rep: Report, dry_run: bool, workdir: Path) -> None:
    with engine.connect() as conn:
        pending = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_picks_v2 WHERE graded_at IS NULL AND status = 'locked' AND event_date < :t"""),
                               {"t": today}).scalar()
        newest = conn.execute(sa.text("SELECT MAX(event_date) FROM fights")).scalar()
    if dry_run and kaggle_configured():          # a harmless look (lists the dataset's files, downloads nothing) proves the token works
        try:
            files = getattr(kaggle_api().dataset_list_files(KAGGLE_DATASET), "files", None)
            rep.add("Kaggle access", f"the token works; {KAGGLE_DATASET} lists {len(files) if files is not None else 'some'} file(s)")
        except Exception as exc:
            rep.add("Kaggle access", f"FAILED: {str(exc)[:200]}")
            rep.need(f"Kaggle: {str(exc)[:160]}")
    if not pending:
        rep.add("Results", f"No locked picks are waiting for results (dataset runs through {newest}).")
        return
    if not kaggle_configured():
        rep.add("Results", f"{pending} locked picks are waiting for results, but no Kaggle token is set.")
        rep.need(f"Results: {pending} picks are waiting for results. Add the KAGGLE_API_TOKEN secret, or download the dataset yourself and run update_from_csv.py.")
        return
    if dry_run:
        rep.add("Results", f"{pending} locked picks are waiting for results. Would download {KAGGLE_DATASET}, add new fights, grade, and score them.")
        return
    api = kaggle_api()
    dest = workdir / "kaggle"
    dest.mkdir(parents=True, exist_ok=True)
    api.dataset_download_files(KAGGLE_DATASET, path=str(dest), unzip=True, quiet=True)
    csv = next((p for p in dest.rglob("*.csv") if "full_data" in p.name.lower()), None) or next(iter(dest.rglob("*.csv")), None)
    if csv is None:
        raise RuntimeError("the downloaded dataset contains no CSV file")
    rc = update_from_csv.main(["--csv", str(csv)])
    if rc not in (0, None):
        raise RuntimeError(f"update_from_csv returned {rc}")
    for fn, label in ((lambda: model_v2.main(["grade"]), "grade v2"), (lambda: model_v3.main(["grade"]), "grade v3"),
                      (lambda: model_v2.main(["backtest"]), "score new fights (v2)"),
                      (lambda: model_distance.main(["grade"]), "grade goes-the-distance predictions"),
                      (lambda: model_distance.main(["backtest"]), "score new fights (goes the distance)"),
                      (lambda: bout_results.main(["record", "--today", today.isoformat()]), "record how bouts ended (for the friends' game)")):
        rc = fn()
        if rc not in (0, None):
            raise RuntimeError(f"{label} returned {rc}")
    with engine.connect() as conn:
        left = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_picks_v2 WHERE graded_at IS NULL AND status = 'locked' AND event_date < :t"""),
                            {"t": today}).scalar()
    rep.add("Results", f"downloaded {csv.name}; {pending - left} picks graded, {left} still waiting for the dataset to include their event")
