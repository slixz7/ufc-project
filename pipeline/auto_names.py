"""Fighter-name matching, the most delicate logic in the job: resolving a card name to a fighter in the data (never guessing), and matching betting-site names to card names."""
from __future__ import annotations

import difflib
import os
import re
from datetime import date

import pandas as pd

import upcoming


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
