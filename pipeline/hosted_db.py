"""
Is this database host a hosted (cloud) service?

Three tools must only ever write into a scratch database on this computer: the demo builder, the restore drill and the one-command recovery rehearsal. Each refuses a host that looks like a hosted
database service (the project's real database is on Neon). They share this one function so that they cannot drift apart.

It matches WHOLE DOMAIN LABELS: `ep-cool-1.us-east-2.aws.neon.tech` is hosted because it ends in `.neon.tech`, and `db.abcd.supabase.co` because one of its dot-separated labels is `supabase`; but
`evilneon.tech` is not mistaken for Neon (and a name that merely contains a provider's name inside a longer word is not refused). This is a deny-list that fails safe, and it is only the first of two
guards: every one of those tools then also refuses any database that already holds tables that it did not create, so a host that slips past this list still cannot be damaged.
It replaces a plain substring test ("neon.tech" in host), which is the pattern code scanning rightly calls incomplete URL substring sanitization.
"""
from __future__ import annotations

HOSTED_DOMAINS = ("neon.tech", "amazonaws.com", "amazonaws.com.cn", "render.com", "azure.com", "googleapis.com")      # the host IS this domain or a sub-domain of it
HOSTED_LABELS = ("supabase", "elephantsql")                                                                           # one of the host's dot-separated labels IS this word


def looks_hosted(host) -> bool:
    """True when `host` (a database host name, as SQLAlchemy reports it) belongs to a hosted database service. An empty or missing host (a local socket) is not hosted."""
    name = str(host or "").strip().lower().rstrip(".")
    if not name:
        return False
    if any(name == domain or name.endswith("." + domain) for domain in HOSTED_DOMAINS):
        return True
    return any(label in HOSTED_LABELS for label in name.split("."))
