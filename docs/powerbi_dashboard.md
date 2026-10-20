# Power BI dashboard guide: UFC prediction scoreboard

Three report pages built on the Postgres tables and views from the earlier steps:

| Page | Question it answers |
|---|---|
| 1. Scoreboard | How accurate has the model been, and is it improving or just lucky? |
| 2. Fight explorer | Which fights did it get right or wrong, and how confident was it? |
| 3. Fight breakdown | Why did the model favor one fighter in a given fight? |

I can't open Power BI from here, so this is written from how the program works. Menu names can shift slightly between versions.

**Requirements:** Power BI Desktop (free from Microsoft, Windows only). On a Mac, use a Windows VM or Microsoft's browser-based Power BI service, which has a more limited editor.

---

## Step 1: Connect to Postgres

1. **Home → Get data → PostgreSQL database.**
2. **Server:** your host (add `:5432` if asked for a port). **Database:** your database name.
3. Choose **Import** (the data is small, and Import is fastest).
4. On the credentials screen choose the **Database** tab and enter your username and password.
5. In the Navigator, tick:
   - `fight_predictions` (the one table you really need)
   - `confidence_buckets` (view)
   - `fighter_features` (view, for page 3)
6. Click **Transform data** (not Load) so you can shape `fighter_features` in Step 3.

**If it won't connect:**
- A provider or driver error usually links to the Npgsql install; install it and restart Power BI.
- Hosted databases (Neon, Supabase) require SSL, and each documents its own Power BI settings. Check their docs if the standard dialog fails.

**Refreshing:** after you load new fights and run the scoring script (see the README), press **Home → Refresh** in Power BI Desktop. Scheduled refresh in the Power BI service is optional and a later step.

---

## Step 2: Model and measures

### Add measures
Select the `fight_predictions` table, then **Table tools → New measure** for each one below. Format the percentages with **Measure tools → Format → Percentage**.

```DAX
Fights Scored = COUNTROWS(fight_predictions)

Correct Picks = SUM(fight_predictions[correct])

Accuracy = DIVIDE([Correct Picks], [Fights Scored])

Coin Flip = 0.5

Avg Confidence = AVERAGE(fight_predictions[confidence])

Last Event Accuracy =
VAR LatestEvent = MAX(fight_predictions[event_date])
RETURN
    CALCULATE(
        DIVIDE(SUM(fight_predictions[correct]), COUNTROWS(fight_predictions)),
        fight_predictions[event_date] = LatestEvent
    )

Running Accuracy =
VAR d = MAX(fight_predictions[event_date])
VAR upto = FILTER(ALL(fight_predictions[event_date]), fight_predictions[event_date] <= d)
RETURN
    DIVIDE(
        CALCULATE(SUM(fight_predictions[correct]), upto),
        CALCULATE(COUNTROWS(fight_predictions), upto)
    )
```

`Running Accuracy` is the accuracy over every fight up to each event date. Because it only clears the *date* filter, it still responds to other slicers such as weight class.

### Add a label column
Select `fight_predictions`, then **Table tools → New column**:

```DAX
Fight Label =
FORMAT(fight_predictions[event_date], "yyyy-mm-dd") & " | " &
fight_predictions[fighter_a] & " vs " & fight_predictions[fighter_b]
```

---

## Step 3 (optional, only for Page 3): Shape `fighter_features` (Power Query)

In the **Transform data** window, select the `fighter_features` query:

1. **Choose columns** (Home → Choose columns): keep only
   `fight_url`, `fighter_name`, `n_prev`, `prev_win_rate`, `streak`, `age`, `reach`, `slpm`, `sapm`, `sig_acc`, `td_per15`, `td_def`, `ctrl_share`.
2. Select the `fight_url` and `fighter_name` columns, then **Transform → Unpivot Columns → Unpivot Other Columns**.
3. Rename the new columns **Attribute → Stat** and **Value → Value**.
4. **Close & Apply.**

Then open **Model view** and check there's a relationship from `fight_predictions[fight_url]` to `fighter_features[fight_url]`: one-to-many, filter direction **single** (from `fight_predictions` to `fighter_features`). Create it by dragging the column if it wasn't detected.

---

## Page 1: Scoreboard

| Visual | Setup |
|---|---|
| **Cards (4)** | `Fights Scored`, `Accuracy`, `Last Event Accuracy`, `Avg Confidence` |
| **Line chart: running accuracy** | X-axis: `event_date`. Y-axis: `Running Accuracy`. Add `Coin Flip` as a second line (or use the **Analytics pane → Constant line** at 0.5). |
| **Column chart: accuracy per event** | X-axis: `event_date`. Y-axis: `Accuracy`. Tooltip: `Fights Scored`. |
| **Bar chart: confidence buckets** | Use the `confidence_buckets` view: axis `confidence_bucket`, values `accuracy`, tooltip `fights`. |
| **Text box** | See the note below |

**Text box for honesty:** *"Scored fights only: both fighters have 3+ prior UFC fights (about 45% of all fights). Each prediction uses only data from before the event. Long-run accuracy is around 63%; recent seasons have been higher."*

**Reading the charts:**
- Single events have only 6 to 12 scored fights, so per-event accuracy swings wildly. Lead with the running line and the totals, not individual events.
- The confidence bar chart should rise from left to right. If it does, the probabilities carry real information.

---

## Page 2: Fight explorer

1. **Slicers:** `event_date` (Between), `weight_class` (dropdown), `confidence` (Between), `correct` (tile or dropdown).
2. **Table** with columns: `event_date`, `fighter_a`, `fighter_b`, `predicted_winner`, `confidence`, `actual_winner`, `correct`.
3. **Colour the results:** select the table → Format visual → Cell elements → turn on **Background color** for the `correct` column → Rules: `1` = light green, `0` = light red.
4. **Sort** by `event_date` descending so the latest fights are at the top.

**Worth checking:**
- Filter `confidence` to 70% or higher and see which fights the model got wrong.
- Filter by weight class to see where it works best and worst. Treat small groups with caution.

---

## Page 3: Fight breakdown ("why this pick?") - optional, not part of the published dashboard

*The finished dashboard in this project has two pages (Scoreboard and Fight explorer). This third page is an extra you can add if you want it.*

1. **Slicer:** `Fight Label` (dropdown, single select).
2. **Matrix:**
   - Rows: `Stat`
   - Columns: `fighter_name`
   - Values: `Value` (use **Sum**; each cell has exactly one value)
3. **Cards** above the matrix: `predicted_winner`, `confidence`, `actual_winner`, set to show the **First** value.
4. Format the matrix so `age`, `streak` and `n_prev` display as whole numbers, and `prev_win_rate`, `sig_acc`, `td_def` and `ctrl_share` as percentages (set formats per row using the Stat names, or just state units in a footnote).

**What to look for:** the strongest signals in the model were age, strikes absorbed per minute (`sapm`), takedowns per 15 minutes (`td_per15`) and current streak. Compare those rows for the fight you've picked and the model's choice should usually make sense.

---

## Design notes
- Use one theme for all pages: **View → Themes**.
- Write titles that state the takeaway, such as "Running accuracy vs. a coin flip", rather than "Chart 1".
- Always show the number of fights next to any accuracy.
- Keep a fixed number format across the report: percentages with one decimal.

## For your portfolio README
Include 2 or 3 screenshots (Scoreboard, Explorer) and report the numbers honestly:
- **Recent test window (2025 to now):** about 67%.
- **Long-run walk-forward (2016 to now):** about 63%.
- **Simple baseline** (pick the fighter with the better career win rate): about 62% on the recent test window.
- Note that fight prediction is noisy and these gaps over the baseline are modest.

Honest, well-explained numbers are more convincing to a hiring manager than an inflated headline accuracy.
