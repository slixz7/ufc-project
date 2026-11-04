"""Display helpers for the evidence meter on the Track record page (pure functions that return HTML; no Streamlit, no database). The numbers come from pipeline/evidence.py."""
from __future__ import annotations

import html
import math

from evidence import years_text
from insights_ui import ACCENT, INK, MUTED, TRACK

INTRO = ("The live record is the only clean test of these models, and it is small. For each claim this shows what the live record says so far, how much uncertainty is left next "
         "to what the backtest found, and how many graded fights it would take to be reasonably sure. The bars fill as cards are graded, and for small gains they fill slowly.")
HOW_TO_READ = ("Log loss scores the probabilities, and lower is better, so a difference below zero favours the model. The range is the 95% range of the average per-fight difference on the "
               "graded locked predictions. \"Reasonably sure\" means an 80% chance of seeing an effect of the backtest's size at the usual 5% level, judged by how much the "
               "difference varied per fight in the backtest. These are the same figures as the README's.")


def _pos(x: float, half: float) -> float:
    """Where a value sits on an axis that runs from -half to +half, as a percentage of the width, kept inside the track."""
    return max(0.0, min(100.0, 50.0 + 50.0 * x / half))


def progress_html(e: dict) -> str:
    """How far the graded fights have got toward the number needed. A claim with nothing to confirm has no bar."""
    if e["needed"] is None:
        return ""
    pct = 100.0 * (e["progress"] or 0.0)
    width = 0.0 if e["n_live"] == 0 else max(1.0, pct)                                   # a bar that exists, however small, once there is a graded fight
    label = f'{e["n_live"]:,} of about {e["needed"]:,} graded fights ({pct:.0f}%)' if pct >= 1 or e["n_live"] == 0 else f'{e["n_live"]:,} of about {e["needed"]:,} graded fights (under 1%)'
    return (f'<div style="margin-top:12px"><div style="font-family:IBM Plex Mono,monospace;font-size:.8rem;color:{MUTED}">{html.escape(label)}</div>'
            f'<div style="position:relative;height:10px;background:{TRACK};margin-top:4px"><div style="height:10px;width:{width:.1f}%;background:{INK}"></div></div></div>')


def range_html(e: dict) -> str:
    """The live 95% range as a bar on an axis, with a solid line at zero (no difference) and a dashed line where the backtest landed."""
    live, bt = e["live"], e["backtest"]
    live_drawn = live is not None and e["n_live"] >= 2 and not math.isnan(live["lo"])
    values = [abs(bt["mean"])] if bt else []
    if live_drawn:
        values += [abs(live["lo"]), abs(live["hi"]), abs(live["mean"])]
    half = max(values + [1e-4]) * 1.15
    marks = [f'<div style="position:absolute;left:50%;top:0;bottom:0;border-left:2px solid {INK}"></div>']
    if bt:
        marks.append(f'<div style="position:absolute;left:{_pos(bt["mean"], half):.1f}%;top:0;bottom:0;border-left:2px dashed {ACCENT}"></div>')
    if live_drawn:
        lo, hi = _pos(live["lo"], half), _pos(live["hi"], half)
        marks.append(f'<div style="position:absolute;left:{lo:.1f}%;width:{max(hi - lo, 0.8):.1f}%;top:12px;height:10px;background:{INK};opacity:.4"></div>')
        marks.append(f'<div style="position:absolute;left:{_pos(live["mean"], half):.1f}%;top:7px;width:4px;height:20px;background:{INK};margin-left:-2px"></div>')
    scale = (f'<div style="display:flex;justify-content:space-between;font-family:IBM Plex Mono,monospace;font-size:.72rem;color:{MUTED};margin-top:3px">'
             f'<span>{-half:+.3f} better</span><span>no difference</span><span>worse {half:+.3f}</span></div>')
    parts = []
    if live_drawn:
        parts.append(f'<span style="display:inline-block;width:16px;height:8px;background:{INK};opacity:.4;margin-right:5px"></span>live record, 95% range')
    if bt:
        parts.append(f'<span style="display:inline-block;width:0;height:10px;border-left:2px dashed {ACCENT};margin:0 5px 0 4px"></span>what the backtest found')
    parts.append("axis: log-loss difference per fight")
    legend = f'<div style="font-size:.78rem;color:{MUTED};margin-top:5px">' + " &nbsp; ".join(parts) + "</div>"
    return f'<div style="margin-top:14px"><div style="position:relative;height:34px;background:{TRACK}">{"".join(marks)}</div>{scale}{legend}</div>'


def needed_html(e: dict) -> str:
    if e["needed"] is None:
        return '<div class="note">The backtest found no gain to confirm, so there is no number of fights to wait for.</div>'
    pace = f' At the recent pace of about {e["per_year"]:.0f} fights a year, that is {years_text(e["years_needed"])}.' if e["per_year"] and e["years_needed"] else ""
    gain = f' (a difference of {e["backtest"]["mean"]:+.4f} per fight)' if e["backtest"] else ""
    return f'<div class="note">About {e["needed"]:,} graded fights would give an 80% chance of confirming a gain as big as the backtest found{gain}.{html.escape(pace)}</div>'


def card_html(e: dict) -> str:
    return (f'<div style="border-top:1px solid {INK};padding:16px 0 4px;margin-top:6px"><div class="disp" style="font-size:1.25rem;letter-spacing:.02em">{html.escape(e["question"])}</div>'
            f'<div class="aside" style="margin-top:6px;max-width:780px"><b>{html.escape(e["sentence"])}</b></div>{progress_html(e)}{range_html(e)}{needed_html(e)}</div>')


def section_html(entries: list[dict]) -> str:
    """The whole section: heading, a short introduction, one card per claim, and how to read the figures."""
    return ('<div class="sect"><h3>How much evidence is there?</h3></div>'
            f'<div class="aside" style="margin-top:0;max-width:780px">{html.escape(INTRO)}</div>' + "".join(card_html(e) for e in entries) + f'<div class="note">{html.escape(HOW_TO_READ)}</div>')
