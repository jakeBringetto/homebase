"""Display formatting in the athlete's units. Storage is always SI (m, s, s/km)."""

from datetime import date, datetime

M_PER_UNIT = {"km": 1000.0, "mi": 1609.344}
PERIOD_FORMATS = {"week": "%b %-d, %Y", "month": "%b %Y", "year": "%Y"}


def mmss(seconds: float) -> str:
    t = round(seconds)
    return f"{t // 60}:{t % 60:02d}"


class Fmt:
    def __init__(self, units: str):
        self.units = units
        self.m = M_PER_UNIT[units]

    def dist(self, meters: float | None, unit: bool = True) -> str:
        value = f"{(meters or 0) / self.m:.1f}"
        return f"{value} {self.units}" if unit else value

    def pace(self, moving_s: float, distance_m: float) -> str:
        if not distance_m:
            return "–"
        return f"{mmss(moving_s / (distance_m / self.m))}/{self.units}"

    def pace_km(self, s_per_km: float | None) -> str:
        """Format a stored s/km pace in the display unit."""
        return f"{mmss(s_per_km * self.m / 1000)}/{self.units}" if s_per_km else "–"

    def band(self, start_s: int, width_s: int) -> str:
        return f"{mmss(start_s)}–{mmss(start_s + width_s)}/{self.units}"

    @staticmethod
    def duration(seconds: float | None) -> str:
        t = round(seconds or 0)
        h, rem = divmod(t, 3600)
        return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else mmss(rem)

    @staticmethod
    def day(start_local: str) -> str:
        return datetime.fromisoformat(start_local).strftime("%a %b %-d, %Y")

    @staticmethod
    def period(d: date, bucket: str) -> str:
        return d.strftime(PERIOD_FORMATS[bucket])

    @staticmethod
    def num(value: float | None, digits: int = 0) -> str:
        return "–" if value is None else f"{value:.{digits}f}"
