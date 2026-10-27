"""
Discord digest: announces (1) each batch of newly locked picks for cards in the next two weeks and (2) each event's results once all
of its picks are graded. Every announcement is recorded in the `digest_sent` table, so it is posted exactly once; one that fails
to post is simply retried on the next run.

Setup: a Discord channel webhook, stored as the repository secret DISCORD_WEBHOOK_URL.   Test it with:
  python pipeline/auto_update.py discord-test

The webhook address is a secret (anyone holding it can post to the channel). It is never printed or written to the report, and
errors are reported by status code only.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import pandas as pd
import requests
import sqlalchemy as sa

SITE = "https://ufc-predictor.streamlit.app/"
WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
LIMIT = 1900                      # Discord allows 2000 characters per message
WINDOW_DAYS = 14                  # announce picks for cards this close; announce results for events this recent
TABLES = {"model-only": "upcoming_picks_v2", "market-informed": "upcoming_picks_v3"}
DDL = "CREATE TABLE IF NOT EXISTS digest_sent (key TEXT PRIMARY KEY, sent_at TIMESTAMPTZ DEFAULT now())"
FOOTER = f"Not betting advice. The reasons behind every pick: {SITE}"
SUB = {"model-only": "Model-only picks, made when the card was announced:",
       "market-informed": "Market-informed picks, locked two days before the event:"}


# ----------------------------------------------------------------------------------------------
# formatting (pure functions)
# ----------------------------------------------------------------------------------------------
def day(d) -> str:
    d = pd.Timestamp(d).date()
    return f"{d:%a %b} {d.day}"


def chunk(lines: list[str], limit: int = LIMIT) -> list[str]:
    """Pack lines into messages no longer than `limit`, never splitting a line."""
    out, cur = [], ""
    for ln in lines:
        ln = ln if len(ln) <= limit else ln[: limit - 1] + "…"
        if cur and len(cur) + 1 + len(ln) > limit:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


def _other(r, winner: str) -> str:
    return r.fighter_b if winner == r.fighter_a else r.fighter_a


def locked_messages(event_date, event_name: str, pick_type: str, picks: pd.DataFrame, model_only: pd.DataFrame | None = None) -> list[str]:
    said = {} if model_only is None or model_only.empty else dict(zip(model_only.bout_id, model_only.predicted_winner))
    lines = [f"**{event_name}** ({day(event_date)})", SUB[pick_type]]
    for r in picks.sort_values("bout_order").itertuples():
        line = f"• {r.predicted_winner} {float(r.confidence):.0%} over {_other(r, r.predicted_winner)}"
        if pick_type == "market-informed" and r.bout_id in said and said[r.bout_id] != r.predicted_winner:
            line += f" (model-only said {said[r.bout_id]})"
        lines.append(line)
    lines.append(FOOTER)
    return chunk(lines)


def _mark(v) -> str:
    return "✅" if bool(v) else "❌"


def results_messages(event_date, event_name: str, v2: pd.DataFrame, v3: pd.DataFrame | None = None) -> list[str]:
    """v2: the event's graded model-only picks; v3: its graded market-informed picks (may be empty or None)."""
    v3 = pd.DataFrame(columns=["bout_id", "correct"]) if v3 is None else v3
    v3c = dict(zip(v3.bout_id, v3.correct))
    n2, r2 = len(v2), int(sum(bool(x) for x in v2.correct))
    lines = [f"**Results: {event_name}** ({day(event_date)})", f"Model-only picks: {r2} of {n2} right ({r2 / n2:.0%})"]
    if len(v3):
        n3, r3 = len(v3), int(sum(bool(x) for x in v3.correct))
        lines.append(f"Market-informed picks: {r3} of {n3} right ({r3 / n3:.0%})")
    lines.append("First mark: model-only. Second mark: market-informed (➖ if it had no pick).")
    for r in v2.sort_values("bout_order").itertuples():
        if pd.isna(r.actual_winner):
            continue
        second = _mark(v3c[r.bout_id]) if r.bout_id in v3c else "➖"
        lines.append(f"{_mark(r.correct)} {second}  {r.actual_winner} def. {_other(r, r.actual_winner)}")
    lines.append(f"{n2} fights cannot say whether a model is good; the record is built over many cards. {SITE}")
    return chunk(lines)


# ----------------------------------------------------------------------------------------------
# database: what is waiting to be announced
# ----------------------------------------------------------------------------------------------
def _exists(engine, table: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(sa.text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}).scalar())


def ensure(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(DDL))


def sent_keys(engine) -> set:
    ensure(engine)
    return set(pd.read_sql(sa.text("SELECT key FROM digest_sent"), engine).key)


def mark_sent(engine, keys: list[str]) -> None:
    with engine.begin() as conn:
        for k in keys:
            conn.execute(sa.text("INSERT INTO digest_sent (key) VALUES (:k) ON CONFLICT (key) DO NOTHING"), {"k": k})


def pending(engine, today: date, window: int = WINDOW_DAYS) -> list[dict]:
    """Announcements not yet sent: [{kind, label, keys, messages}], oldest event first."""
    sent = sent_keys(engine)
    groups: list[dict] = []
    frames = {}
    for ptype, table in TABLES.items():
        if _exists(engine, table):
            frames[ptype] = pd.read_sql(sa.text(
                f"SELECT bout_id, event_date, event_name, bout_order, fighter_a, fighter_b, predicted_winner, confidence FROM {table} "
                "WHERE status = 'locked' AND event_date >= :t AND event_date <= :u"), engine, params={"t": today, "u": today + timedelta(days=window)})
    model_only = frames.get("model-only")
    for ptype, df in frames.items():
        df = df.assign(key=[f"lock:{ptype}:{b}" for b in df.bout_id])
        df = df[~df.key.isin(sent)]
        for (d, name), g in df.groupby(["event_date", "event_name"]):
            mo = None if model_only is None else model_only[model_only.bout_id.isin(g.bout_id)]
            groups.append({"kind": "picks", "date": pd.Timestamp(d).date(), "order": 0 if ptype == "model-only" else 1, "label": f"{name} ({ptype})",
                           "keys": list(g.key), "messages": locked_messages(d, name, ptype, g, mo)})
    if _exists(engine, "upcoming_picks_v2"):
        cols = "bout_id, event_date, event_name, bout_order, fighter_a, fighter_b, predicted_winner, actual_winner, correct, graded_at"
        v2 = pd.read_sql(sa.text(f"SELECT {cols} FROM upcoming_picks_v2 WHERE status = 'locked' AND event_date < :t AND event_date >= :s"),
                         engine, params={"t": today, "s": today - timedelta(days=window)})
        v3 = pd.read_sql(sa.text(f"SELECT {cols} FROM upcoming_picks_v3 WHERE status = 'locked' AND event_date < :t AND event_date >= :s AND graded_at IS NOT NULL"),
                         engine, params={"t": today, "s": today - timedelta(days=window)}) if _exists(engine, "upcoming_picks_v3") else None
        for (d, name), g in v2.groupby(["event_date", "event_name"]):
            key = f"results:{pd.Timestamp(d).date()}:{name}"
            if key in sent or g.graded_at.isna().any():
                continue                                   # already announced, or the dataset does not have every result yet
            g3 = None if v3 is None else v3[v3.bout_id.isin(g.bout_id)]
            groups.append({"kind": "results", "date": pd.Timestamp(d).date(), "order": 2, "label": f"results of {name}", "keys": [key],
                           "messages": results_messages(d, name, g, g3)})
    return sorted(groups, key=lambda x: (x["date"], x["order"]))


# ----------------------------------------------------------------------------------------------
# posting
# ----------------------------------------------------------------------------------------------
def valid_webhook(url: str) -> bool:
    return url.startswith(WEBHOOK_PREFIXES) and len(url) > len(WEBHOOK_PREFIXES[0]) + 5


def post(url: str, text: str, session=None, sleep=time.sleep):
    """Post one message. Returns the HTTP status code, or the name of the error. The address is never part of what is returned."""
    s = session or requests
    for attempt in (1, 2):
        try:
            r = s.post(url, json={"content": text, "allowed_mentions": {"parse": []}}, timeout=20)
        except Exception as exc:                          # the message of a network error can contain the address, so report only its type
            return type(exc).__name__
        if r.status_code == 429 and attempt == 1:         # rate limited: wait as long as Discord asks (at most 10 s) and try once more
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            sleep(min(wait, 10.0))
            continue
        return r.status_code
    return 429


def run_digest(engine, today: date, rep, dry_run: bool, session=None, pause: float = 0.6, sleep=time.sleep) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        rep.add("Discord digest", "No DISCORD_WEBHOOK_URL set, skipped.")
        return
    if not valid_webhook(url):
        rep.add("Discord digest", "DISCORD_WEBHOOK_URL does not look like a Discord webhook address, skipped.")
        rep.need("Discord: the DISCORD_WEBHOOK_URL secret is not a Discord webhook address (it should start with https://discord.com/api/webhooks/).")
        return
    groups = pending(engine, today)
    n_msgs = sum(len(g["messages"]) for g in groups)
    if dry_run:
        rep.add("Discord digest", f"{len(groups)} announcement(s) would be posted ({n_msgs} message(s)); nothing was sent.")
        return
    posted, done, failed = 0, 0, []
    for g in groups:
        ok = True
        for text in g["messages"]:
            code = post(url, text, session, sleep)
            if code not in (200, 204):
                ok = False
                failed.append(f"{g['label']} (answer: {code})")
                break
            posted += 1
            sleep(pause)
        if ok:
            mark_sent(engine, g["keys"])
            done += 1
    lines = [f"{posted} message(s) posted for {done} announcement(s)."] if groups else ["Nothing new to announce."]
    lines += [f"could not post, will retry on the next run: {f}" for f in failed]
    rep.add("Discord digest", *lines)


def discord_test(session=None) -> int:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("Set DISCORD_WEBHOOK_URL first (the webhook address from Discord).")
        return 2
    if not valid_webhook(url):
        print("That does not look like a Discord webhook address (it should start with https://discord.com/api/webhooks/).")
        return 2
    code = post(url, "Test message from the UFC Predictor job. If you can read this, the webhook works.", session)
    if code in (200, 204):
        print("Sent. Check the Discord channel.")
        return 0
    print(f"Discord did not accept it (answer: {code}). If it is 404 the webhook was deleted; create a new one.")
    return 1
