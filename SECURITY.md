# Security policy

This is a solo portfolio project. It has no user accounts apart from a small friends-only game, and it stores nothing personal beyond that game's usernames and salted scrypt hashes of
its PINs (the PINs themselves are never stored).

## Reporting a vulnerability

Please report it **privately**, not in a public issue: on the repository page choose **Security**, then **Report a vulnerability** (GitHub's private vulnerability reporting). I will aim
to reply within a week and to fix real problems quickly, but this is a project maintained by one person, so I cannot promise more than that.

## What is in scope

- The code in this repository and the live site at https://ufc-predictor.streamlit.app/.
- Anything that exposes a secret, lets a visitor read data the site is meant to keep private (the betting-line tables and the game's tables), or lets a visitor write to the database.

## What is out of scope

Third-party services (GitHub, Streamlit Community Cloud, Neon, Kaggle, The Odds API), denial of service by sheer volume, and social engineering.

## What the project already does

- The website's database login is read-only and cannot read the betting-line tables or the game's tables; an audit command checks that (`pipeline/db_roles.py`).
- Every file and the whole git history are scanned for credentials on every change (`tools/scan_secrets.py`), and no secret is ever printed by a workflow.
- Every dependency is pinned (`constraints-ci.txt`) and refreshed monthly by a workflow that runs the new versions in a job with read-only access and no secrets, and opens a pull
  request for review (`.github/workflows/refresh-lock.yml`).
- Workflows ask for the least access they need, nothing is spliced into a shell script, and every action is pinned to a major version and kept current by Dependabot.
- The live site is checked in a real browser every day, and the daily job reports silent failures (`.github/workflows/smoke-test.yml`, `pipeline/watchdog.py`).

## Supported versions

Only the latest commit on `main`.
