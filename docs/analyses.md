# Analyses

What each chart computes and the research behind it. Running code is in `app/modules/running/analysis/` (on ingest) and `reports.py` (at read time); recovery is in `app/modules/recovery/reports.py`.

## Running

- **Sample weighting:** each sample's dt is capped at 10 s (longer gaps are pauses). Samples that aren't moving or have no HR get weight 0.
- **Steady-state HR at pace** (`analysis/steady.py`): a minute counts as steady if at least 90% of it was moving, speed CV ≤ 8%, |grade| ≤ 2%, speed ≥ 1.5 m/s, it's past a 10-minute warm-up, and the previous minute was also stable and within 5% of its pace. Steady minutes are binned into 5 s/km bands; charts show avg HR per period for the most-run bands.
- **Pace profile:** the same bins summed over a range: minutes, avg HR, avg cadence per band.
- **Aerobic efficiency:** EF = avg speed (m/min) / avg HR, weighted by moving time per period. Decoupling compares EF in the first vs second half after warm-up, for runs ≥ 45 min.
- **Training load** (`analysis/load.py`): Banister TRIMP per activity. CTL (42-day) and ATL (7-day) are exponential averages of daily TRIMP; TSB is yesterday's CTL − ATL. Runs without streams use an estimate from summary avg HR.
- **Best efforts** (`analysis/efforts.py`): fastest moving time over 400 m to marathon within a run, from the distance stream resampled to 1 s on a moving clock. Stops don't count, but distance covered during a stop or gap is timed at walking speed (1.5 m/s), so a GPS jump can't make an effort faster.
- **GAP:** moving time per flat-equivalent km, using the Minetti (2002) energy-cost polynomial on `grade_smooth` (clipped to ±45%). Generous on steep downhills.
- **Cadence:** steps per minute averaged over moving samples with cadence > 0.
- **Long-run spikes:** a run more than 10% longer than the longest run in the prior 30 days (Frandsen et al., BJSM 2025, which links single-session spikes to injury risk more than weekly-volume ratios). The Plan page turns this into a cap: the longest run on a given day that wouldn't be a spike, based on runs already done.
- **Easy/hard balance:** Seiler 3-zone shares from zone time (Z1–2 low, Z3 moderate, Z4–5 high). Approximates lactate thresholds with %max zones.
- **Monotony and strain** (Foster 1998): per Mon–Sun week, monotony = mean daily TRIMP / SD (population, rest days included); strain = weekly load × monotony. Flag at monotony > 2.0.
- **Race estimate:** Riegel (t₂ = t₁ × (d₂/d₁)^1.06) on the fastest best effort of a mile or longer in the prior 90 days. Shorter efforts are excluded because Riegel overpredicts endurance from them. From training runs, it shows demonstrated fitness, not a race prediction.
- **HR vs expected:** each run's avg HR minus a speed→HR line fit on the prior 90 days of runs (≥ 10 runs). Summary data only.
- **Consistency calendar:** daily grid per year; levels from the range's run-distance quartiles.

## Recovery

- **HRV normal range:** Garmin's `balancedLow`–`balancedUpper` baseline. Without one, mean ± 1 SD of the prior 60 days (≥ 14 nights).
- **Night-to-night variation:** 7-day CV of ln HRV (sample SD, needs 4 of 7 nights), following Plews et al.
- **Resting HR:** only days with a sleep row, or measured stress and moving Body Battery.
- **Next-morning response:** HRV and RHR on day D against TRIMP on D−1, bucketed rest / easy / hard (≥ 75th percentile of training days).
- **Morning readiness:** each run's HR vs expected against that morning's HRV state (below, in or above range) and sleep score (under 60, 60–79, 80+).
