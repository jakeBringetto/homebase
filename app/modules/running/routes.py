"""Running pages: overview, load, performance, plan, activities, sync, settings, chart JSON."""

import logging
import secrets
import sqlite3
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.core import garmin, jobs
from app.core.charts import Series, chart, day_epoch, merge_points
from app.core.jobs import enqueue
from app.core.web import get_conn, render
from app.modules.running import config, plan, reports, storage, sync
from app.modules.running.analysis.steady import MIN_SPEED
from app.modules.running.config import AthleteSettings
from app.modules.running.fmt import M_PER_UNIT, Fmt
from app.modules.running.sources import strava

log = logging.getLogger(__name__)

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
RangeKey = Annotated[str | None, Query(alias="range")]
PAGE_SIZE = 50
HR_PACE_WIDTH_S = 15
STRAIN_TABLE_WEEKS = 26


def _ctx(conn: sqlite3.Connection) -> dict:
    athlete = storage.athlete_settings(conn)
    return {"athlete": athlete, "f": Fmt(athlete.units)}


def _error_msg(e: ValueError) -> str:
    errors = e.errors() if isinstance(e, ValidationError) else [{"msg": str(e)}]
    return "; ".join(err["msg"] for err in errors)


def _range(conn: sqlite3.Connection, key: str | None) -> tuple[reports.Range, date, date]:
    today = date.today()
    r = reports.get_range(key)
    return r, reports.range_start(conn, r, today), today


def _page(request: Request, conn: sqlite3.Connection, rng: str | None, template: str, build):
    """Render a range-filtered page; `build(ctx, range, start, today)` returns its fields."""
    ctx = _ctx(conn)
    r, start, today = _range(conn, rng)
    return render(
        request, f"running/{template}", **ctx, rng=r, ranges=reports.RANGES.values(),
        start=start, **build(ctx, r, start, today),
    )  # fmt: skip


@router.get("/")
def overview(request: Request, conn: Conn, rng: RangeKey = None):
    def build(ctx, r, start, today):
        return {
            "totals": reports.totals(conn, start),
            "periods": list(reversed(reports.periods(conn, start, r.table, today))),
            "calendar": reports.calendar(conn, start, today),
            "connected": _connected(conn),
        }

    return _page(request, conn, rng, "overview.html", build)


@router.get("/load")
def load_page(request: Request, conn: Conn, rng: RangeKey = None):
    def build(ctx, r, start, today):
        days, ctl, atl, tsb = reports.fitness(conn, ctx["athlete"], start, today)
        load_table = [(days[i], ctl[i], atl[i], tsb[i]) for i in range(len(days) - 1, -1, -7)]
        weeks, mono_7d = reports.weekly_strain(conn, ctx["athlete"], start, today)
        spikes = reports.run_spikes(conn, start)
        return {
            "totals": reports.totals(conn, start),
            "load_now": load_table[0] if load_table else None,
            "load_table": load_table,
            "spikes": [s for s in reversed(spikes) if (s["pct"] or 0) > reports.SPIKE_PCT],
            "weeks": list(reversed(weeks))[:STRAIN_TABLE_WEEKS],
            "mono_7d": mono_7d,
            "mono_high": reports.MONOTONY_HIGH,
        }

    return _page(request, conn, rng, "load.html", build)


@router.get("/performance")
def performance_page(request: Request, conn: Conn, rng: RangeKey = None):
    def build(ctx, r, start, today):
        f = ctx["f"]
        _, bands, periods = reports.hr_at_pace(
            conn, f.units, start, r.hr, today, HR_PACE_WIDTH_S, trim=r.days is None
        )
        hr_table = [(d, [s.values[i] for _, s in bands]) for i, d in enumerate(periods)]
        return {
            "bands": [f.band(b, HR_PACE_WIDTH_S) for b, _ in bands],
            "hr_table": [row for row in reversed(hr_table) if any(v is not None for v in row[1])],
            "best": reports.best_efforts(conn, start),
            "predictions": reports.race_predictions(conn, today),
            "estimate_days": reports.ESTIMATE_DAYS,
            "profile": reports.pace_profile(conn, f.units, start, HR_PACE_WIDTH_S),
            "band_width": HR_PACE_WIDTH_S,
        }

    return _page(request, conn, rng, "performance.html", build)


FORM_CHARTS = ("gct_ms", "vert_ratio", "stride_cm")


@router.get("/form")
def form_page(request: Request, conn: Conn, rng: RangeKey = None):
    def build(ctx, r, start, today):
        return {
            "periods": reports.form_periods(conn, start, r.table),
            "metrics": reports.FORM_METRICS,
            "charts": [m for m in reports.FORM_METRICS if m[0] in FORM_CHARTS],
            "garmin_runs": conn.execute("SELECT count(*) FROM running_garmin").fetchone()[0],
            "matched": len(reports.form_runs(conn)),
            "garmin_state": sync.garmin_state(conn),
            "garmin_configured": garmin.get_settings().configured,
        }

    return _page(request, conn, rng, "form.html", build)


@router.get("/charts/form/{metric}.json")
def form_chart(conn: Conn, metric: str, rng: RangeKey = None):
    """One form metric against pace: runs in the range vs earlier runs."""
    labels = {c: f"{label} ({unit})" for c, label, unit in reports.FORM_METRICS}
    if metric not in FORM_CHARTS:
        raise HTTPException(404)
    f = _ctx(conn)["f"]
    r, start, _ = _range(conn, rng)
    inside, before = [], []
    for a in reports.form_runs(conn):
        if a[metric] is not None:
            point = (round(a["moving_s"] / (a["distance_m"] / f.m)), round(a[metric], 1))
            (inside if a["start_local"] >= start.isoformat() else before).append(point)
    groups, names = [inside], [r.label if r.days else "All runs"]
    if r.days and before:
        groups, names = [before, inside], ["Earlier runs", r.label]
    xs, ys = merge_points(groups)
    return chart(
        xs, [Series(n, y, "points") for n, y in zip(names, ys, strict=True)],
        labels[metric], x_label=f"Pace (min/{f.units})", x_format="pace",
    )  # fmt: skip


@router.get("/charts/distance.json")
def distance_chart(conn: Conn, rng: RangeKey = None):
    f = _ctx(conn)["f"]
    r, start, today = _range(conn, rng)
    rows = reports.periods(conn, start, r.chart, today)
    xs = [day_epoch(p["start"]) for p in rows]
    values = [float(f.dist(p["distance_m"], unit=False)) for p in rows]
    return chart(xs, [Series("Distance", values)], f.units, bars=True)


def _period_and_runs(conn: sqlite3.Connection, rng: str | None, period_key: str, run_key: str,
                     digits: int, y_label: str) -> dict:  # fmt: skip
    """Chart of a per-period average (line) with each run's value (points)."""
    r, start, today = _range(conn, rng)
    rows = reports.periods(conn, start, r.chart, today)
    avg = [(day_epoch(p["start"]), round(v, digits) if (v := p[period_key]) else None)
           for p in rows]  # fmt: skip
    runs = [
        (reports.local_epoch(a["start_local"]), round(a[run_key], digits))
        for a in reports.per_run(conn, start) if a[run_key]
    ]  # fmt: skip
    xs, (avg_ys, run_ys) = merge_points([avg, runs])
    series = [Series(f"Per {r.chart}", avg_ys), Series("Each run", run_ys, "points")]
    return chart(xs, series, y_label)


@router.get("/charts/efficiency.json")
def efficiency_chart(conn: Conn, rng: RangeKey = None):
    return _period_and_runs(conn, rng, "ef", "ef", 3, "m/min per bpm")


@router.get("/charts/cadence.json")
def cadence_chart(conn: Conn, rng: RangeKey = None):
    return _period_and_runs(conn, rng, "cadence", "cadence_spm", 1, "steps per minute")


@router.get("/charts/intensity.json")
def intensity_chart(conn: Conn, rng: RangeKey = None):
    r, start, today = _range(conn, rng)
    rows = reports.periods(conn, start, r.chart, today)
    shares = [reports.intensity(p["zones"]) for p in rows]
    labels = ["Low (Z1–2)", "Moderate (Z3)", "High (Z4–5)"]
    series = [Series(lab, [s[i] if s else None for s in shares]) for i, lab in enumerate(labels)]
    return chart([day_epoch(p["start"]) for p in rows], series, "% of HR time")


@router.get("/charts/spikes.json")
def spikes_chart(conn: Conn, rng: RangeKey = None):
    f = _ctx(conn)["f"]
    _, start, _ = _range(conn, rng)
    rows = reports.run_spikes(conn, start)
    limit = 1 + reports.SPIKE_PCT / 100
    return chart(
        [reports.local_epoch(a["start_local"]) for a in rows],
        [
            Series("Run distance", [float(f.dist(a["distance_m"], unit=False)) for a in rows],
                   "points"),
            Series("Spike line (+10% on 30-day longest)",
                   [float(f.dist(a["prior_max"] * limit, unit=False)) if a["prior_max"] else None
                    for a in rows]),
        ],
        f.units,
    )  # fmt: skip


@router.get("/charts/hr-pace-runs.json")
def hr_pace_runs_chart(conn: Conn, rng: RangeKey = None):
    f = _ctx(conn)["f"]
    r, start, _ = _range(conn, rng)
    inside, before = [], []
    for a in reports.per_run(conn):
        if a["avg_hr"] and a["moving_s"] and a["distance_m"] / a["moving_s"] >= MIN_SPEED:
            point = (round(a["moving_s"] / (a["distance_m"] / f.m)), a["avg_hr"])
            (inside if a["start_local"] >= start.isoformat() else before).append(point)
    groups, labels = [inside], [r.label if r.days else "All runs"]
    if r.days and before:
        groups, labels = [before, inside], ["Earlier runs", r.label]
    xs, ys = merge_points(groups)
    return chart(
        xs, [Series(lab, y, "points") for lab, y in zip(labels, ys, strict=True)],
        "Avg HR (bpm)", x_label=f"Pace (min/{f.units})", x_format="pace",
    )  # fmt: skip


@router.get("/charts/hr-pace.json")
def hr_pace_chart(conn: Conn, rng: RangeKey = None):
    f = _ctx(conn)["f"]
    r, start, today = _range(conn, rng)
    xs, bands, _ = reports.hr_at_pace(
        conn, f.units, start, r.hr, today, HR_PACE_WIDTH_S, trim=r.days is None
    )
    series = [Series(f.band(b, HR_PACE_WIDTH_S), s.values) for b, s in bands]
    return chart(xs, series, "Avg HR (bpm)")


@router.get("/charts/fitness.json")
def fitness_chart(conn: Conn, rng: RangeKey = None):
    _, start, today = _range(conn, rng)
    days, ctl, atl, tsb = reports.fitness(conn, storage.athlete_settings(conn), start, today)
    series = [
        Series("Fitness (CTL)", ctl.round(1).tolist()),
        Series("Fatigue (ATL)", atl.round(1).tolist()),
        Series("Form (TSB)", tsb.round(1).tolist()),
    ]
    return chart([day_epoch(d) for d in days], series, "TRIMP per day")


@router.get("/charts/monotony.json")
def monotony_chart(conn: Conn, rng: RangeKey = None):
    _, start, today = _range(conn, rng)
    weeks, _ = reports.weekly_strain(conn, storage.athlete_settings(conn), start, today)
    series = [
        Series("Monotony", [_r2(w["monotony"]) for w in weeks]),
        Series(f"Foster's {reports.MONOTONY_HIGH:g} line", [reports.MONOTONY_HIGH] * len(weeks)),
    ]
    return chart([day_epoch(w["start"]) for w in weeks], series, "mean / SD of daily load")


@router.get("/charts/race-estimate.json")
def race_estimate_chart(conn: Conn, rng: RangeKey = None):
    r, start, today = _range(conn, rng)
    keys, secs = reports.race_estimates(conn, start, r.chart, today)
    return chart(
        [day_epoch(k) for k in keys], [Series("Estimated 5 km", secs)], "5 km time (min:s)",
        y_format="pace",
    )  # fmt: skip


def _r2(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


@router.get("/activities")
def activities(request: Request, conn: Conn, page: int = 1):
    page = max(page, 1)
    rows = reports.activities(conn, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)
    shown = rows[:PAGE_SIZE]
    oldest = date.fromisoformat(shown[-1]["start_local"][:10]) if shown else date.today()
    return render(
        request, "running/activities.html", **_ctx(conn),
        rows=shown, page=page, has_next=len(rows) > PAGE_SIZE,
        spikes=reports.spike_pcts(conn, oldest),
    )  # fmt: skip


def _monday(week: str | None) -> date:
    try:
        d = date.fromisoformat(week) if week else date.today()
    except ValueError:
        d = date.today()
    return reports.week_start(d)


def _plan_page(
    request: Request, conn: sqlite3.Connection, monday: date, form: dict | None = None,
    edit_id: int | None = None, error: str | None = None, status: int = 200,
):  # fmt: skip
    today = date.today()
    default_day = today if monday <= today < monday + timedelta(days=7) else monday
    blank = {"day": default_day.isoformat(), "kind": "easy"}
    blank |= dict.fromkeys(("distance", "minutes", "notes"), "")
    return render(
        request, "running/plan.html", **_ctx(conn), status=status,
        monday=monday, today=today, this_monday=reports.week_start(today),
        prev=monday - timedelta(weeks=1), next=monday + timedelta(weeks=1),
        wk=plan.week(conn, monday, today), kinds=plan.KINDS, run_kinds=plan.RUN_KINDS,
        spikes=reports.spike_pcts(conn, monday),
        caps=reports.spike_caps(conn, min(monday, today), max(monday + timedelta(days=6), today)),
        form=form or blank, edit_id=edit_id, error=error,
    )  # fmt: skip


@router.get("/plan")
def plan_page(request: Request, conn: Conn, week: str | None = None, edit: int | None = None):
    row = plan.get(conn, edit) if edit else None
    if row is None:
        return _plan_page(request, conn, _monday(week))
    f = _ctx(conn)["f"]
    form = {
        "day": row["day"], "kind": row["kind"], "notes": row["notes"],
        "distance": f.dist(row["distance_m"], unit=False) if row["distance_m"] else "",
        "minutes": round(row["duration_s"] / 60) if row["duration_s"] else "",
    }  # fmt: skip
    return _plan_page(request, conn, _monday(row["day"]), form, edit_id=row["id"])


@router.post("/plan")
def plan_save(
    request: Request,
    conn: Conn,
    week: Annotated[str, Form()],
    day: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    distance: Annotated[str, Form()] = "",
    minutes: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    session_id: Annotated[str, Form()] = "",
):
    form = {"day": day, "kind": kind, "distance": distance, "minutes": minutes, "notes": notes}
    m_per_unit = _ctx(conn)["f"].m
    try:
        sid = int(session_id) if session_id else None
        s = plan.Session(
            day=day, kind=kind, notes=notes,
            distance_m=float(distance) * m_per_unit if distance.strip() else None,
            duration_s=round(float(minutes) * 60) if minutes.strip() else None,
        )  # fmt: skip
    except ValueError as e:
        edit_id = int(session_id) if session_id.isdigit() else None
        return _plan_page(
            request, conn, _monday(week), form, edit_id, error=_error_msg(e), status=400
        )
    plan.save(conn, s, sid)
    conn.commit()
    return RedirectResponse(f"/running/plan?week={reports.week_start(s.day)}", 303)


@router.post("/plan/{session_id}/delete")
def plan_delete(conn: Conn, session_id: int, week: Annotated[str, Form()] = ""):
    plan.delete(conn, session_id)
    conn.commit()
    return RedirectResponse(f"/running/plan?week={_monday(week)}", 303)


@router.post("/plan/copy")
def plan_copy(conn: Conn, week: Annotated[str, Form()]):
    """Copy last week's sessions into an empty week (no-op otherwise, e.g. a double submit)."""
    monday = _monday(week)
    if plan.week(conn, monday, date.today())["sessions"] == 0:
        plan.copy_week(conn, monday - timedelta(weeks=1), monday)
        conn.commit()
    return RedirectResponse(f"/running/plan?week={monday}", 303)


@router.get("/shoes")
def shoes_page(request: Request, conn: Conn):
    return render(
        request, "running/shoes.html", **_ctx(conn), shoes=reports.shoes(conn, date.today()),
        no_gear=reports.runs_without_gear(conn), active_days=reports.SHOE_ACTIVE_DAYS,
    )  # fmt: skip


def _connected(conn: sqlite3.Connection) -> strava.StravaToken | None:
    return storage.kv_get(conn, strava.TOKEN_KEY, strava.StravaToken)


@router.get("/sync")
def sync_page(request: Request, conn: Conn, error: str | None = None):
    return render(
        request,
        "running/sync.html",
        **_ctx(conn),
        configured=config.get_strava_settings().configured,
        token=_connected(conn),
        state=sync.sync_state(conn),
        counts=reports.sync_counts(conn),
        failed=jobs.failed(conn, "running.", limit=5),
        limits=strava.LIMITER.status(conn),
        history_missing=len(sync.history_without_streams(conn)),
        error=error,
    )


@router.post("/sync/history")
def sync_history(conn: Conn):
    sync.queue_history_streams(conn)
    conn.commit()
    return RedirectResponse("/running/sync", status_code=303)


@router.post("/sync")
def sync_now(conn: Conn):
    enqueue(conn, "running.sync", dedupe_key="recurring:running.sync")
    conn.execute(
        "UPDATE jobs SET run_after = 0 WHERE dedupe_key = 'recurring:running.sync'"
        " AND status = 'pending'"
    )
    conn.commit()
    return RedirectResponse("/running/sync", status_code=303)


@router.get("/strava/connect")
def strava_connect(request: Request):
    settings = config.get_strava_settings()
    if not settings.configured:
        return RedirectResponse("/running/sync?error=Strava+credentials+missing", 303)
    state = secrets.token_urlsafe(16)
    request.session["strava_state"] = state
    redirect_uri = str(request.url_for("strava_callback"))
    return RedirectResponse(strava.authorize_url(settings, redirect_uri, state), 303)


@router.get("/strava/callback", name="strava_callback")
def strava_callback(
    request: Request, conn: Conn, state: str = "", code: str = "", scope: str = "",
    error: str = "",
):  # fmt: skip
    expected = request.session.pop("strava_state", None)
    if error or not code:
        return RedirectResponse(f"/running/sync?error=Strava+said:+{error or 'no+code'}", 303)
    if not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse("/running/sync?error=OAuth+state+mismatch;+try+again", 303)
    if strava.SCOPE not in scope.split(","):
        return RedirectResponse("/running/sync?error=Allow+activity+access+to+sync+runs", 303)
    with strava.make_http() as http:
        try:
            strava.exchange_code(conn, http, config.get_strava_settings(), code)
        except Exception:
            log.exception("strava token exchange failed")
            return RedirectResponse("/running/sync?error=Token+exchange+failed;+see+logs", 303)
    return sync_now(conn)


@router.post("/strava/disconnect")
def strava_disconnect(conn: Conn):
    storage.kv_delete(conn, strava.TOKEN_KEY)
    conn.commit()
    return RedirectResponse("/running/sync", status_code=303)


@router.get("/settings")
def settings_page(request: Request, conn: Conn):
    return render(request, "running/settings.html", **_ctx(conn), error=None, saved=False)


@router.post("/settings")
def save_settings(
    request: Request,
    conn: Conn,
    max_hr: Annotated[int, Form()],
    resting_hr: Annotated[int, Form()],
    units: Annotated[str, Form()],
    zone_bounds: Annotated[str, Form()] = "",
    shoe_warn: Annotated[str, Form()] = "",
):
    old = storage.athlete_settings(conn)
    try:
        bounds = [int(x) for x in zone_bounds.replace(" ", "").split(",") if x] or None
        warn = old.shoe_warn_m
        if shoe_warn.strip():
            warn = float(shoe_warn) * M_PER_UNIT.get(units, 1.0)
        new = AthleteSettings(
            max_hr=max_hr, resting_hr=resting_hr, units=units, zone_bounds=bounds,
            shoe_warn_m=warn,
        )  # fmt: skip
    except ValueError as e:
        return render(
            request, "running/settings.html", **_ctx(conn), error=_error_msg(e), saved=False,
            status=400,
        )  # fmt: skip
    storage.kv_set(conn, "athlete", new)
    if new.affects_metrics(old):
        enqueue(conn, "running.recompute", dedupe_key="running.recompute")
    conn.commit()
    return render(request, "running/settings.html", **_ctx(conn), error=None, saved=True)
