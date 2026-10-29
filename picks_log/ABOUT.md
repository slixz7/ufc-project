# Public pick log

Three append-only files record everything this project has locked before an event:

- **`locked_picks.csv`**: every winner pick (model-only and market-informed).
- **`locked_distance.csv`**: every goes-the-distance prediction (the chance a fight ends in a decision).
- **`locked_distance_market.csv`**: every market-informed goes-the-distance prediction, locked once a betting line exists. It has the same columns as `locked_distance.csv`; only the chance is written, never any odds.

The daily GitHub job adds new rows to them and commits the files, so this repository's history shows **when each one was made**.

## What is in them

`locked_picks.csv`

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

`locked_distance.csv`

| Column | Meaning |
|---|---|
| `bout_id`, `event_date`, `event_name`, `fighter_a`, `fighter_b` | the bout, as above |
| `scheduled_rounds` | 5 for a title fight or main event, otherwise 3 (the cards do not say, so this is inferred) |
| `p_distance` | the predicted chance the fight goes to a decision |
| `locked_at_utc`, `logged_at_utc` | as above |

No betting odds are ever written here, only the picks and predictions.

## How to check it

In a clone of the repository (git is required):

```
python pipeline/pick_log.py verify
```

It reads the commit history and prints, for each file and each event, how many rows were committed before the event date and the typical delay between a row being locked and being published. On GitHub you can also open either file and click **History** to see every commit with its time.

## What it proves, and what it does not

- **A row that appears in a commit made before its event cannot have been chosen after the result.** That is the point of the log.
- **The first batches were published after the fact.** Picks locked before the log existed (for example UFC 333) were added on the log's first run, and the goes-the-distance predictions were added a day after they were locked. Their `locked_at_utc` is earlier than their commit time, and the verifier counts only rows committed before their event, so it does not give them credit they have not earned.
- **A commit time is evidence, not mathematical proof.** For commits made by the GitHub job it is GitHub's clock. The repository owner could in principle rewrite history; protecting the `main` branch against force pushes makes that visible and harder, and anyone is free to clone the repository and keep their own copy.
- **The log never changes a past row.** The job also compares every logged row with the database each run and raises an alert if one differs.
