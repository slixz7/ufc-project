"""Display helpers for the Insights page (pure functions that return HTML; no Streamlit, no database). The numbers come from pipeline/insights.py."""
from __future__ import annotations

import html

INK, ACCENT, MUTED, TRACK = "#171717", "#c4361f", "#6b675d", "#e4dfd0"
NOT_ENOUGH = "Not enough fights yet to say anything here."


def _pc(x: float) -> str:
    return f"{round(100 * x)}%"


def bar(value: float, even_marker: bool = False, colour: str = INK) -> str:
    """A horizontal bar for a share between 0 and 1, optionally with a dashed line at 50% (a coin flip)."""
    w = max(0.0, min(1.0, float(value))) * 100
    marker = f'<div style="position:absolute;left:50%;top:-3px;height:16px;border-left:2px dashed {ACCENT}"></div>' if even_marker else ""
    return (f'<div style="position:relative;height:10px;background:{TRACK}"><div style="height:10px;width:{w:.1f}%;background:{colour}"></div>{marker}</div>')


def _table(head: list[str], rows: list[str], widths: tuple[int, ...] | None = None) -> str:
    """A table in the site's style. With `widths` (percentages) the columns are fixed, so bars in stacked tables start and end in the same place."""
    cols = "<colgroup>" + "".join(f'<col style="width:{w}%">' for w in widths) + "</colgroup>" if widths else ""
    fixed = ' style="table-layout:fixed"' if widths else ""
    return f'<table class="hist"{fixed}>' + cols + "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in head) + "</tr>" + "".join(rows) + "</table>"


def _range(r: dict) -> str:
    return f'<span class="muted">{_pc(r["lo"])} to {_pc(r["hi"])}</span>'


def bars_html(rows: list[dict], label_key: str, head: str, even_marker: bool = False, count: str = "n") -> str:
    """One bar per row: its label, a bar for its rate, the rate with its 95% range, and a count. `count` is "n" (how many fights the row rests on, the right choice when
    each row is its own group) or "k" (how many fights ended that way, the right choice when every row shares one group and n would repeat)."""
    body = [(f'<tr><td>{html.escape(str(r[label_key]))}</td><td>{bar(r["rate"], even_marker)}</td>'
             f'<td class="mono"><b>{_pc(r["rate"])}</b> {_range(r)}</td><td class="mono muted">{r[count]:,}</td></tr>') for r in rows]
    return _table([head, "", "Share (95% range)", "Fights"], body, widths=(27, 31, 28, 14))


def method_html(shares: dict) -> str:
    rows = [r for r in shares["rows"] if r["method"] != "Other" or r["k"]]
    return bars_html(rows, "method", "How the fight ended", count="k")


def rounds_html(rounds: list[dict]) -> str:
    out = []
    for blk in rounds:
        if blk["n"] < 1:
            continue
        rows = [{**r, "label": f"Round {r['round']}"} for r in blk["rounds"]]
        out.append(f'<div class="aside" style="margin-top:14px">{blk["scheduled"]}-round fights that ended early ({blk["n"]:,})</div>' + bars_html(rows, "label", "Ended in", count="k"))
    return "".join(out)


def favourites_html(rows: list[dict]) -> str:
    body = [(f'<tr><td>{html.escape(r["label"])}</td><td class="mono muted">{r["n"]:,}</td>'
             f'<td style="width:22%">{bar(r["fav_won"]["rate"], True)}</td><td class="mono"><b>{_pc(r["fav_won"]["rate"])}</b> <span class="muted">(market {_pc(r["implied"])})</span></td>'
             f'<td style="width:22%">{bar(r["decision"]["rate"], False, ACCENT)}</td><td class="mono"><b>{_pc(r["decision"]["rate"])}</b></td></tr>') for r in rows]
    return _table(["The market made the favourite", "Fights", "", "Favourite won", "", "Went the distance"], body)


def edges_html(rows: list[dict], head: str) -> str:
    return f'<div style="margin-top:18px">{bars_html(rows, "label", head, even_marker=True)}</div>' if rows else ""


def section(title: str, sentence: str | None, body: str, how_to_read: str) -> str:
    """A titled section: the sentence built from the numbers (or a plain note when there is too little data), the chart, and how to read it."""
    lead = f'<div class="aside" style="margin-top:0;max-width:780px"><b>{html.escape(sentence)}</b></div>' if sentence else f'<div class="note">{NOT_ENOUGH}</div>'
    return f'<div class="sect"><h3>{html.escape(title)}</h3></div>{lead}{body}<div class="note">{html.escape(how_to_read)}</div>'


def sections(d: dict) -> list[str]:
    """The whole page body, one HTML string per section, from insights.compute_all(...)."""
    s = d["sentences"]
    have = lambda rows: bool(rows)
    out = [
        section("How fights end", " ".join(x for x in (s["method"], s["rounds"]) if x) or None,
                (method_html(d["method"]) if d["method"]["n"] else "") + rounds_html(d["rounds"]),
                "Every fight since 2002 with 3 or 5 scheduled rounds; disqualifications are left out. \"Other\" is rare results such as an overturned fight. Ranges are 95%."),
        section("Which divisions go the distance", s["divisions"], bars_html(d["divisions"], "division", "Division") if have(d["divisions"]) else "",
                "Divisions with at least 100 fights. A longer bar means more fights going to the judges; a shorter one means more finishes."),
        section("Are fights going the distance more or less often?", s["trend"], bars_html([{**r, "label": str(r["year"])} for r in d["years"]], "label", "Year") if have(d["years"]) else "",
                "Years with at least 100 fights, each with its own range. A swing of a few points from one year to the next is the kind of thing chance produces."),
        section("Favourites, upsets and finishing", " ".join(x for x in (s["favourites"], s["upsets"]) if x) or None, favourites_html(d["favourites"]) if have(d["favourites"]) else "",
                "The market's price is the betting line with the bookmaker's margin divided out evenly across both fighters, a simple method that tends to make heavy favourites look "
                "a little weaker than they are, so a favourite winning a few points more often than priced here does not by itself mean the line was wrong. The site never shows odds: "
                "these are groups of fights, not individual lines, and only fights that have a line are included. The dashed line marks 50%."),
        section("Does UFC experience matter?", s["experience"], bars_html([{**r, "label": f"{r['label']} UFC fights"} for r in d["experience"]["gap"]], "label",
                                                                        "The fighter with") if have(d["experience"]["gap"]) else "",
                "\"UFC fights\" counts a fighter's fights in this data before the one being looked at (from 2005 on), so it misses fights in other promotions. The bars show how often "
                "the more experienced fighter won. More UFC fights usually means a longer career, so read this beside the age pattern below."),
        section("Age and reach: how much do they matter?", s["edges"], (edges_html(d["edges"]["age"], "The older fighter, by how much") + edges_html(d["edges"]["reach"], "The longer reach, by how much"))
                if have(d["edges"]["age"]) or have(d["edges"]["reach"]) else "",
                "A pattern is not a cause: older fighters and longer reaches also go with other things, such as weight class and who is booked against whom. The dashed line marks 50%."),
    ]
    return out
