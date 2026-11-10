"""
pipeline/hosted_db.py: the one rule the demo builder, the restore drill and the recovery rehearsal share for refusing a hosted database. It replaced three substring tests, one of which CodeQL flagged
as incomplete URL substring sanitization. It matches whole domain labels; every tool that uses it also refuses a database that already holds tables of its own, which is the real protection.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import hosted_db  # noqa: E402


@pytest.mark.parametrize("host", [
    "ep-cool-123.us-east-2.aws.neon.tech", "NEON.TECH", "neon.tech.", "a.b.c.neon.tech",                                    # the project's own provider, in any case, with a trailing dot
    "db.abcd.supabase.co", "aws-0-us-east-1.pooler.supabase.com", "supabase",                                              # a whole label, whatever the ending
    "mydb.cluster-xyz.us-east-1.rds.amazonaws.com", "x.rds.amazonaws.com.cn",
    "dpg-123.oregon-postgres.render.com", "xyz.db.elephantsql.com", "srv.postgres.database.azure.com", "sql.googleapis.com"])
def test_a_hosted_database_service_is_recognised(host):
    assert hosted_db.looks_hosted(host) is True


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "::1", "myserver.local", "db.internal.example.org", "/var/run/postgresql", "",                      # this computer, a private name, a socket folder
    "   ", None,                                                                                                          # nothing to judge
    "evilneon.tech", "neon.tech.example.org", "notamazonaws.com", "supabase-thing.example.com", "my-elephantsql-proxy.example.com"])   # contains a provider's name but is not that provider's domain
def test_anything_else_is_not_called_hosted_here(host):
    assert hosted_db.looks_hosted(host) is False


def test_the_three_tools_use_this_one_rule_and_keep_no_list_of_their_own():
    builder = (ROOT / "demo" / "build_demo.py").read_text(encoding="utf-8")
    drill = (ROOT / "pipeline" / "restore_drill.py").read_text(encoding="utf-8")
    assert "import hosted_db" in builder and "hosted_db.looks_hosted(host)" in builder
    assert "import hosted_db" in drill and "hosted_db.looks_hosted(host)" in drill and "HOSTED = (" not in drill
    assert "restore_drill.assert_safe(engine)" in (ROOT / "pipeline" / "recover.py").read_text(encoding="utf-8")                # the recovery rehearsal goes through the drill's guard
    for name, text in (("build_demo", builder), ("restore_drill", drill)):
        assert not re.search(r'"[a-z0-9.-]+\.(tech|com|co|net)" in ', text), name                                              # no "domain" in host test is left


def test_the_tools_still_refuse_a_hosted_host_before_looking_at_the_database():
    import sqlalchemy as sa
    sys.path.insert(0, str(ROOT / "demo"))
    import build_demo
    import restore_drill
    for host in ("ep-cool-123.us-east-2.aws.neon.tech", "db.abcd.supabase.co"):
        engine = sa.create_engine(f"postgresql+psycopg2://u:p@{host}/db")                                                      # never connected: the refusal comes first
        with pytest.raises(build_demo.BuildError, match="looks like a hosted database"):
            build_demo.assert_safe_to_build(engine)
        with pytest.raises(SystemExit, match="looks like a hosted database"):
            restore_drill.assert_safe(engine)


def test_the_runbook_records_the_alerts_and_how_to_triage_the_next_one():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in ("## Code scanning (CodeQL) alerts", "Incomplete URL substring sanitization", "Used in tests", "pipeline/hosted_db.py", "tests/test_hosted_db.py", "whole domain labels"):
        assert needle in runbook, needle
