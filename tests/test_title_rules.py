"""What counts as a championship fight: the tournament bouts the source also flags as 'title' must not be shown as championships."""
import sys
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import title_rules as tr  # noqa: E402

CASES = [  # (title_fight, num_rounds, event_date, championship, tournament)
    (True, 5, "2026-01-31", True, False),        # Volkanovski vs Lopes 2
    (True, 3, "2026-01-31", False, True),        # a Road to UFC final on the same card
    (True, 3, "2013-04-13", False, True),        # a The Ultimate Fighter finale bout
    (True, 5, "2001-01-01", True, False),        # the first day of the modern era counts as modern
    (True, 3, "2000-12-31", True, False),        # before 2001 the source's flag is left as it is
    (True, 1, "1997-05-30", True, False),
    (True, None, "1994-03-11", True, False),     # UFC 2: unknown rounds, old era
    (True, None, "2012-06-01", False, True),     # unknown rounds in the modern era: not a championship
    (False, 5, "2026-01-31", False, False),      # a five-round main event that is not a title fight
    (False, 3, "2026-01-31", False, False),
    (None, 5, "2026-01-31", False, False),       # a missing flag is not a title
    (None, None, "1990-01-01", False, False),
]


def test_a_flagged_five_round_fight_is_a_championship_and_a_flagged_three_round_fight_is_a_tournament_bout():
    t, n, d = zip(*[(c[0], c[1], c[2]) for c in CASES])
    champ, tour = tr.is_championship(t, n, d), tr.is_tournament(t, n, d)
    assert champ.tolist() == [c[3] for c in CASES] and tour.tolist() == [c[4] for c in CASES]
    assert not (champ & tour).any()                                                          # never both


def _db(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 't.db'}")
    df = pd.DataFrame([dict(fight_url=f"f{i}", title_fight=c[0], num_rounds=c[1], event_date=c[2]) for i, c in enumerate(CASES)])
    df["title_fight"] = df.title_fight.map(lambda v: None if v is None else int(v))
    df.to_sql("fights", eng, index=False)
    return eng


def test_the_sql_the_website_runs_agrees_with_the_python_rule_on_every_edge_case(tmp_path):
    eng = _db(tmp_path)
    q = pd.read_sql(sa.text(f"SELECT f.fight_url, {tr.championship_sql()} AS c, {tr.tournament_sql()} AS t FROM fights f ORDER BY f.fight_url"), eng)
    q = q.set_index("fight_url").loc[[f"f{i}" for i in range(len(CASES))]]
    assert q.c.astype(bool).tolist() == [c[3] for c in CASES] and q.t.astype(bool).tolist() == [c[4] for c in CASES]
    assert set(q.c.unique()) <= {0, 1} and set(q.t.unique()) <= {0, 1}                       # a real true or false, never a missing value (a missing value would read as true on the page)


def test_the_past_events_order_puts_the_real_main_event_first_not_the_tournament_finals(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'ufc325.db'}")
    rows = [("Lawrence Lui def. Sulangrangbo", 1, 3), ("Keiichiro Nakamura def. Szalay", 1, 3), ("Dom Mar Fan def. Kim", 1, 3), ("Jacob Malkoun def. Finney", 0, 3),
            ("Volkanovski def. Lopes", 1, 5), ("Hooker def. Saint-Denis", 0, 5), ("Ofli def. Yizha", 0, 3)]
    pd.DataFrame([dict(fight_url=name, title_fight=t, num_rounds=n, event_date="2026-01-31") for name, t, n in rows]).to_sql("fights", eng, index=False)
    got = pd.read_sql(sa.text(f"SELECT f.fight_url, {tr.championship_sql()} AS championship, {tr.tournament_sql()} AS tournament FROM fights f ORDER BY {tr.order_sql()}, f.fight_url"), eng)
    assert got.fight_url.iloc[0] == "Volkanovski def. Lopes" and bool(got.championship.iloc[0])                  # the title fight first
    assert got.fight_url.iloc[1] == "Hooker def. Saint-Denis"                                                    # then the other five-round main event
    assert set(got.fight_url.iloc[2:5]) == {"Lawrence Lui def. Sulangrangbo", "Keiichiro Nakamura def. Szalay", "Dom Mar Fan def. Kim"}      # then the tournament finals
    assert got.championship.astype(bool).sum() == 1 and got.tournament.astype(bool).sum() == 3                   # one real title fight, three tournament bouts


def test_the_website_uses_the_shared_rule_and_no_longer_sorts_or_tags_by_the_raw_flag():
    src = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "import title_rules" in src and "title_rules.championship_sql()" in src and "title_rules.tournament_sql()" in src and "title_rules.order_sql()" in src
    assert "ORDER BY f.title_fight DESC" not in src
    assert "if r.championship else" in src and "Tournament</span>" in src


def test_the_rule_is_stated_plainly_where_a_reader_will_look():
    doc = (ROOT / "pipeline" / "title_rules.py").read_text(encoding="utf-8")
    assert "Road to UFC" in doc and "championship = flagged and scheduled for five rounds" in doc
    assert "tournament" in (ROOT / "pipeline" / "model_distance.py").read_text(encoding="utf-8").split("from __future__")[0]
