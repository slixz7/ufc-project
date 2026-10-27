"""Step `cards`: read the schedule and announced fight cards from Wikipedia and save them (upcoming_fights)."""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup
import pandas as pd
import requests
import sqlalchemy as sa

import upcoming
from auto_common import ROOT, Report, UA
from auto_names import letters_key, resolve_name


WIKI_API = "https://en.wikipedia.org/w/api.php"
HORIZON_DAYS = 75          # how far ahead to look for announced events
URGENT_DAYS = 10           # a held name inside this many days makes the run fail loudly
MAX_SHRINK = 2             # a card that loses more bouts than this since we last saved it is not applied


def clean(t: str) -> str:
    t = unicodedata.normalize("NFKC", str(t)).replace("\xa0", " ")
    t = re.sub(r"\[[^\]]{0,12}\]", "", t)             # footnote markers such as [1] [a] [note 2]
    return re.sub(r"\s+", " ", t).strip()


# ----------------------------------------------------------------------------------------------
# Wikipedia: schedule and fight cards
# ----------------------------------------------------------------------------------------------
def wiki_html(title: str, session) -> str:
    r = session.get(WIKI_API, params={"action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": 2,
                                      "redirects": 1, "disableeditsection": 1}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"Wikipedia: {j['error'].get('info', 'unknown error')} ({title})")
    return j["parse"]["text"]


def parse_date(text: str):
    t = clean(text)
    m = re.search(r"[A-Z][a-z]+\.? \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]+ \d{4}|\d{4}-\d{2}-\d{2}", t)
    if not m:
        return None
    d = pd.to_datetime(m.group(0), errors="coerce")
    return None if pd.isna(d) else d.date()


def parse_scheduled_events(html: str, today: date, horizon: int = HORIZON_DAYS) -> list[dict]:
    """Rows of the 'Scheduled events' table on 'List of UFC events': name, date, wiki page title."""
    soup = BeautifulSoup(html, "html.parser")
    table = None
    anchor = soup.find(id=re.compile(r"^Scheduled_events?$", re.I))
    if anchor is not None:
        table = anchor.find_next("table")
    if table is None:
        for t in soup.find_all("table"):
            head = " ".join(th.get_text(" ", strip=True).lower() for th in t.find_all("th")[:10])
            if "event" in head and "date" in head:
                table = t
                break
    if table is None:
        return []
    out = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        texts = [clean(c.get_text(" ", strip=True)) for c in cells]
        d = next((parse_date(t) for t in texts if parse_date(t)), None)
        if d is None or not (today < d <= today + timedelta(days=horizon)):
            continue
        link = next((c.find("a", href=re.compile(r"^/wiki/[^#:]")) for c in cells if c.find("a", href=re.compile(r"^/wiki/[^#:]"))), None)
        name = texts[0]
        out.append({"name": name, "date": d, "page": unquote(link["href"][len("/wiki/"):]).replace("_", " ") if link else None})
    return out


SEPARATORS = {"vs.", "vs", "v.", "def.", "def", "defeated"}


def parse_card(html: str) -> list[dict]:
    """Bouts from the fight-card table(s) of an event page, main event first. Tolerant of layout: every row that has a
    'vs.' cell is a bout; the fighter before it is A, the fighter after it is B, the first cell is the weight class."""
    soup = BeautifulSoup(html, "html.parser")
    bouts, seen = [], set()
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            texts = [clean(c.get_text(" ", strip=True)) for c in cells]
            if len(texts) < 3:
                continue
            sep = next((i for i, t in enumerate(texts) if t.lower() in SEPARATORS), None)
            if sep is None or sep < 1 or sep + 1 >= len(texts):
                continue
            a_raw, b_raw = texts[sep - 1], texts[sep + 1]
            notes = " ".join(texts[sep + 2:])
            champ = bool(re.search(r"\((?:c|ic)\)", a_raw + " " + b_raw, re.I))
            a = re.sub(r"\s*\((?:c|ic)\)", "", a_raw, flags=re.I).strip()
            b = re.sub(r"\s*\((?:c|ic)\)", "", b_raw, flags=re.I).strip()
            if not a or not b or a.lower() in {"tba", "tbd"} or b.lower() in {"tba", "tbd"}:
                continue
            key = frozenset([a.lower(), b.lower()])
            if key in seen:
                continue
            seen.add(key)
            weight = texts[0] if sep >= 2 else ""
            title = champ or bool(re.search(r"championship|title", notes, re.I))
            bouts.append({"weight_class": weight, "fighter_a": a, "fighter_b": b, "title_fight": title})
    return bouts


# ----------------------------------------------------------------------------------------------
# step: cards
# ----------------------------------------------------------------------------------------------
def step_cards(engine, today: date, rep: Report, dry_run: bool, horizon: int, save_raw: Path | None, session=None, show: bool = False) -> None:
    session = session or requests.Session()
    lst = wiki_html("List of UFC events", session)
    if save_raw:
        save_raw.mkdir(parents=True, exist_ok=True)
        (save_raw / "List_of_UFC_events.html").write_text(lst, encoding="utf-8")
    events = parse_scheduled_events(lst, today, horizon)
    lines = []
    if not events:
        rep.add("Cards", "No scheduled events were found in the next %d days." % horizon)
        rep.need("Cards: the schedule table on Wikipedia could not be read. If events are announced, its layout may have changed. "
                 "Run `python pipeline/auto_update.py cards --dry-run --save-raw raw` and send me raw/List_of_UFC_events.html.")
        return
    idx = upcoming.fighter_index(engine)
    idx["lk"] = idx.norm.map(letters_key)
    aliases = upcoming.load_aliases(ROOT / "upcoming" / "aliases.csv")
    with engine.connect() as conn:
        saved = pd.read_sql(sa.text("SELECT fighter_a_name AS n, fighter_a_url AS u FROM upcoming_fights "
                                    "UNION SELECT fighter_b_name, fighter_b_url FROM upcoming_fights"), conn)
    remembered = {upcoming.norm(r.n): r.u for r in saved.itertuples() if r.u}
    for ev in events:
        head = f"{ev['name']} ({ev['date']})"
        if not ev["page"]:
            lines.append(f"{head}: no Wikipedia page yet, skipped")
            continue
        time.sleep(1)
        try:
            html = wiki_html(ev["page"], session)
        except Exception as exc:
            lines.append(f"{head}: could not read the page ({exc})")
            rep.need(f"Cards: could not read the page for {ev['name']}: {exc}")
            continue
        if save_raw:
            (save_raw / (re.sub(r"[^A-Za-z0-9]+", "_", ev["page"])[:80] + ".html")).write_text(html, encoding="utf-8")
        bouts = parse_card(html)
        if not bouts:
            lines.append(f"{head}: no fight-card table yet (the card is probably not announced), skipped")
            continue
        resolved, held, notes, shown = [], [], [], []
        for order, b in enumerate(bouts, 1):
            ra, rb = resolve_name(b["fighter_a"], idx, aliases, remembered, today), resolve_name(b["fighter_b"], idx, aliases, remembered, today)
            if show:
                tag = lambda r: {"ok": "", "alias": " (alias)", "auto": " (matched)", "new": " (UFC debut)", "held": " (HELD)"}[r["status"]]
                shown.append(f"   {order:02d} {b['weight_class'] or '?':18s}{' [title]' if b['title_fight'] else '':9s} {b['fighter_a']}{tag(ra)}  vs  {b['fighter_b']}{tag(rb)}")
            for nm, r in ((b["fighter_a"], ra), (b["fighter_b"], rb)):
                if r["status"] in ("auto", "new") and r["note"]:
                    notes.append(f"{nm}: {r['note']}")
                if r["status"] == "held":
                    held.append(f"{nm}: {r['note']}")
            if ra["url"] and rb["url"]:
                resolved.append({**b, "bout_order": order, "url_a": ra["url"], "url_b": rb["url"],
                                 "bout_id": upcoming.bout_id(ev["date"], ra["url"], rb["url"])})
        with engine.connect() as conn:
            existing = conn.execute(sa.text("SELECT COUNT(*) FROM upcoming_fights WHERE event_date = :d AND status = 'scheduled'"), {"d": ev["date"]}).scalar()
            old_name = conn.execute(sa.text("SELECT event_name FROM upcoming_fights WHERE event_date = :d AND status = 'scheduled' LIMIT 1"), {"d": ev["date"]}).scalar()
        event_name = old_name or ev["page"] or ev["name"]
        urgent = (ev["date"] - today).days <= URGENT_DAYS
        status = f"{head}: {len(bouts)} bouts read, {len(resolved)} usable, {len(held)} held, {existing} already saved"
        if len(bouts) < existing - MAX_SHRINK:
            lines.append(status + ". SKIPPED: the card shrank by more than %d bouts, which is more likely a reading problem than real" % MAX_SHRINK)
            rep.need(f"Cards: {ev['name']} has {len(bouts)} bouts on Wikipedia but {existing} saved. Check the card by hand before it is applied.")
            continue
        lines.append(status)
        lines.extend(shown)
        for h in held:
            lines.append(f"   HELD: {h}")
            if urgent:
                rep.need(f"Cards: {ev['name']} ({ev['date']}): {h}")
        for n in notes:
            lines.append(f"   note: {n}")
        if dry_run or not resolved:
            continue
        with engine.begin() as conn:
            before = {r[0] for r in conn.execute(sa.text("SELECT bout_id FROM upcoming_fights"))}
            for r in resolved:
                conn.execute(sa.text("""
                    INSERT INTO upcoming_fights (bout_id, event_name, event_date, bout_order, weight_class, title_fight,
                                                 fighter_a_name, fighter_a_url, fighter_b_name, fighter_b_url, status)
                    VALUES (:bout_id, :event_name, :event_date, :bout_order, :weight_class, :title_fight,
                            :a, :ua, :b, :ub, 'scheduled')
                    ON CONFLICT (bout_id) DO UPDATE SET event_name = EXCLUDED.event_name, bout_order = EXCLUDED.bout_order,
                        weight_class = EXCLUDED.weight_class, title_fight = EXCLUDED.title_fight, status = 'scheduled'"""),
                    {"bout_id": r["bout_id"], "event_name": event_name, "event_date": ev["date"], "bout_order": r["bout_order"],
                     "weight_class": r["weight_class"], "title_fight": r["title_fight"], "a": r["fighter_a"], "ua": r["url_a"],
                     "b": r["fighter_b"], "ub": r["url_b"]})
            cancelled = 0
            if not held:      # only when the whole card was read cleanly
                gone = [x[0] for x in conn.execute(sa.text("SELECT bout_id FROM upcoming_fights WHERE event_date = :d AND status = 'scheduled' "
                                                           "AND bout_id <> ALL(:ids)"), {"d": ev["date"], "ids": [r["bout_id"] for r in resolved]})]
                if gone:
                    conn.execute(sa.text("UPDATE upcoming_fights SET status = 'cancelled' WHERE bout_id = ANY(:g)"), {"g": gone})
                    cancelled = len(gone)
        new = sum(r["bout_id"] not in before for r in resolved)
        lines.append(f"   saved {len(resolved)} bouts ({new} new); {cancelled} removed from the card and marked cancelled")
    rep.add("Cards (Wikipedia)", *lines)
