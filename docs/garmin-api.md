# Garmin Connect Personal API

Notes from building the `recovery` module. Tags:
- **[S]** observed in real API calls (2026-11-01)
- **[V]** documented, with the source below
- **[I]** inferred

Sources:
- [spec] OpenAPI 3.0.1 "Garmin Connect Personal API" v1.0.0, embedded in the JS bundle of https://developer.garmin.com/gc-developer-program/personal/swagger-docs/. The page renders client-side; to re-extract, find `JSON.parse('{"openapi"` in `component---src-pages-gc-developer-program-personal-swagger-docs-js-*.js`.
- [api] https://developer.garmin.com/gc-developer-program/personal/personal-api
- [faq] https://developer.garmin.com/gc-developer-program/personal/personal-faq/
- [afs] https://appsforstrava.com/blog/garmin-connect-developer-program-personal-api-key (secondary; quotes the Supplemental Terms)
- [5kr] https://the5krunner.com/2026/10/22/garmin-personal-api/ (secondary)

## Key

- **Creating it:** at https://connect.garmin.com/app/settings/personalApiToken. It's shown once [V api].
- **Expiry:** 30, 90, 180 or 365 days, with an email about a week before [V faq].
- **One key at a time:** a new key requires deleting the old one [V afs].
- **Format:** the key starts with `GCDC_` and is 133 characters long [S].
- **Where it lives:** `GARMIN_API_KEY` in `.env`.
- **Revocation:** deleting the key kills it and all its tokens [V faq].

## Token

```
POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token
Authorization: ApiKey <GARMIN_API_KEY>
(no Accept header, or Accept: */*)
```

- **Don't send `Accept: application/json`:** it returns **406** `NotAcceptableException` [S].
- **The response is the raw token**, not JSON: 200 `text/plain`, a JWT of about 1.1 KB [S].
  - Claims: `iss=https://diauth.garmin.com`, `exp`/`iat` 24 h apart.
  - `scope=[CONNECT_READ, PERSONAL_API_FITNESS_READ, PERSONAL_API_NUTRITION_READ, PERSONAL_API_USER_READ, PERSONAL_API_WELLNESS_READ]`.
- **Expiry:** read it from the JWT's `exp` (base64-decode the payload; no signature check needed).
- **Lifetime:** 24 h, or until the key expires, whichever is first. There's no refresh token; exchange the key again [V faq, S].
- **Data calls** use `Authorization: Bearer <jwt>` [S].
- **Error handling:**
  - Data call returns 401: re-exchange once and retry. If it fails again, treat the key as bad [V spec lists 401].
  - Exchange returns 401/403: the key is expired, revoked or wrong [I]. Stop calling until the key changes.
  - Data call returns 403: missing scope [V spec]. Same handling as above.
  - 429/503: not documented. Retry later [I].
  - Error bodies are JSON: `{"clientMessage", "errorId", "error"}` [S].

## Rate limits

- **Nothing published:** no 429 in the spec [V spec], and **no rate-limit headers on any response** [S].
- **Terms:** Garmin may "throttle, delay, reject, suspend, or otherwise restrict API requests without notice" [V afs].
- **Self-imposed limiter used here:** 6 per minute, 30 per 15 minutes, 300 per day.

## History

- **Window:** the earliest data returned was exactly 365 days before the request date [S]. Older ranges came back empty (HRV: 204). This may be a rolling 1-year window or just the start of the account's data; store data locally either way.
- **Gaps:** v2 summaries return rows only for days with data [S].

## Endpoints

Base URL: `https://personal.connectapi.garmin.com`. There are 26 GET endpoints, read-only and pull-only [V spec]. Dates are local `YYYY-MM-DD`.

| Use | Path | Params | Range cap |
|---|---|---|---|
| Daily summaries (RHR, stress, Body Battery) | `/usersummary-service/v2/dailysummaries` | `startDate`, `endDate` (query) | 1 year [V spec, S] |
| HRV nightly | `/hrv-service/hrv/daily/{fromDate}/{toDate}` | path; 204 = no data | not documented; **366 d worked** [S] |
| HRV one night + readings | `/hrv-service/hrv/{calendarDate}` | path; 204 = no data | 1 day |
| Sleep | `/sleep-service/sleep/dailySleepSummariesByDate` | `startDate`, `endDate` | 31 d [V spec] |
| Sleep one night | `/sleep-service/sleep/dailySleep` | `date` | 1 day |
| Daily HR curve + RHR | `/wellness-service/wellness/dailyHeartRate` | `date` | 1 day |
| Body Battery detail | `/wellness-service/wellness/bodyBattery/reports/daily` | `startDate`, `endDate`, `chunks` | 31 d [V spec] |
| Stress | `/usersummary-service/stats/stress/daily/{startDate}/{endDate}` | path | 28 d [V spec] |
| HR zones | `/biometric-service/heartRateZones/v2` | `generateDefaults` (default true) | n/a |
| Activity search | `/activitylist-service/activities/search/activities` | `start` (offset), `limit` (default 99), `startDate`/`endDate`, `activityType=running`, `sortOrder`... | paged |
| Activity detail | `/activity-service/activity/{activityId}` | path | n/a |
| Activity file | `/download-service/files/activity/{activityId}` | path; `application/x-zip-compressed`, "original format" | 1 activity |

Others: v1 single-day summary, steps (28 d), hydration (31 d), SpO2 acclimation, fitness age, weight, weight goals, nutrition, menstrual cycle, user settings, activity workouts, wellness activities.

## Response fields and gotchas

- **v2 daily** `{"data": [UserDailySummaryV2DTO]}`:
  - `heartRate.{restingValue,minValue,maxValue}` (bpm)
  - `stress.{avgLevel,maxLevel}` (0–100; **`-1` = no data** [S])
  - `bodyBattery.{minValue,maxValue,chargedValue,drainedValue,endOfDayValue}` (5–100)
  - `respiration.avgValue`, `pulseOx.avgValue`, `intensityMinutes`
  - **`wellnessChronology.totalDurationInMillis` is not wear time:** it was 24 h even on a day with 5 HR samples [S].
  - **`restingValue`** falls back to the "absolute minimum" when there's no real RHR [V spec]. It matched `dailyHeartRate.restingHeartRate` on every day compared [S], so that endpoint doesn't help.
  - **Treat RHR as valid only when that night has sleep data** (or `stress.avgLevel >= 0` and Body Battery min ≠ max) [I]. A barely worn day shows stress −1 and Body Battery min = max.
- **dailyHeartRate:** `restingHeartRate`, `lastSevenDaysAvgRestingHeartRate`, `heartRateValues` ([ts, bpm] rows; the sample count reveals wear [S]). It costs 1 call per day, so use it only for spot checks.
- **HRV** `{"hrvSummaries": [...]}`:
  - `calendarDate`, `lastNightAvg` (ms [I]), `lastNight5MinHigh`, `weeklyAvg`
  - `status` (`BALANCED`, `NONE` seen [S]; LOW/UNBALANCED/POOR [I])
  - `baseline.{lowUpper,balancedLow,balancedUpper,markerValue}`
  - **`lastNightAvg` is null on nights not worn, while `weeklyAvg`, `status` and `baseline` are still set** [S].
- **Sleep** `[DailySleepDTO]`:
  - **Placeholder rows:** days without sleep come back as rows with every field null, including `calendarDate`. Skip them [S].
  - **Timestamps** (`sleepStartTimestampGMT`, ...) are **epoch ms integers** [S].
  - Stage fields are in seconds: `sleepTimeSeconds`, `deepSleepSeconds`, `lightSleepSeconds`, `remSleepSeconds`, `awakeSleepSeconds`. Also `avgHeartRate`, `avgSleepStress`, `averageRespirationValue`.
  - **`sleepScores`** [S]: `overall.{value (0–100), qualifierKey}`, plus per-component `{value?, qualifierKey, optimalStart, optimalEnd}` for totalDuration, stress, awakeCount, remPercentage, lightPercentage, deepPercentage, restlessness...
  - **`sleepNeed`:** `{baseline, actual, feedback, trainingFeedback}`.
- **HRV timestamps** are ISO strings "without GMT offset or 'Z'" [V spec]. Parse `*GMT` as naive UTC.
- **Activity summary** (230 fields):
  - IDs and time: `activityId`, `beginTimestamp` (ms epoch UTC), `startTimeGMT` (string, no offset)
  - Size and HR: `duration`/`elapsedDuration` (s), `distance` (m), `averageHR`, `hrTimeInZone_1..5`
  - Running form: `avgPower` (W), `avgGroundContactTime` (ms), `avgVerticalOscillation` (cm), `avgStrideLength` (cm), `avgVerticalRatio`, `averageRunningCadenceInStepsPerMinute`
  - Load and fitness: `activityTrainingLoad`, `vO2MaxValue`
  - Power and running dynamics averages are in the summaries [S], so no FIT download is needed for per-run averages.

## Matching Garmin and Strava activities

- **Start times match exactly:** Garmin `beginTimestamp/1000` equals Strava's activity start time to the second, and distances match to the metre [S].
  - Join on start time (±60 s for safety), confirmed by distance within 2%.
- **Strava `SummaryActivity.external_id`** ("The identifier provided at upload time" [V developers.strava.com/swagger/activity.json]) is a fallback. Its format, `garmin_ping_<id>`, is [I].

## FIT files

- `/download-service/files/activity/{id}` returns a zip of the original file (FIT [I]). Garmin may strip some fields from API files (VO2max, recovery time, performance condition) [V 5kr].
- Per-second power and running dynamics need FIT parsing (`fitparse` or `garmin-fit-sdk`), at 1 call per run.

## Terms

- **Personal only:** "intended for individual Garmin users who want programmatic access to their own data", "not intended for commercial applications serving multiple end users" [V faq].
- **The key:** "may be used only by you"; don't sell or share it; breach can lead to suspension and revocation [V afs].
- **Storage and attribution:** not found in public docs. Check the terms shown at key creation.
