"""The "Why this distance prediction?" panel for the goes-the-distance models (pure functions that return HTML; no Streamlit, no database).

The numbers come from pipeline/distance_reasons.py through the `pick_reasons` table (versions 'd1' and 'd2'). The panel reuses the winner panel's look (the .why, .two, .fac and
.fr styles), so it needs no new stylesheet rules. A bar grows from the left for "goes the distance" and from the right for "ends early", longer meaning a bigger push.
Anything missing or malformed gives an empty string: an explanation must never break a card.
"""
from __future__ import annotations

import html

SCHEMA = 1
NET_NOTE = "net effect: the model also weighs related stats not shown here"
FOOTNOTE = ("These are tendencies in past fights, using only the numbers listed. The model knows nothing about injuries, camps or how two styles fit together"
            "; the betting line, where used, may. \"Goes the distance\" means all the scheduled rounds are fought and the judges decide.")


D2_FIELDS = ("p_v1", "p_v2", "fav_prob", "typical_fav", "line_effect", "rest_effect")


def _ok(d, kind: str) -> bool:
    return isinstance(d, dict) and d.get("schema") == SCHEMA and d.get("kind") == kind


def _valid_d2(d) -> bool:
    """A comparison with every number it needs. One that is damaged is left out; the rest of the panel still stands."""
    try:
        return _ok(d, "d2") and all(isinstance(float(d[k]), float) for k in D2_FIELDS)
    except (KeyError, TypeError, ValueError):
        return False


def _points(x: float) -> str:
    """A change in probability as plain points: 'about 3 points higher', 'less than a point lower'."""
    pts = abs(float(x)) * 100
    if pts < 0.5:
        return "less than a point"
    n = int(pts + 0.5)                                                                                              # half a point rounds up (Python's round() would give "about 0 points")
    return f"about {n} point{'s' if n != 1 else ''} {'higher' if x > 0 else 'lower'}"


def line_sentence(d2: dict) -> str:
    """What the betting line did, from the stored numbers only."""
    p1, p2 = float(d2["p_v1"]), float(d2["p_v2"])
    head = (f"The betting line makes the favourite {float(d2['fav_prob']):.0%} (a typical fight in the model's history: {float(d2['typical_fav']):.0%}). "
            f"With it the estimate goes from {p1:.0%} to {p2:.0%}")
    line, rest = float(d2["line_effect"]), float(d2["rest_effect"])
    if abs(line) * 100 < 0.5 and abs(rest) * 100 < 0.5:
        return head + ": the line changes almost nothing here."
    return (head + f". The favourite's strength moves it {_points(line)}; the rest of the change is the second stage re-weighting the model-only number"
            + (f" ({_points(rest)})." if abs(rest) * 100 >= 0.5 else " (less than a point)."))


def _factor_row(f: dict, mx: float) -> str:
    decision = f["toward"] == "decision"
    net = f' <span class="muted">({html.escape(NET_NOTE)})</span>' if f.get("net") else ""
    width = min(100, abs(float(f["c"])) / mx * 100)
    return (f'<div class="fr"><div class="fl">{html.escape(str(f["label"]))}</div><div class="fd">{html.escape(str(f["detail"]))}{net}</div>'
            f'<div class="fb {"a" if decision else "b"}"><div class="tr2"><i style="width:{width:.0f}%"></i></div><span>{"Goes the distance" if decision else "Ends early"}</span></div></div>')


def why_html(d1, d2=None, open_: bool = False) -> str:
    try:
        if not _ok(d1, "d1"):
            return ""
        cells = [("Average fight", float(d1["base"])), ("This fight, model only", float(d1["p"]))]
        has_line = _valid_d2(d2)
        if has_line:
            cells.append(("With the betting line", float(d2["p_v2"])))
        parts = ['<div class="two">' + "".join(f'<div><span class="k">{html.escape(k)}</span><b>{v:.0%}</b> go the distance</div>' for k, v in cells) + "</div>"]
        factors = d1.get("factors") or []
        if factors:
            mx = max([abs(float(f["c"])) for f in factors] + [0.35])
            rows = [_factor_row(f, mx) for f in factors]
            rest = float(d1.get("rest", 0))
            if abs(rest) >= 0.05:
                decision = rest > 0
                rows.append(f'<div class="fr rest"><div class="fl">All the rest</div><div class="fd">the smaller factors added together</div>'
                            f'<div class="fb {"a" if decision else "b"}"><div class="tr2"><i style="width:{min(100, abs(rest) / mx * 100):.0f}%"></i></div>'
                            f'<span>{"Goes the distance" if decision else "Ends early"}</span></div></div>')
            parts.append('<div class="fac"><div class="hd">What moves the model-only estimate (longer bar = bigger push)</div>' + "".join(rows) + "</div>")
        if has_line:
            parts.append(f'<div class="note" style="margin-top:8px">{html.escape(line_sentence(d2))}</div>')
        parts.append(f'<div class="note">{html.escape(FOOTNOTE)}</div>')
        return f'<details{" open" if open_ else ""}><summary>Why this distance prediction?</summary><div class="why">{"".join(parts)}</div></details>'
    except (KeyError, TypeError, ValueError):
        return ""                                                                                                       # an explanation must never break the page
