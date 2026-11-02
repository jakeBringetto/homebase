// Renders [data-chart] elements with uPlot from a JSON endpoint: {data: [xs, ys...], opts: {...}}.
// JSON can't carry functions, so opts use names: series "paths": "bars" | "points",
// axis/series "format".
// Series colors come from CSS slots --s1..--s4 in order, so they follow the light/dark theme.
const formats = {
  pace: (s) => {
    if (s == null) return "";
    const t = Math.round(s);
    return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
  },
  // Hours from midnight, possibly negative (-1.5 = 22:30 the evening before).
  clock: (h) => {
    if (h == null) return "";
    const m = ((Math.round(h * 60) % 1440) + 1440) % 1440;
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  },
};

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function prepare(opts) {
  const ink = css("--muted");
  const rule = css("--line");
  opts.axes = (opts.axes || [{}, {}]).map((a) => {
    const axis = { stroke: ink, grid: { stroke: rule, width: 1 }, ticks: { stroke: rule, width: 1 }, ...a };
    if (formats[a.format]) axis.values = (u, splits) => splits.map(formats[a.format]);
    return axis;
  });
  opts.series = (opts.series || [{}]).map((s, i) => {
    if (i === 0) {
      return formats[s.format] ? { ...s, value: (u, v) => formats[s.format](v) } : s;
    }
    const color = css(`--s${i}`);
    const out = { stroke: color, width: 2, spanGaps: true, points: { size: 8, fill: color }, ...s };
    if (s.paths === "points") {
      Object.assign(out, { paths: () => null, points: { show: true, size: 7, fill: color } });
    }
    if (s.paths === "bars") {
      Object.assign(out, {
        paths: uPlot.paths.bars({ size: [0.7, 48], radius: 0.2 }),
        fill: color,
        points: { show: false },
      });
    }
    if (formats[s.format]) out.value = (u, v) => formats[s.format](v);
    return out;
  });
  return opts;
}

const charts = new Map();

async function renderChart(el) {
  if (el.dataset.rendered) return;
  el.dataset.rendered = "1";
  const res = await fetch(el.dataset.chart);
  if (!res.ok) {
    el.textContent = "Chart unavailable";
    return;
  }
  const { data, opts } = await res.json();
  const size = { width: el.clientWidth, height: +el.dataset.height || 300 };
  charts.set(el, new uPlot({ ...size, ...prepare(opts) }, data, el));
}

const renderCharts = (root) => root.querySelectorAll("[data-chart]").forEach(renderChart);
document.addEventListener("DOMContentLoaded", () => renderCharts(document));
document.addEventListener("htmx:afterSwap", (e) => renderCharts(e.target));
window.addEventListener("resize", () => {
  for (const [el, u] of charts) u.setSize({ width: el.clientWidth, height: u.height });
});
