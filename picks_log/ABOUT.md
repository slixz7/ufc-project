# Public pick log

`locked_picks.csv` is an append-only record of every pick this project has locked. The daily GitHub job adds new picks to it and commits the file, so this repository's history shows **when each pick was made**. The file appears after the job's first real run.

## What is in it

| Column | Meaning |
|---|---|
| `pick_type` | `model-only` (made when a card is announced) or `market-informed` (locked two days before the event) |
| `bout_id` | the project's identifier for the bout |
| `event_date`, `event_name` | the event |
| `fighter_a`, `fighter_b` | the two fighters |
| `predicted_winner` | who the pick says wins |
| `p_a_wins` | the pick's probability that `fighter_a` wins |
| `locked_at_utc` | when the pick was locked in the database |
| `logged_at_utc` | when the pick was written to this file |

No betting odds are ever written here, only the picks.

## How to check it

In a clone of the repository (git is required):

```
python pipeline/pick_log.py verify
```

It reads the commit history and prints, for each event, how many picks were committed before the event date and the typical delay between a pick being locked and being published. On GitHub you can also open `picks_log/locked_picks.csv` and click **History** to see every commit with its time.

## What it proves, and what it does not

- **A pick that appears in a commit made before its event cannot have been chosen after the result.** That is the point of the log.
- **The first batch was published after the fact.** Picks locked before the log existed (for example UFC 333) were added on the log's first run. Their `locked_at_utc` is earlier than their commit time, and the verifier counts only picks committed before their event, so it does not give them credit they have not earned.
- **A commit time is evidence, not mathematical proof.** For commits made by the GitHub job it is GitHub's clock. The repository owner could in principle rewrite history; protecting the `main` branch against force pushes makes that visible and harder, and anyone is free to clone the repository and keep their own copy.
- **The log never changes a past row.** The job also compares every logged pick with the database each run and raises an alert if one differs.
