# Try it yourself: the demo

Run the whole project on your own computer in about two minutes, with **no accounts, no credentials and nothing to install except Python packages**.

```
pip install -r demo/requirements.txt
python demo/run_demo.py
```

The first run takes about a minute while it builds; after that it starts in seconds. Your browser opens at `http://localhost:8501`.

![The demo's Upcoming page: an invented card, its locked picks and the reasons behind them](../docs/images/demo-upcoming.png)

## What is real and what is invented

**Invented: all of the raw material.** Every fighter, fight, result and betting line. The names are made up from syllables, the fights never happened, and the web addresses in the data use a reserved domain (`demo.invalid`) that can never be a real site. Nothing is copied from any real source, so there is nothing to license, scrape or download.

**Real: everything built from it.** The demo feeds that raw material to the project's own pipeline, command by command, and then opens the project's own website on the result:

| Stage | What runs |
|---|---|
| Feature views | `sql/features.sql` and `sql/predictions.sql`, unchanged |
| Winner model | `model_v2.py`: a walk-forward pick for every past fight, each using only earlier fights |
| Betting-market model | `model_v3.py`: loads the historical lines, backtests, blends model and market |
| Goes-the-distance model | `model_distance.py` |
| Goes-the-distance with the betting market | `model_distance_v2.py`: the same question with the favourite's strength from the line, locked beside the first |
| Locking and grading | `upcoming.py` and each model's `predict` and `grade`, replayed on two cards that "have already happened" |
| The record | `bout_results.py`, the "Why this pick?" reasons, the Track record page |
| The friends' game | The real game tables, scoring and page, with six fictional players |
| Security | The real read-only and game database logins, and the real permission audits (the build stops if they fail) |

So the demo is not a mock-up with screenshots; it is the project, running on made-up data.

## Why made-up data, and what its numbers mean

Real fight data comes with licensing and terms-of-use questions that a public repository should not carry. The demo needs nothing from the outside world: no Kaggle, no odds feed, no hosted database.

**One more thing the generator does on purpose:** fights between very unequal fighters end in a finish more often, as heavy favourites do in real fights, so the market-informed distance prediction has something to find. At the demo's size the improvement is too small to confirm, which is true of the real thing too.

**Please do not read the demo's accuracy as the project's accuracy.** The generator was set up so the demo resembles the real project's ranges (a winner model around 60%, an opening line around 65%, a market that is a little sharper than the statistics), so that the pages look like the real thing. That says nothing about how well the real models predict real fights. For that, see the [README's results](../README.md#results-honestly) and the public, time-stamped [pick log](../picks_log/ABOUT.md).

## Using it

- **Pages:** Upcoming, Past events, Fighters, Track record and Game, exactly as on the real site, under a red DEMO banner.
- **The game:** sign in as `demo_ann`, `demo_bob`, `demo_cyd`, `demo_dee`, `demo_eli` or `demo_fay` with the PIN `123456`, or join as yourself with the invite code `demo`. Your picks are saved in the demo's own database.
- **Dates move with you.** Everything is measured from the day the demo was built, so there is always an upcoming card a few days away. A demo more than three days old is rebuilt automatically.

## Options

| Option | What it does |
|---|---|
| `--rebuild` | Throw the demo away and build a fresh one |
| `--port 8502` | Use another port |
| `--no-browser` | Do not open a browser tab |
| `--build-only` | Build the demo database and stop |
| `--size small` / `tiny` | A smaller, faster demo. `tiny` has no betting-market model (it needs about 1,000 fights to learn from, a safeguard in the real model), and the site copes with that |
| `--seed 8` | A different invented world |
| `--data-dir PATH` | Where the built-in database lives |
| `--database-url URL` | Use your own **empty** Postgres instead (see below) |

## Where the data lives, and how to remove it

The built-in database is a private Postgres that the launcher starts and stops itself. It lives in your system's cache folder (the first line the launcher prints says where), **never inside the project**: a database folder inside a synced folder such as OneDrive or Dropbox gets locked and damaged by the syncing. To remove the demo, delete that folder. Pressing Ctrl+C stops the website and the database.

## Using your own Postgres (for example Docker)

```
docker run --name ufc-demo-db -e POSTGRES_PASSWORD=demo -p 5433:5432 -d postgres:16
python demo/run_demo.py --database-url postgresql://postgres:demo@localhost:5433/postgres
```

## Safeguards: it cannot harm a real database

- It only ever builds into an **empty** database, or one it built before. Anything else is refused before a single change is made, with the names of the tables it found.
- It refuses hosted databases (Neon, Supabase, Amazon) whatever they contain.
- Database logins belong to the whole server, so the demo creates logins of its own, named after its database (`demo_site_<database>`, `demo_game_<database>`). It can never change a real project's `site_reader` or `game_app`, and two demos cannot break each other.
- **Nothing is reachable from outside your computer.** The website is started with `--server.address 127.0.0.1` (Streamlit's default is every network interface) and the built-in database listens only on `127.0.0.1` or a private socket, so other devices on your network cannot open the demo.
- `DEMO_MODE`, which switches on the banner and the sign-in hint, is set only by the launcher and appears nowhere in the real site's configuration.

## Troubleshooting

- **"The built-in database is not installed":** run `pip install -r demo/requirements.txt`. The built-in database ([pixeltable-pgserver](https://pypi.org/project/pixeltable-pgserver/)) has prebuilt packages for Windows, macOS and Linux for Python 3.12 and newer. On an older Python, use the Docker option above. I built and tested the demo on Linux; the Windows and macOS packages exist but I have not run the demo on them.
- **"Port 8501 is busy":** use `--port 8502`.
- **A firewall question on Windows:** the demo listens only on your own computer (`127.0.0.1`), so you can decline network access.
- **Something looks stale or odd:** `python demo/run_demo.py --rebuild`.

## For developers

`tests/test_demo.py` tests the invented data (deterministic, dated from today, the layouts the real loader fills, plausible results, no real names), the safeguards, and the launcher. On a machine with a Postgres server it also builds whole demos and opens every page of the real website on them; the Tests workflow does this on every push. `synth.py` holds the generator, `build_demo.py` the build, and `run_demo.py` the launcher.
