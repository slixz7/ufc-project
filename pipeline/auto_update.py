#!/usr/bin/env python3
"""
auto_update.py - hands-off upkeep for the UFC Fight Predictor.

One command runs everything in the right order (each step is separate, so one failure never blocks the others):
    cards         read the schedule and announced fight cards from Wikipedia and save them (upcoming_fights)
    v2            lock a model-only pick for every newly announced bout (model_v2.py predict)
    odds          fetch current betting lines from The Odds API and store a consensus for each announced bout
    lock          lock market-informed (v3) picks once an event is inside the lock window (default: 2 days before)
    results       download the latest results, add them, grade locked picks, score new fights
    refresh-odds  (Mondays) refresh the historical odds dataset and the v3 backtest
    odds-history  (a report, not part of `all`) what the daily line snapshots have collected so far

    python pipeline/auto_update.py all --dry-run        # look, change nothing (do this first)
    python pipeline/auto_update.py all                  # the real thing (this is what the scheduled job runs)
    python pipeline/auto_update.py cards --dry-run --show              # lists every bout it read and how each name was matched
    python pipeline/auto_update.py cards --dry-run --save-raw raw      # also saves the downloaded pages for inspection
    python pipeline/auto_update.py odds  --dry-run --save-raw raw

Safety rules (this script writes to your database on its own, so it is deliberately careful)
  * Locked picks are never changed; every pick is made before its event.
  * A fighter name that is not an exact match is NEVER guessed. Near-misses are held for you to review
    (add a line to upcoming/aliases.csv); only a name with no similar fighter is treated as a UFC debut.
  * A card that suddenly has far fewer bouts than we already saved is skipped, not applied.
  * A bout is only marked cancelled when its whole card was read cleanly (no held names).
  * Anything that needs a human is listed under "Needs your attention" and makes the run end with a non-zero
    exit code, so GitHub emails you.

Environment: DATABASE_URL (owner string), ODDS_API_KEY (optional), KAGGLE_API_TOKEN (optional; the older KAGGLE_USERNAME + KAGGLE_KEY pair also works).
Sources: Wikipedia (text under CC BY-SA, https://en.wikipedia.org), The Odds API (https://the-odds-api.com).
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pandas as pd
import requests
import sqlalchemy as sa
from bs4 import BeautifulSoup

import model_v2
import model_v3
import update_from_csv
import upcoming

log = logging.getLogger("auto_update")
ROOT = Path(__file__).resolve().parents[1]
UA = "ufc-fight-predictor/1.0 (https://github.com/slixz7/ufc-project; personal portfolio project)"
WIKI_API = "https://en.wikipedia.org/w/api.php"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
HORIZON_DAYS = 75          # how far ahead to look for announced events
LOCK_DAYS = 2              # lock market-informed picks when the event is this close
URGENT_DAYS = 10           # a held name inside this many days makes the run fail loudly
MAX_SHRINK = 2             # a card that loses more bouts than this since we last saved it is not applied
SNAPSHOT_GAP_HOURS = 3     # a bout gets at most one line snapshot per this many hours (so a repeated run adds nothing)
KAGGLE_DATASET = "scarekrow/ufc-data"
ODDS_DATASET_REPO = "https://github.com/nlbair/ufc-data.git"


# ----------------------------------------------------------------------------------------------
# report (printed, and written to the GitHub job summary)
# ----------------------------------------------------------------------------------------------
@dataclass
class Report:
    sections: list = field(default_factory=list)
    attention: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def add(self, title: str, *lines: str):
        self.sections.append((title, [l for l in lines if l]))

    def need(self, text: str):
        self.attention.append(text)

    def render(self, today: date, dry_run: bool) -> str:
        out = [f"# UFC auto-update, {today.isoformat()}" + ("  (DRY RUN: nothing was written)" if dry_run else ""), ""]
        out.append("## Needs your attention")
        out += [f"- {a}" for a in self.attention] if self.attention else ["Nothing."]
        for title, lines in self.sections:
            out += ["", f"## {title}"] + [f"- {l}" for l in lines]
        return "\n".join(out) + "\n"


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
# names: never guess
# ----------------------------------------------------------------------------------------------
def letters_key(norm_name: str) -> str:
    """The letters of a normalised name, sorted, with spaces and punctuation gone. 'Ko Seok-hyeon', 'Seok Hyeon Ko' and
    'Seokhyeon Ko' all give the same key: the same letters in a different order or spacing (family name first, hyphens)."""
    return "".join(sorted(re.sub(r"[^a-z]", "", norm_name)))


ACTIVE_DAYS = 1100          # a fighter counts as recently active if they fought within about 3 years


def resolve_name(name: str, idx: pd.DataFrame, aliases: dict, remembered: dict | None = None, today: date | None = None) -> dict:
    """status: ok | alias | auto | new | held.  Returns url (None when held), label, note.
    Order: your aliases file, then an exact unique match, then names you already resolved before (saved in upcoming_fights),
    then the same letters in a different order ('Ko Seok-hyeon' = 'Seokhyeon Ko'), then a name with middle names added or
    dropped when exactly one recently active fighter fits, then near-matches (held, or matched only when it is an
    unmistakable typo), then a debut. Nicknames and shortened first names ('Bia' / 'Beatriz', 'Wes' / 'Wesley') are held
    for you to confirm once in aliases.csv."""
    key = upcoming.norm(name)
    if not key:
        return {"status": "held", "url": None, "note": f"empty name {name!r}"}
    if key in aliases:
        try:
            url, ds, n, how = upcoming.resolve(name, idx, aliases, allow_new=False)
            return {"status": "new" if url.startswith("new:") else "alias", "url": url, "note": f"alias -> {ds or 'debut'}"}
        except upcoming.Unresolved as exc:
            return {"status": "held", "url": None, "note": str(exc)[:200]}
    hits = idx[idx.norm == key]
    if len(hits) == 1:
        return {"status": "ok", "url": hits.iloc[0].fighter_url, "note": ""}
    if len(hits) > 1:
        return {"status": "held", "url": None, "note": f"{name!r} matches {len(hits)} different fighters; add a line to upcoming/aliases.csv"}
    if remembered and key in remembered:
        return {"status": "new" if remembered[key].startswith("new:") else "ok", "url": remembered[key], "note": ""}
    if "lk" not in idx.columns:
        idx = idx.assign(lk=idx.norm.map(letters_key))
    lk = letters_key(key)
    same = idx[idx.lk == lk]
    if len(lk) >= 6 and same.fighter_url.nunique() == 1:
        row = same.iloc[0]
        return {"status": "auto", "url": row.fighter_url, "note": f"matched to {row.fighter_name} (same letters, name order or spacing differs)"}
    if len(lk) >= 6 and same.fighter_url.nunique() > 1:
        return {"status": "held", "url": None, "note": f"{name!r} could be any of {same.fighter_name.nunique()} fighters with the same letters; add a line to upcoming/aliases.csv"}
    toks = key.split()
    if len(toks) >= 2:
        cand = idx[(idx.norm != key) & idx.norm.map(lambda n: n.split()[0] == toks[0] and n.split()[-1] == toks[-1] and len(n.split()) != len(toks))]
        if cand.fighter_url.nunique() == 1:
            row = cand.iloc[0]
            age = ((pd.Timestamp(today or date.today()) - pd.to_datetime(cand.last_fight).max()).days) if "last_fight" in cand.columns else 0
            if age <= ACTIVE_DAYS:
                return {"status": "auto", "url": row.fighter_url, "note": f"matched to {row.fighter_name} (middle name differs)"}
            return {"status": "held", "url": None, "note": f"{name!r} could be {row.fighter_name}, who last fought {age // 365} years ago; add a line to upcoming/aliases.csv"}
        if cand.fighter_url.nunique() > 1:
            return {"status": "held", "url": None, "note": f"{name!r} fits {cand.fighter_name.nunique()} fighters ({', '.join(cand.fighter_name.unique()[:3])}); add a line to upcoming/aliases.csv"}
    names = sorted(set(idx.norm))
    sims = sorted(((difflib.SequenceMatcher(None, key, n).ratio(), n) for n in names), reverse=True)[:3]
    top, top_name = sims[0] if sims else (0.0, "")
    second = sims[1][0] if len(sims) > 1 else 0.0
    first, last = key.split()[0], key.split()[-1]
    same_last = [n for n in names if n.split()[-1] == last and n.split()[0][:1] == first[:1]]
    if top >= 0.93 and top - second >= 0.03:
        row = idx[idx.norm == top_name].iloc[0]
        return {"status": "auto", "url": row.fighter_url, "note": f"matched to {row.fighter_name} ({top:.0%} similar)"}
    lk_top, lk_name = max(((difflib.SequenceMatcher(None, lk, o).ratio(), n) for n, o in zip(idx.norm, idx.lk)), default=(0.0, ""))
    if top >= 0.80 or same_last or lk_top >= 0.90:
        guess = ", ".join(idx[idx.norm.isin([top_name] + same_last[:2] + ([lk_name] if lk_top >= 0.90 else []))].fighter_name.unique()[:3])
        return {"status": "held", "url": None, "note": f"{name!r} is not an exact match; could be {guess}. Add a line to upcoming/aliases.csv"}
    return {"status": "new", "url": f"new:{key.replace(' ', '-')}", "note": "treated as a UFC debut (no similar fighter found)"}


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


# ----------------------------------------------------------------------------------------------
# step: v2 picks for new bouts
# ----------------------------------------------------------------------------------------------
def step_v2(engine, today: date, rep: Report, dry_run: bool) -> None:
    with engine.connect() as conn:
        n = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_fights u LEFT JOIN upcoming_picks_v2 p USING (bout_id)
                                    WHERE u.status = 'scheduled' AND u.event_date > :t AND p.bout_id IS NULL"""), {"t": today}).scalar()
    if dry_run:
        rep.add("Model-only picks (v2)", f"{n} newly announced bouts would get a locked model-only pick")
        return
    rc = model_v2.main(["predict", "--today", today.isoformat()])
    rep.add("Model-only picks (v2)", f"{n} bouts needed a pick; model_v2 predict returned {rc}")
    if rc != 0:
        raise RuntimeError(f"model_v2 predict returned {rc}")
    model_v2.main(["explain"])               # fills in any missing "Why this pick?" reasons (never blocks a pick)


# ----------------------------------------------------------------------------------------------
# step: odds
# ----------------------------------------------------------------------------------------------
def fetch_odds(api_key: str, session=None, save_raw: Path | None = None):
    session = session or requests
    r = session.get(ODDS_URL, params={"regions": "us", "markets": "h2h", "oddsFormat": "american", "dateFormat": "iso", "apiKey": api_key},
                    headers={"User-Agent": UA}, timeout=30)
    if r.status_code == 401:
        raise RuntimeError("The Odds API rejected the key (401). Check the ODDS_API_KEY secret.")
    if r.status_code == 429:
        raise RuntimeError("The Odds API says the monthly quota or the rate limit is used up (429).")
    r.raise_for_status()
    if save_raw:
        save_raw.mkdir(parents=True, exist_ok=True)
        (save_raw / "odds_api_mma.json").write_text(r.text, encoding="utf-8")
    return r.json(), r.headers.get("x-requests-remaining")


def event_consensus(ev: dict):
    """(home, away, P(home) with margin removed as the median across bookmakers, number of books) or None."""
    home, away = ev.get("home_team"), ev.get("away_team")
    ps = []
    for bk in ev.get("bookmakers", []) or []:
        for m in bk.get("markets", []) or []:
            if m.get("key") != "h2h":
                continue
            prices = {o.get("name"): o.get("price") for o in m.get("outcomes", []) or [] if o.get("price") is not None}
            if home in prices and away in prices:
                try:
                    ps.append(model_v3.devig(float(prices[home]), float(prices[away])))
                except (ValueError, ZeroDivisionError):
                    continue
    if not ps or not home or not away:
        return None
    return home, away, float(np.median(ps)), len(ps)


def name_sim(x: str, y: str) -> float:
    """How alike two spellings of a fighter's name are. Same last name plus a first name that is a shortening of the
    other ('Alex' / 'Alexander', 'Cam' / 'Cameron') counts as a close match. Used ONLY to attach a betting line to a bout
    that is already on the card on the same date, where both fighters must match; never to decide who a fighter is."""
    a, b = upcoming.norm(x), upcoming.norm(y)
    if not a or not b:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = a.split(), b.split()
    if ta[-1] == tb[-1]:
        if len(ta) == 1 or len(tb) == 1 or ta[0].startswith(tb[0]) or tb[0].startswith(ta[0]):
            r = max(r, 0.97)
        elif len(os.path.commonprefix([ta[0], tb[0]])) >= 4:          # 'Ramazan' / 'Ramazonbek'
            r = max(r, 0.92)
    # family name first, hyphens and a one-letter romanisation difference ('Jeongyoung Lee' / 'Lee Jeong-yeong'): the letters of the two
    # names are almost the same. A shared name part is required, because different fighters can be anagrams (Ryan Bader / Dean Barry).
    if set(ta) & set(tb) and difflib.SequenceMatcher(None, letters_key(a), letters_key(b)).ratio() >= 0.92:
        r = max(r, 0.9)
    return r


def _pair_sim(x1: str, x2: str, y1: str, y2: str) -> float:
    return max(min(name_sim(x1, y1), name_sim(x2, y2)), min(name_sim(x1, y2), name_sim(x2, y1)))


def match_line_to_bout(home: str, away: str, commence: date, bouts: pd.DataFrame):
    """Return (bout row, home_is_a) for the best-matching announced bout, or None."""
    best, best_s = None, 0.0
    for r in bouts.itertuples():
        if abs((r.event_date - commence).days) > 1:
            continue
        s = _pair_sim(home, away, r.a, r.b)
        if s > best_s:
            best, best_s = r, s
    if best is None or best_s < 0.88:
        return None
    return best, name_sim(home, best.a) >= name_sim(home, best.b)


def classify_unmatched(home: str, away: str, commence: date, bouts: pd.DataFrame) -> str:
    """The Odds API's MMA feed also lists other promotions and rumoured or exhibition matchups, so most lines that match
    none of our bouts are simply not UFC. A line only matters when one of its fighters IS on a saved UFC bout within a day
    of it but against someone else (a late replacement opponent): 'card_changed'. Everything else is 'ignored'."""
    for r in bouts.itertuples():
        if abs((r.event_date - commence).days) > 1:
            continue
        if any(max(name_sim(x, r.a), name_sim(x, r.b)) >= 0.88 for x in (home, away)):
            return "card_changed"
    return "ignored"


SNAPSHOT_DDL = [
    """CREATE TABLE IF NOT EXISTS odds_snapshots (
        id             BIGSERIAL PRIMARY KEY,
        snapshot_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        bout_id        TEXT NOT NULL,
        event_date     DATE,
        fighter_a      TEXT,
        fighter_b      TEXT,
        commence_time  TIMESTAMPTZ,
        market_prob_a  DOUBLE PRECISION,     -- median across bookmakers, margin removed
        prob_min_a     DOUBLE PRECISION,
        prob_max_a     DOUBLE PRECISION,
        n_books        INTEGER,
        books          JSONB,                -- every bookmaker's prices, in card order: [{book, a, b, p_a}]
        source         TEXT)""",
    "CREATE INDEX IF NOT EXISTS odds_snapshots_bout_idx ON odds_snapshots (bout_id, snapshot_at)",
    """CREATE OR REPLACE VIEW odds_bout_summary AS
       WITH r AS (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY bout_id ORDER BY snapshot_at) AS rn_first,
                              ROW_NUMBER() OVER (PARTITION BY bout_id ORDER BY snapshot_at DESC) AS rn_last
                  FROM odds_snapshots s)
       SELECT bout_id, MAX(event_date) AS event_date, MAX(fighter_a) AS fighter_a, MAX(fighter_b) AS fighter_b, COUNT(*) AS snapshots,
              MIN(snapshot_at) AS first_seen, MAX(snapshot_at) AS last_seen,
              MAX(market_prob_a) FILTER (WHERE rn_first = 1) AS first_prob_a,
              MAX(market_prob_a) FILTER (WHERE rn_last = 1)  AS last_prob_a
       FROM r GROUP BY bout_id""",
    # the raw prices are kept private: the website's read-only user cannot see them (the app never shows odds)
    """DO $$ BEGIN
         IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_reader') THEN
           EXECUTE 'REVOKE ALL ON odds_snapshots FROM app_reader';
           EXECUTE 'REVOKE ALL ON odds_bout_summary FROM app_reader';
         END IF;
       END $$""",
]


def ensure_snapshot_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in SNAPSHOT_DDL:
            conn.exec_driver_sql(stmt)


def event_books(ev: dict) -> list[dict]:
    """Every bookmaker's head-to-head prices for one listed fight: [{book, home, away, p_home}] (American prices)."""
    home, away = ev.get("home_team"), ev.get("away_team")
    out = []
    for bk in ev.get("bookmakers", []) or []:
        for m in bk.get("markets", []) or []:
            if m.get("key") != "h2h":
                continue
            prices = {o.get("name"): o.get("price") for o in m.get("outcomes", []) or [] if o.get("price") is not None}
            if home in prices and away in prices:
                try:
                    out.append({"book": bk.get("key"), "home": float(prices[home]), "away": float(prices[away]),
                                "p_home": model_v3.devig(float(prices[home]), float(prices[away]))})
                except (ValueError, ZeroDivisionError):
                    continue
    return out


def orient_books(books: list[dict], home_is_a: bool) -> list[dict]:
    """The same prices in card order (a = the card's fighter A), with each book's margin-free P(A)."""
    return [{"book": b["book"], "a": b["home"] if home_is_a else b["away"], "b": b["away"] if home_is_a else b["home"],
             "p_a": round(b["p_home"] if home_is_a else 1 - b["p_home"], 4)} for b in books]


def step_odds(engine, today: date, rep: Report, dry_run: bool, save_raw: Path | None, session=None, show: bool = False) -> None:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        rep.add("Betting lines (The Odds API)", "No ODDS_API_KEY set, skipped. Bouts stay model-only until you add one.")
        return
    events, remaining = fetch_odds(key, session, save_raw)
    now = datetime.now(timezone.utc)
    bouts = pd.read_sql(sa.text("""SELECT u.bout_id, u.event_date, u.fighter_a_name AS a, u.fighter_b_name AS b,
                                           (SELECT COUNT(*) FROM upcoming_picks_v3 p WHERE p.bout_id = u.bout_id) AS locked
                                    FROM upcoming_fights u WHERE u.status = 'scheduled' AND u.event_date >= :t"""), engine, params={"t": today})
    matched, ignored, changed, lopsided, skipped_locked, rows, snaps, shown = 0, [], [], [], 0, [], [], []
    for ev in events:
        cons = event_consensus(ev)
        if cons is None:
            continue
        home, away, p_home, n_books = cons
        try:
            commence_dt = datetime.fromisoformat(str(ev["commence_time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if commence_dt <= now:
            continue                                  # already started: a line is no longer "before the fight"
        commence = commence_dt.date()
        hit = match_line_to_bout(home, away, commence, bouts)
        if hit is None:
            if classify_unmatched(home, away, commence, bouts) == "card_changed":
                changed.append((home, away, commence))
            else:
                ignored.append((home, away, commence))
            continue
        bout, home_is_a = hit
        p_a = p_home if home_is_a else 1 - p_home
        books = orient_books(event_books(ev), home_is_a)
        snaps.append({"b": bout.bout_id, "d": bout.event_date, "fa": bout.a, "fb": bout.b, "ct": commence_dt, "p": p_a,
                      "pmin": min(x["p_a"] for x in books), "pmax": max(x["p_a"] for x in books), "n": len(books), "books": json.dumps(books),
                      "src": f"the-odds-api ({now:%Y-%m-%d %H:%M} UTC)"})
        if bout.locked:
            skipped_locked += 1                       # its pick is locked; the line is still recorded in the history above
            continue
        if not 0.02 <= p_a <= 0.98:
            lopsided.append((bout.a, bout.b, p_a))
            continue
        if bout.event_date <= today:
            continue                                  # event day: nothing left to lock, but the snapshot above is kept
        matched += 1
        shown.append(f"   {bout.event_date}  {bout.a} vs {bout.b}: {p_a:.0%} for {bout.a} (median of {n_books} book(s))")
        rows.append({"bout_id": bout.bout_id, "p": p_a, "src": f"the-odds-api median of {n_books} book(s), {now:%Y-%m-%d %H:%M} UTC"})
    stored = 0
    if not dry_run:
        ensure_snapshot_tables(engine)
        with engine.begin() as conn:
            for r in rows:
                conn.execute(sa.text("""INSERT INTO upcoming_odds (bout_id, odds_a, odds_b, market_prob_a, source) VALUES (:b, NULL, NULL, :p, :s)
                                        ON CONFLICT (bout_id) DO UPDATE SET market_prob_a = EXCLUDED.market_prob_a, source = EXCLUDED.source,
                                        entered_at = now()"""), {"b": r["bout_id"], "p": r["p"], "s": r["src"]})
            for sn in snaps:
                res = conn.execute(sa.text("""INSERT INTO odds_snapshots (bout_id, event_date, fighter_a, fighter_b, commence_time, market_prob_a, prob_min_a,
                                                                           prob_max_a, n_books, books, source)
                                              SELECT :b, :d, :fa, :fb, :ct, :p, :pmin, :pmax, :n, CAST(:books AS jsonb), :src
                                              WHERE NOT EXISTS (SELECT 1 FROM odds_snapshots s WHERE s.bout_id = :b
                                                                AND s.snapshot_at > now() - make_interval(hours => :gap))"""), {**sn, "gap": SNAPSHOT_GAP_HOURS})
                stored += res.rowcount
    lines = [f"{len(events)} fights listed by the API; {matched} matched to announced UFC bouts" + (" (dry run, not saved)" if dry_run else " and saved"),
             f"{skipped_locked} already have a locked market-informed pick (their lines are final)",
             f"API credits left this month: {remaining if remaining is not None else 'unknown'}"]
    if dry_run:
        lines.append(f"{len(snaps)} line snapshots would be added to the odds history")
    else:
        total = pd.read_sql(sa.text("SELECT COUNT(*) AS n, COUNT(DISTINCT bout_id) AS b, MIN(snapshot_at)::date AS d FROM odds_snapshots"), engine).iloc[0]
        lines.append(f"{stored} new line snapshots stored; the history now holds {int(total.n)} snapshots of {int(total.b)} bouts since {total.d}")
    if show:
        lines += shown
    for a, b, p in lopsided:
        lines.append(f"not used for a pick: the line for {a} vs {b} is too lopsided ({p:.0%}); it is still recorded in the history")
    if ignored:
        lines.append(f"{len(ignored)} other lines ignored (other promotions, rumoured or not-yet-announced matchups)")
        if show:
            lines += [f"   ignored: {h} vs {a} ({c})" for h, a, c in ignored[:15]]
    for h, a, c in changed:
        lines.append(f"CHECK THE CARD: a line lists {h} vs {a} on {c}, but one of them is on our card against someone else")
        if (c - today).days <= 5:
            rep.need(f"Odds: a line lists {h} vs {a} on {c}, but one of them is saved against a different opponent. "
                     "The card may need a replacement opponent (re-run the cards step).")
    rep.add("Betting lines (The Odds API)", *lines)


def odds_history(engine) -> int:
    """A plain report on the odds history collected so far, and (once results are in) how the last line did against our picks."""
    ok = pd.read_sql(sa.text("SELECT to_regclass('public.odds_snapshots') IS NOT NULL AS ok"), engine).ok.iloc[0]
    if not ok:
        print("No odds history yet. It starts with the first real run of the odds step.")
        return 0
    t = pd.read_sql(sa.text("SELECT COUNT(*) AS n, COUNT(DISTINCT bout_id) AS b, COUNT(DISTINCT snapshot_at::date) AS days, MIN(snapshot_at)::date AS d0, "
                            "MAX(snapshot_at)::date AS d1 FROM odds_snapshots"), engine).iloc[0]
    print(f"Odds history: {int(t.n)} snapshots of {int(t.b)} bouts over {int(t.days)} day(s), {t.d0} to {t.d1}.")
    s = pd.read_sql(sa.text("SELECT * FROM odds_bout_summary"), engine)
    multi = s[s.snapshots > 1]
    if len(multi):
        move = (multi.last_prob_a - multi.first_prob_a).abs() * 100
        print(f"Line movement between the first and last snapshot of a bout: average {move.mean():.1f} points, biggest {move.max():.1f} "
              f"({len(multi)} bouts with 2+ snapshots).")
    g = pd.read_sql(sa.text("""SELECT s.fighter_a, s.fighter_b, s.last_prob_a, v2.actual_winner, v2.correct AS v2c, v3.correct AS v3c
                               FROM odds_bout_summary s JOIN upcoming_picks_v2 v2 USING (bout_id) LEFT JOIN upcoming_picks_v3 v3 USING (bout_id)
                               WHERE v2.actual_winner IS NOT NULL"""), engine)
    if g.empty:
        print("No bout with a recorded line has a result yet. After an event is graded, this report compares the last line with our picks.")
        return 0
    fav_right = ((g.last_prob_a >= 0.5) == (g.actual_winner == g.fighter_a))
    print(f"Graded bouts with a recorded line: {len(g)}. The last line's favourite won {int(fav_right.sum())} ({fav_right.mean():.0%}); "
          f"our model-only picks {int(g.v2c.sum())} ({g.v2c.mean():.0%})" + (f"; market-informed picks {int(g.v3c.dropna().sum())} of {int(g.v3c.notna().sum())}." if g.v3c.notna().any() else "."))
    return 0


# ----------------------------------------------------------------------------------------------
# step: lock market-informed picks
# ----------------------------------------------------------------------------------------------
def step_lock(engine, today: date, rep: Report, dry_run: bool, lock_days: int) -> None:
    argv = ["predict", "--today", today.isoformat(), "--within-days", str(lock_days)] + (["--dry-run"] if dry_run else [])
    rc = model_v3.main(argv)
    rep.add("Market-informed picks (v3)", f"events within {lock_days} days are locked with the latest line; model_v3 predict returned {rc}")
    if rc not in (0, None):
        raise RuntimeError(f"model_v3 predict returned {rc}")
    if not dry_run:
        model_v3.main(["explain"])           # fills in any missing "Why this pick?" reasons (never blocks a pick)
    with engine.connect() as conn:
        soon = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_fights u LEFT JOIN upcoming_odds o USING (bout_id)
                                       WHERE u.status = 'scheduled' AND u.event_date > :t AND u.event_date <= :lim AND o.bout_id IS NULL"""),
                            {"t": today, "lim": today + timedelta(days=lock_days)}).scalar()
    if soon:
        rep.add("Bouts about to start with no betting line", f"{soon} bout(s) in the next {lock_days} days have no line and will stay model-only")


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
                      (lambda: model_v2.main(["backtest"]), "score new fights (v2)")):
        rc = fn()
        if rc not in (0, None):
            raise RuntimeError(f"{label} returned {rc}")
    with engine.connect() as conn:
        left = conn.execute(sa.text("""SELECT COUNT(*) FROM upcoming_picks_v2 WHERE graded_at IS NULL AND status = 'locked' AND event_date < :t"""),
                            {"t": today}).scalar()
    rep.add("Results", f"downloaded {csv.name}; {pending - left} picks graded, {left} still waiting for the dataset to include their event")


def step_refresh_odds(engine, today: date, rep: Report, dry_run: bool, workdir: Path, force: bool) -> None:
    if today.weekday() != 0 and not force:
        rep.add("Historical odds refresh", "Only runs on Mondays.")
        return
    if dry_run:
        rep.add("Historical odds refresh", "Would clone the odds dataset, reload it, and extend the v3 backtest.")
        return
    dest = workdir / "nlbair"
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"], check=True, capture_output=True)
    else:
        subprocess.run(["git", "clone", "--depth", "1", ODDS_DATASET_REPO, str(dest)], check=True, capture_output=True)
    csv = dest / "data" / "ufc_all_fights_wide.csv"
    for argv in (["load-odds", "--csv", str(csv)], ["backtest"]):
        rc = model_v3.main(argv)
        if rc not in (0, None):
            raise RuntimeError(f"model_v3 {argv[0]} returned {rc}")
    rep.add("Historical odds refresh", "reloaded the odds dataset and extended the v3 backtest")


# ----------------------------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------------------------
def run(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    engine = model_v2.make_engine()
    rep = Report()
    save_raw = Path(args.save_raw) if args.save_raw else None
    workdir = Path(args.workdir)
    steps = {
        "cards": lambda: step_cards(engine, today, rep, args.dry_run, args.horizon, save_raw, show=args.show),
        "v2": lambda: step_v2(engine, today, rep, args.dry_run),
        "odds": lambda: step_odds(engine, today, rep, args.dry_run, save_raw, show=args.show),
        "lock": lambda: step_lock(engine, today, rep, args.dry_run, args.lock_days),
        "results": lambda: step_results(engine, today, rep, args.dry_run, workdir),
        "refresh-odds": lambda: step_refresh_odds(engine, today, rep, args.dry_run, workdir, args.command == "refresh-odds"),
    }
    chosen = list(steps) if args.command == "all" else [args.command]
    for name in chosen:
        t0 = time.time()
        try:
            steps[name]()
            log.info("step %-13s ok (%.1fs)", name, time.time() - t0)
        except (Exception, SystemExit) as exc:               # one step failing must not stop the others (even one that calls exit())
            log.exception("step %s failed", name)
            rep.failed.append(name)
            rep.add(f"FAILED: {name}", f"{type(exc).__name__}: {str(exc)[:300]}")
            rep.need(f"The '{name}' step failed: {type(exc).__name__}: {str(exc)[:160]}")
    text = rep.render(today, args.dry_run)
    print("\n" + text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    if rep.failed:
        return 1
    return 3 if rep.attention else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hands-off upkeep for the UFC Fight Predictor")
    ap.add_argument("command", choices=["all", "cards", "v2", "odds", "lock", "results", "refresh-odds", "odds-history"])
    ap.add_argument("--dry-run", action="store_true", help="look and report; write nothing")
    ap.add_argument("--today", help="(testing only) pretend today is YYYY-MM-DD")
    ap.add_argument("--horizon", type=int, default=HORIZON_DAYS, help="how many days ahead to look for announced events")
    ap.add_argument("--lock-days", type=int, default=LOCK_DAYS, help="lock market-informed picks this many days before an event")
    ap.add_argument("--show", action="store_true", help="(cards) list every bout it read and how each name was matched; (odds) list each matched line and the ignored ones")
    ap.add_argument("--save-raw", help="folder to save the downloaded Wikipedia pages / odds response (to inspect or send to me)")
    ap.add_argument("--workdir", default=str(ROOT / "data" / "auto"), help="scratch folder for downloads")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "odds-history":
        return odds_history(model_v2.make_engine())
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
