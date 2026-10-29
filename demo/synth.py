"""
Invented fighters and fights, for the demo. NOTHING here is real: the names are made up from syllables, the fights never happened, and every date is
measured from the day the demo is built, so there is always an upcoming card.

What it makes, in the layouts the real pipeline reads:
  fights, fighter_fights    the two base tables the real data loader fills (history up to the weeks before the live cards)
  odds                      a "historical betting lines" table, the layout `model_v3.py load-odds` reads
  cards                     five announced cards: L1 and L2 have already happened (their picks were locked before the event and graded after: a live record),
                            U1, U2 and U3 are coming up. Each card has its card file, its current lines, and (L1, L2) its real results.

Each fighter has a hidden skill that rises and falls with age and with recent form. It shows up, noisily, in the results and in the per-fight statistics
(strikes, takedowns, control time) that the real models learn from. The betting market sees the skill a little more clearly than the statistics do, as a
real market does. So the models find genuine signal, but nobody is predicting a coin flip, and nobody is predicting with certainty.
The lines for a fight are computed BEFORE the fight is simulated, from the skill gap alone, so a result can never leak into its own odds.

    synth.generate(today, size="normal", seed=7)  ->  Synthetic
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

URL = "https://demo.invalid"                 # a reserved domain that can never be a real site
WEIGHT_CLASSES = [("Flyweight", 165, .10), ("Bantamweight", 170, .13), ("Featherweight", 175, .13), ("Lightweight", 177, .17), ("Welterweight", 181, .15),
                  ("Middleweight", 185, .10), ("Light Heavyweight", 188, .07), ("Heavyweight", 192, .05),
                  ("Women's Strawweight", 162, .04), ("Women's Flyweight", 165, .03), ("Women's Bantamweight", 168, .03)]
WC_NAMES = [w[0] for w in WEIGHT_CLASSES]
WC_P = np.array([w[2] for w in WEIGHT_CLASSES]) / sum(w[2] for w in WEIGHT_CLASSES)
FIRST = ["Ka", "Ro", "Mi", "Da", "Vel", "Tor", "Ney", "Sal", "Bri", "Zan", "Lu", "Kor", "Ma", "Ten", "Ari", "Pa", "Gre", "Ju", "Oli", "Fen", "Ba", "Quin", "Ser", "Hal", "Wen", "Ivo", "Tal", "Dre"]
FIRST_END = ["", "n", "do", "vin", "ko", "ric", "mar", "ton", "sh", "lan", "za", "ek"]
LAST = ["Vask", "Dalm", "Brok", "Ferr", "Quel", "Morv", "Tesh", "Orl", "Hask", "Prin", "Zeld", "Krav", "Nolt", "Wyr", "Sarn", "Belk", "Tund", "Ghes", "Rav", "Ulm", "Jorv", "Camb", "Dusk", "Elm"]
LAST_END = ["ov", "ez", "son", "ini", "ak", "ward", "ides", "berg", "ante", "oux", "enko", "ley", "ard"]
# The betting-market model will not train on fewer than ~1,000 fights with lines (a safeguard in model_v3.py), so only "small" and "normal" have one.
SIZES = {"tiny": dict(years=4, events=14, fights=(7, 9)), "small": dict(years=10, events=15, fights=(9, 12)), "normal": dict(years=13, events=16, fights=(9, 12))}
MARKET_MIN_FIGHTS = 1400    # the market model needs 1,000 fights to learn from AFTER the 300 the winner model skips; this leaves a margin
LIVE = ("L1", "L2")
MIN_REST_DAYS = 49
# How noisy the world is. These were set so the demo resembles the real project's accuracy ranges (a winner model around 60%, an opening line around 65%,
# and a blend that edges the line); they say nothing about how well the real models predict real fights.
GAP_SCALE = 1.30            # how decisively hidden skill decides a fight
STAT_EDGE = 3.0             # how much of the skill gap shows up in the per-fight statistics the models read
MARKET_SEES = 0.85          # the share of the true gap the betting market sees...
MARKET_NOISE = 0.55         # ...and how noisy its opening price is


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _hex(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


@dataclass
class Fighter:
    fid: int
    name: str
    wc: str
    height: float
    reach: float
    stance: str
    dob: date
    skill: float                  # hidden skill at the peak
    peak_age: float
    striking: float               # 0..1: a grappler (0) to a striker (1)
    power: float
    chin: float
    cardio: float
    fights: int = 0
    last: date | None = None
    form: float = 0.0
    retired: bool = False
    url: str = ""

    def skill_at(self, d: date) -> float:
        age = (d - self.dob).days / 365.25
        return self.skill - 0.045 * max(0.0, age - self.peak_age) ** 1.5 - 0.05 * max(0.0, self.peak_age - 4 - age) ** 1.5 + self.form


@dataclass
class Card:
    key: str
    date: date
    csv: pd.DataFrame                            # the card file: event_name, event_date, bout_order, weight_class, title_fight, fighter_a, fighter_b
    odds: pd.DataFrame                           # the current lines: fighter_a, fighter_b, odds_a, odds_b (American)
    results: tuple | None = None                 # (fights, fighter_fights) for a card that has already happened


@dataclass
class Synthetic:
    fights: pd.DataFrame
    fighter_fights: pd.DataFrame
    odds: pd.DataFrame
    cards: dict = field(default_factory=dict)
    fighters: list = field(default_factory=list)


class _World:
    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)
        self.fighters: list[Fighter] = []
        self.names: set[str] = set()

    def name(self) -> str:
        r = self.rng
        for _ in range(500):
            n = f"{r.choice(FIRST)}{r.choice(FIRST_END)} {r.choice(LAST)}{r.choice(LAST_END)}"
            if n not in self.names:
                self.names.add(n)
                return n
        raise RuntimeError("ran out of names")

    def new_fighter(self, when: date, wc: str | None = None, seasoned: bool = False) -> Fighter:
        r = self.rng
        wc = wc or str(r.choice(WC_NAMES, p=WC_P))
        base_h = next(w[1] for w in WEIGHT_CLASSES if w[0] == wc)
        h = float(np.round(r.normal(base_h, 4.5)))
        age = float(r.uniform(25, 34)) if seasoned else float(np.clip(r.normal(27.5, 3.2), 21, 38))
        f = Fighter(fid=len(self.fighters), name=self.name(), wc=wc, height=h, reach=float(np.round(h + r.normal(3.0, 4.0))),
                    stance=str(r.choice(["Orthodox", "Southpaw", "Switch"], p=[.74, .21, .05])), dob=when - timedelta(days=int(age * 365.25)),
                    skill=float(r.normal(0, 1.0)), peak_age=float(r.normal(28.5, 1.8)), striking=float(r.beta(3, 3)), power=float(r.beta(2.5, 3.5)),
                    chin=float(r.beta(4, 3)), cardio=float(r.beta(4, 2.5)))
        f.url = f"{URL}/fighter-details/{_hex('fighter', f.fid, f.name)}"
        self.fighters.append(f)
        return f


def true_gap(a: Fighter, b: Fighter, d: date) -> float:
    """The hidden truth about a matchup, on the log-odds scale (positive: a is better). Used for the result AND (partly seen) by the betting market."""
    style = 0.25 * (a.striking - b.striking) * (a.skill - b.skill)
    reach = 0.012 * (a.reach - b.reach) + (0.10 if a.stance == "Southpaw" and b.stance == "Orthodox" else -0.10 if b.stance == "Southpaw" and a.stance == "Orthodox" else 0.0)
    return GAP_SCALE * (a.skill_at(d) - b.skill_at(d) + style + reach)


def _how_it_ended(w: _World, win: Fighter, lose: Fighter, rounds: int):
    r = w.rng
    p_finish = float(np.clip(0.20 + 0.30 * win.power + 0.20 * (1 - lose.chin) - 0.10 * lose.cardio + (0.05 if rounds == 3 else -0.04), 0.12, 0.80))
    if r.random() < 0.004:
        return "DQ", 1, f"0:{int(r.integers(10, 59)):02d}", 1.0
    if r.random() < p_finish:
        method = "KO/TKO" if r.random() < 0.35 + 0.45 * win.striking else "Submission"
        if method == "KO/TKO" and r.random() < 0.05:
            method = "TKO - Doctor's Stoppage"
        rnd = int(min(rounds, r.geometric(0.48 if rounds == 3 else 0.40)))
        sec = int(r.integers(8, 300))
        return method, rnd, f"{sec // 60}:{sec % 60:02d}", ((rnd - 1) * 300 + sec) / 60.0
    return str(r.choice(["Decision - Unanimous", "Decision - Split", "Decision - Majority"], p=[.75, .20, .05])), rounds, "5:00", rounds * 5.0


def _simulate(w: _World, a: Fighter, b: Fighter, d: date, event: str, wc: str, flagged_title: bool, rounds: int, gap: float):
    """One fight: its result and each fighter's statistics. `gap` was fixed BEFORE the fight, so the result cannot have leaked into any odds."""
    r = w.rng
    a_wins = r.random() < float(np.clip(_sigmoid(gap), 0.03, 0.97))                     # nobody is a certainty
    win, lose = (a, b) if a_wins else (b, a)
    result, rnd, ftime, minutes = _how_it_ended(w, win, lose, rounds)
    furl = f"{URL}/fight-details/{_hex('fight', d, a.fid, b.fid)}"
    fight = dict(fight_url=furl, event_date=d, event_name=event, weight_class=wc, title_fight=bool(flagged_title), num_rounds=float(rounds), result=result,
                 finish_round=float(rnd), finish_time=ftime)
    minutes = max(minutes, 0.5)
    rows = {}
    for me, opp, side in ((a, b, 1), (b, a, 2)):
        edge = float(np.clip(me.skill_at(d) - opp.skill_at(d), -2, 2))
        won = me is win
        pace = max(4.0, 17 + 9 * me.striking + 1.5 * STAT_EDGE * edge + r.normal(0, 2.5))
        sig_att = max(2, int(r.poisson(pace * minutes * (0.5 if result == "DQ" else 1.0))))
        sig_land = int(r.binomial(sig_att, float(np.clip(0.40 + 0.05 * me.striking + 0.04 * STAT_EDGE * edge + r.normal(0, 0.04), 0.2, 0.7))))
        td_att = int(r.poisson((0.9 + 2.6 * (1 - me.striking)) * minutes / 5))
        td_land = int(r.binomial(td_att, float(np.clip(0.32 + 0.05 * STAT_EDGE * edge + 0.06 * (1 - me.striking), 0.08, 0.7))))
        ctrl = int(np.clip(td_land * r.normal(75, 25) + (60 if won and result == "Submission" else 0), 0, minutes * 60 * 0.9))
        sub_att = int(r.poisson(0.35 * (1 - me.striking) * minutes / 5 + (1.2 if won and result == "Submission" else 0)))
        kd = int(r.poisson(0.18 * me.power * minutes / 5 + (0.9 if won and result == "KO/TKO" else 0)))
        clinch = int(sig_att * float(np.clip(0.12 + 0.10 * (1 - me.striking), 0.05, 0.3)))
        ground = int(sig_att * float(np.clip(0.05 + 0.12 * (1 - me.striking) + (0.1 if ctrl > 90 else 0), 0.02, 0.4)))
        rows[side] = dict(fight_url=furl, fighter_url=me.url, fighter_name=me.name, side=side, won=int(won), sig_land=sig_land, sig_att=sig_att, td_land=td_land, td_att=td_att,
                          sub_att=sub_att, kd=kd, ctrl_sec=ctrl, dist_att=sig_att - clinch - ground, clinch_att=clinch, ground_att=ground, sig_absorbed=0, opp_td_land=0, opp_td_att=0,
                          height_cm=me.height, reach_cm=me.reach, stance=me.stance, dob=me.dob)
    rows[1]["sig_absorbed"], rows[2]["sig_absorbed"] = rows[2]["sig_land"], rows[1]["sig_land"]
    rows[1]["opp_td_land"], rows[1]["opp_td_att"] = rows[2]["td_land"], rows[2]["td_att"]
    rows[2]["opp_td_land"], rows[2]["opp_td_att"] = rows[1]["td_land"], rows[1]["td_att"]
    return fight, [rows[1], rows[2]], win


def _saturdays(first: date, last: date):
    d = first + timedelta((5 - first.weekday()) % 7)
    while d <= last:
        yield d
        d += timedelta(7)


def american(p: float, vig: float = 1.045) -> int:
    """American odds for a win probability, with a bookmaker's margin added."""
    p = float(np.clip(p * vig, 0.03, 0.97))
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def _book(w: _World, gap: float) -> tuple[float, float]:
    """(opening, closing) probability that fighter a wins, as a market would price it: it sees the skill gap with some noise, and sharpens by the close."""
    r = w.rng
    opening = gap * MARKET_SEES + float(r.normal(0, MARKET_NOISE))
    closing = opening + 0.40 * (gap - opening) + float(r.normal(0, 0.10))
    return float(np.clip(_sigmoid(opening), 0.04, 0.96)), float(np.clip(_sigmoid(closing), 0.04, 0.96))          # real prices stop around 95%


def _matchmake(w: _World, pool: dict, d: date, n: int, booked: dict):
    """n pairs for the card on date d: similar skill, rested, never on two cards close together. A debutant now and then keeps fresh faces coming."""
    r = w.rng
    used, pairs = set(), []
    for _ in range(n):
        wc = str(r.choice(WC_NAMES, p=WC_P))
        ok = lambda f: (not f.retired and f.fid not in used and (f.last is None or (d - f.last).days >= MIN_REST_DAYS) and (d - f.dob).days / 365.25 < 41
                        and not any(abs((d - x).days) < MIN_REST_DAYS for x in booked.get(f.fid, ())))
        avail = [f for f in pool[wc] if ok(f)]
        if len(avail) < 2 or r.random() < 0.18:
            a = w.new_fighter(d, wc)
            pool[wc].append(a)
            avail = [f for f in pool[wc] if ok(f) and f.fid != a.fid]
        else:
            a = avail[int(r.integers(len(avail)))]
            avail = [f for f in avail if f.fid != a.fid]
        if not avail:
            continue
        gap = np.array([abs(f.skill_at(d) - a.skill_at(d)) for f in avail])
        p = np.exp(-gap / 0.8) + 1e-6
        b = avail[int(r.choice(len(avail), p=p / p.sum()))]
        used |= {a.fid, b.fid}
        pairs.append((a, b, wc))
    # The best pair headlines, and a main event features experienced fighters (at least three UFC fights each) whenever such a pair exists.
    strength = lambda x: x[0].skill_at(d) + x[1].skill_at(d)
    seasoned = [p for p in pairs if min(p[0].fights, p[1].fights) >= 3]
    ordered = sorted(pairs, key=lambda x: -strength(x))
    if seasoned:
        head = max(seasoned, key=strength)
        ordered = [head] + [p for p in ordered if p is not head]
    return ordered


def generate(today: date, size: str = "normal", seed: int = 7) -> Synthetic:
    cfg = SIZES[size]
    w = _World(seed)
    r = w.rng
    u1 = next(_saturdays(today + timedelta(days=2), today + timedelta(days=12)))           # the next card is at least 2 days away
    when = {"L1": u1 - timedelta(days=21), "L2": u1 - timedelta(days=14), "U1": u1, "U2": u1 + timedelta(days=7), "U3": u1 + timedelta(days=14)}
    first = when["L1"] - timedelta(days=int(365.25 * cfg["years"]))
    pool = {wc: [] for wc in WC_NAMES}
    for wc, _, share in WEIGHT_CLASSES:                                                       # an opening roster of established fighters
        for _ in range(max(4, int(round(70 * share * cfg["years"] / 6)))):
            pool[wc].append(w.new_fighter(first, wc, seasoned=True))
    saturdays = list(_saturdays(first, when["L2"]))
    chosen = set(r.choice(len(saturdays), size=min(len(saturdays), cfg["years"] * cfg["events"]), replace=False).tolist())
    history_dates = [d for i, d in enumerate(saturdays) if i in chosen and d not in (when["L1"], when["L2"])]
    timeline = history_dates + [when["L1"], when["L2"], when["U1"], when["U2"], when["U3"]]
    fights, ffs, odds = [], [], []
    live_rows = {k: ([], []) for k in LIVE}
    cards: dict[str, Card] = {}
    booked: dict[int, list] = {}
    quirk_years: set[int] = set()
    n_events = 100
    for d in timeline:
        n_events += 1
        key = next((k for k, v in when.items() if v == d), None)
        upcoming = d > when["L2"]
        pairs = _matchmake(w, pool, d, int(r.integers(*cfg["fights"])), booked)
        if not pairs:
            continue
        title_card = n_events % 3 == 0
        head_a, head_b, _ = pairs[0]
        event = f"Demo {'Championship' if title_card else 'Fight Night'} {n_events}: {head_a.name.split()[-1]} vs. {head_b.name.split()[-1]}"
        card_rows, card_odds = [], []
        for k, (a, b, wc) in enumerate(pairs):
            main = k == 0
            is_title = main and title_card
            tournament = (not main) and k == 2 and d.year not in quirk_years and r.random() < 0.20
            quirk_years |= {d.year} if tournament else set()
            rounds = 5 if main else 3
            gap = true_gap(a, b, d)
            if key:
                card_rows.append(dict(event_name=event, event_date=str(d), bout_order=k + 1, weight_class=wc, title_fight=bool(is_title), fighter_a=a.name, fighter_b=b.name))
                p_open, _ = _book(w, gap)
                card_odds.append(dict(fighter_a=a.name, fighter_b=b.name, odds_a=american(p_open), odds_b=american(1 - p_open)))
            if upcoming:
                for f in (a, b):
                    booked.setdefault(f.fid, []).append(d)
                continue
            fight, two, winner = _simulate(w, a, b, d, event, wc, is_title or tournament, rounds, gap)
            if key in LIVE:
                live_rows[key][0].append(fight)
                live_rows[key][1].extend(two)
            else:
                fights.append(fight)
                ffs.extend(two)
                p_open, p_close = _book(w, gap)
                odds.append(dict(fight_id=fight["fight_url"].rsplit("/", 1)[-1], event_date=d, f1_id=a.url.rsplit("/", 1)[-1], f2_id=b.url.rsplit("/", 1)[-1], f1_name=a.name, f2_name=b.name,
                                 has_odds="True", f1_open_prob_novig=p_open, f1_close_prob_novig=p_close))
            for f in (a, b):
                f.fights += 1
                f.last = d
                f.form = float(np.clip(0.55 * f.form + (0.12 if f is winner else -0.12) + r.normal(0, 0.05), -0.5, 0.5))
                if f.fights >= 4 and f.form < -0.2 and r.random() < 0.18:                    # a losing streak sometimes ends a career
                    f.retired = True
        if key:
            res = None if key not in LIVE else (pd.DataFrame(live_rows[key][0]), pd.DataFrame(live_rows[key][1]))
            cards[key] = Card(key, d, pd.DataFrame(card_rows), pd.DataFrame(card_odds), res)
    return Synthetic(fights=pd.DataFrame(fights), fighter_fights=pd.DataFrame(ffs), odds=pd.DataFrame(odds), cards=cards, fighters=w.fighters)
