"""SECURITY.md: how to report a problem privately, and what the project already does. It must not promise what it cannot deliver, must not invent a contact, and must not describe a protection that is not there."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
FLAT = re.sub(r"\s+", " ", POLICY)                                                                                          # the file wraps its lines; a sentence may span a break
README = (ROOT / "README.md").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")


def test_it_has_the_sections_github_and_a_reader_expect_in_order():
    heads = re.findall(r"^#{1,2} (.+)$", POLICY, flags=re.M)
    assert heads == ["Security policy", "Reporting a vulnerability", "What is in scope", "What is out of scope", "What the project already does", "Supported versions"]


def test_it_says_to_report_privately_through_github_and_invents_no_contact_and_promises_no_reward():
    assert "**privately**" in POLICY and "Report a vulnerability" in POLICY and "private vulnerability reporting" in POLICY
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", POLICY) and not re.search(r"https?://[^\s)]*(mailto|form)", POLICY)            # no email address: only GitHub's own channel
    low = POLICY.lower()
    assert "bounty" not in low and "reward" not in low and "guarantee" not in low
    assert "aim to reply within a week" in FLAT and "cannot promise more than that" in FLAT                                      # an aim, honestly hedged


def test_the_live_address_is_the_one_the_readme_gives():
    url = re.search(r"https://ufc-predictor\.streamlit\.app/", POLICY).group(0)
    assert url in README


def test_every_file_it_names_exists_and_every_claim_about_the_code_is_true():
    for path in re.findall(r"`((?:pipeline|tools|\.github)/[\w./-]+|constraints-ci\.txt)`", POLICY):
        assert (ROOT / path).exists(), path
    game = (ROOT / "pipeline" / "game_core.py").read_text(encoding="utf-8")
    assert "hashlib.scrypt" in game and "compare_digest" in game and "The PIN itself is never stored" in game                          # "salted scrypt hashes ... the PINs themselves are never stored"
    roles = (ROOT / "pipeline" / "db_roles.py").read_text(encoding="utf-8")
    assert all(t in roles for t in ('"fight_odds"', '"upcoming_odds"', '"game_players"', '"game_picks"'))                             # the private tables it says the site cannot read
    assert "no secret is ever printed" in FLAT and (ROOT / "tools" / "scan_secrets.py").exists()


def test_the_readme_points_to_it_and_the_runbook_lists_the_settings_that_make_it_work():
    assert "[SECURITY.md](SECURITY.md)" in README
    for needle in ("## Security settings to turn on", "Private vulnerability reporting", "Code scanning", "Default setup", "Secret scanning", "push protection", "settings, not files"):
        assert needle in RUNBOOK, needle
