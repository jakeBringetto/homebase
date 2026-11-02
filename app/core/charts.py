"""Chart data helpers. Series are sent as uPlot-style JSON: {"data": [xs, ys...], "opts": {...}}."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

MAX_POINTS = 2000


def day_epoch(d: date) -> int:
    """A local day as midnight UTC, so chart x labels show that date."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


@dataclass
class Series:
    label: str
    values: Sequence[float | None]
    paths: str | None = None  # "bars" or "points"; default is a line


def chart(
    xs: Sequence[float],
    series: list[Series],
    y_label: str,
    *,
    bars: bool = False,
    x_label: str | None = None,
    x_format: str | None = None,
    y_format: str | None = None,
) -> dict:
    """uPlot payload. x is unix seconds unless `x_label` is given (then a plain number axis).
    Formats are app.js format names. app.js colors series by slot order (--s1, --s2...)."""
    y_fmt = {"format": y_format} if y_format else {}
    opts: dict = {
        "series": [
            {},
            *[{"label": s.label, **({"paths": s.paths or "bars"} if bars or s.paths else {}),
               **y_fmt} for s in series],
        ],
        "axes": [{}, {"label": y_label, **y_fmt}],
    }  # fmt: skip
    if x_label:
        opts["scales"] = {"x": {"time": False}}
        opts["series"][0] = {"label": x_label, "format": x_format}
        opts["axes"][0] = {"label": x_label, "format": x_format}
    return {"data": [list(xs), *[list(s.values) for s in series]], "opts": opts}


def merge_points(
    groups: Sequence[Sequence[tuple[float, float | None]]],
) -> tuple[list[float], list[list[float | None]]]:
    """Put several (x, y) point sets on one sorted x axis, None where a set has no point."""
    points = [(x, i, y) for i, pts in enumerate(groups) for x, y in pts]
    tagged = sorted(points, key=lambda t: t[:2])  # y may be None, so don't compare it
    xs = [x for x, _, _ in tagged]
    ys = [[y if i == g else None for _, i, y in tagged] for g in range(len(groups))]
    return xs, ys


def downsample(
    xs: Sequence[float], ys: Sequence[float], max_points: int = MAX_POINTS
) -> tuple[list[float], list[float]]:
    """Reduce a numeric series to `max_points` with largest-triangle-three-buckets (keeps peaks)."""
    n = len(xs)
    if n != len(ys):
        raise ValueError("xs and ys differ in length")
    if n <= max_points or max_points < 3:
        return list(xs), list(ys)
    bucket = (n - 2) / (max_points - 2)
    out_x, out_y = [xs[0]], [ys[0]]
    a = 0
    for i in range(max_points - 2):
        start, end = int(i * bucket) + 1, int((i + 1) * bucket) + 1
        nxt_end = min(int((i + 2) * bucket) + 1, n)
        avg_x = sum(xs[end:nxt_end]) / (nxt_end - end)
        avg_y = sum(ys[end:nxt_end]) / (nxt_end - end)
        best, best_area = start, -1.0
        for j in range(start, end):
            area = abs((xs[a] - avg_x) * (ys[j] - ys[a]) - (xs[a] - xs[j]) * (avg_y - ys[a]))
            if area > best_area:
                best, best_area = j, area
        out_x.append(xs[best])
        out_y.append(ys[best])
        a = best
    out_x.append(xs[-1])
    out_y.append(ys[-1])
    return out_x, out_y
