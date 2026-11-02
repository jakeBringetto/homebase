"""Recovery pages: overview (HRV, RHR, sleep, next-morning response, readiness), sync, charts."""

import sqlite3
import time
from datetime import date
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from app.core import garmin
from app.core.charts import Series, chart, day_epoch
from app.core.jobs import enqueue
from app.core.web import get_conn, render
from app.modules.recovery import reports, sync
from app.modules.running import reports as running_reports

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
RangeKey = Annotated[str | None, Query(alias="range")]


def _r1(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


@router.get("/")
def overview(request: Request, conn: Conn, rng: RangeKey = None):
    today = date.today()
    key, start = reports.range_start(rng, today)
    nights = reports.nights(conn, start)
    cvs = [n.cv for n in nights if n.cv is not None]
    return render(
        request,
        "recovery/overview.html",
        rng=key,
        ranges=reports.RANGES,
        start=start,
        latest=reports.latest(conn, today),
        fallback_band=any(n.low is not None and not n.garmin_band for n in nights),
        cv_now=cvs[-1] if cvs else None,
        cv_median=float(np.median(cvs)) if cvs else None,
        response=reports.next_morning(conn, start, today),
        readiness=reports.readiness(conn, start),
        expect_days=running_reports.EXPECT_DAYS,
        configured=garmin.get_settings().configured,
        has_data=reports.counts(conn)["hrv"][0] > 0,
    )


@router.get("/charts/hrv.json")
def hrv_chart(conn: Conn, rng: RangeKey = None):
    _, start = reports.range_start(rng, date.today())
    ns = reports.nights(conn, start)
    series = [
        Series("Nightly HRV", [_r1(n.hrv) for n in ns], paths="points"),
        Series("7-day avg", [_r1(n.weekly) for n in ns]),
        Series("Normal low", [_r1(n.low) for n in ns]),
        Series("Normal high", [_r1(n.high) for n in ns]),
    ]
    return chart([day_epoch(n.day) for n in ns], series, "HRV (ms)")


@router.get("/charts/hrv-cv.json")
def hrv_cv_chart(conn: Conn, rng: RangeKey = None):
    _, start = reports.range_start(rng, date.today())
    ns = reports.nights(conn, start)
    return chart([day_epoch(n.day) for n in ns], [Series("7-day CV", [n.cv for n in ns])], "%")


@router.get("/charts/readiness.json")
def readiness_chart(conn: Conn, rng: RangeKey = None):
    _, start = reports.range_start(rng, date.today())
    pts = sorted(
        (p for p in reports.readiness(conn, start)["points"] if p["hrv_pct"] is not None),
        key=lambda p: p["hrv_pct"],
    )
    series = [Series("Run", [_r1(p["delta"]) for p in pts], paths="points")]
    return chart(
        [_r1(p["hrv_pct"]) for p in pts], series, "HR vs expected (bpm)",
        x_label="Morning HRV vs middle of normal range (%)",
    )  # fmt: skip


@router.get("/charts/rhr.json")
def rhr_chart(conn: Conn, rng: RangeKey = None):
    _, start = reports.range_start(rng, date.today())
    rows = reports.resting_hr(conn, start)
    series = [
        Series("Resting HR", [v for _, v, _ in rows], paths="points"),
        Series("7-day avg", [avg for _, _, avg in rows]),
    ]
    return chart([day_epoch(d) for d, _, _ in rows], series, "bpm")


@router.get("/charts/sleep.json")
def sleep_chart(conn: Conn, rng: RangeKey = None):
    _, start = reports.range_start(rng, date.today())
    rows = reports.sleep(conn, start)
    hours = [round(r["sleep_s"] / 3600, 2) for r in rows]
    xs = [day_epoch(date.fromisoformat(r["day"])) for r in rows]
    return chart(xs, [Series("Sleep", hours)], "hours", bars=True)


@router.get("/charts/response.json")
def response_chart(conn: Conn, rng: RangeKey = None):
    today = date.today()
    _, start = reports.range_start(rng, today)
    pts = sorted(
        (p for p in reports.next_morning(conn, start, today)["points"] if p["hrv"] is not None),
        key=lambda p: p["trimp"],
    )
    series = [Series("Next-morning HRV", [p["hrv"] for p in pts], paths="points")]
    return chart(
        [round(p["trimp"], 1) for p in pts], series, "HRV (ms)", x_label="Previous day TRIMP"
    )


@router.get("/sync")
def sync_page(request: Request, conn: Conn):
    settings = garmin.get_settings()
    auth = garmin.auth_state(conn, settings) if settings.configured else None
    return render(
        request,
        "recovery/sync.html",
        configured=settings.configured,
        auth=auth,
        now=time.time(),
        state=sync.sync_state(conn),
        counts=reports.counts(conn),
        limits=garmin.LIMITER.status(conn),
        failed=conn.execute(
            "SELECT kind, last_error FROM jobs WHERE kind LIKE 'recovery.%' AND status = 'failed'"
            " ORDER BY updated_at DESC LIMIT 5"
        ).fetchall(),
    )


@router.post("/sync")
def sync_now(conn: Conn):
    enqueue(conn, "recovery.sync", {"force": True}, priority=1, dedupe_key="recovery.sync:now")
    conn.commit()
    return RedirectResponse("/recovery/sync", status_code=303)
