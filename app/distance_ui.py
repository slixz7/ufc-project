"""Display helpers for the goes-the-distance predictions (pure functions that return HTML; no Streamlit, no database)."""
from __future__ import annotations

import html

TIER_PHRASES = {"full": "both fighters have 3+ UFC fights", "limited": "a fighter has 1 to 2", "debut": "a fighter is a debut"}


def label(p: float) -> str:
    if p >= 0.60:
        return "likely to go to a decision"
    if p <= 0.40:
        return "likely to end early"
    return "could go either way"


def line_html(p: float, rounds: int, tier: str = "full") -> str:
    """The one-line prediction shown on an upcoming bout."""
    note = f"{html.escape(label(p))} &middot; {int(rounds)} rounds" + (" &middot; limited data" if tier == "debut" else "")
    return f'<div class="dist"><span class="k">Goes the distance</span><b>{p:.0%}</b> <span class="muted">{note}</span></div>'


def _t(rows: list[str], head: list[str]) -> str:
    return '<table class="hist"><tr>' + "".join(f"<th>{html.escape(h)}</th>" for h in head) + "</tr>" + "".join(rows) + "</table>"


def section_html(s: dict, live: dict | None = None) -> str:
    """The Track record section. `s` is distance_scores.distance_scores(...) of the backtest; `live` the same for graded locked predictions."""
    sc = s["scores"]
    baseline = min(sc["recent"]["log_loss"], sc["coin"]["log_loss"])
    better = sc["model"]["log_loss"] < baseline - 1e-9                        # bold only when the model genuinely beats both baselines
    names = [("coin", "Coin flip"), ("recent", "Plain decision rate of the previous three years"), ("model", "Goes-the-distance model")]
    rows = []
    for k, name in names:
        cells = [f"{sc[k]['log_loss']:.4f}", f"{sc[k]['brier']:.4f}", f"{sc[k]['auc']:.3f}"]
        if k == "model" and better:
            cells = [f"<b>{c}</b>" for c in cells]
        rows.append(f'<tr><td>{html.escape(name)}</td>' + "".join(f'<td class="mono">{c}</td>' for c in cells) + "</tr>")
    one = lambda label_, x: (f'<tr><td>{label_}</td><td class="mono">{x["n"]:,}</td><td class="mono">{x["mean"]:+.4f}</td>'
                             f'<td class="mono">{x["lo"]:+.4f} to {x["hi"]:+.4f}</td><td>{html.escape(x["verdict"])}</td></tr>')
    cmp_rows = [one("The plain decision rate (all fights)", s["vs_recent"]), one("A coin flip", s["vs_coin"])]
    cmp_rows += [one(f"The plain decision rate, where {html.escape(TIER_PHRASES.get(t, t))}", x) for t, x in s["tiers"].items()]
    cal = [f'<tr><td>{html.escape(c["label"])}</td><td class="mono">{c["n"]:,}</td><td class="mono">{c["predicted"]:.1%}</td><td class="mono">{c["actual"]:.1%}</td></tr>' for c in s["calibration"]]
    out = ['<div class="sect"><h3>Will the fight go the distance?</h3></div>',
           '<div class="aside" style="margin-top:0">Besides the winner, every upcoming bout gets a prediction, locked before the event, of whether it ends in a decision. It uses only how long '
           'the two fighters&rsquo; earlier fights lasted, plus whether it is a title fight or five rounds. '
           f'Tested year by year on {s["n"]:,} fights ({s["first_year"]} to {s["last_year"]}), each predicted from earlier fights only. '
           'Lower log loss and Brier score are better; an AUC of 0.5 is chance.</div>',
           _t(rows, ["Predictor", "Log loss", "Brier score", "AUC"]),
           '<div class="note" style="margin-top:14px">Compared with each baseline (log loss per fight; negative means the model is better; the range is a 95% interval).</div>',
           _t(cmp_rows, ["Model compared with", "Fights", "Average", "95% range", "Verdict"]),
           f'<div class="note">It was better than the plain decision rate in {s["years_better"]} of {s["years_total"]} years. Predicted against actual, by how likely the model said the fight was to go the distance:</div>',
           _t(cal, ["Predicted chance", "Fights", "Average predicted", "Actually went the distance"])]
    if live and live["n"] > 0:
        lm, lr = live["scores"]["model"]["log_loss"], live["scores"]["recent"]["log_loss"]
        out.append(f'<div class="note"><b>The locked record:</b> {live["n"]} graded predictions made before their events. {round(live["rate"] * live["n"])} went the distance. '
                   f'Log loss {lm:.4f} for the model against {lr:.4f} for the plain decision rate. A few cards are too few fights to conclude anything; the record builds over time.</div>')
    return "".join(out)
