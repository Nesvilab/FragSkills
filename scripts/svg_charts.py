"""Minimal SVG chart primitives for the FragPipe summary report.

Plain strings, no dependencies -- the report has to work on a bare Python with
no matplotlib, no numpy, and no FragPipe installation.  Each function takes the
series ``qc_data.py`` produces and returns one ``<svg>`` element.

Geometry and colours follow FragPipe's own report generators (the
``generate_reports_pdf.py`` script that shipped through FragPipe 24.0, and the
FragSummarizer jar that replaced it), so the output is recognisable to anyone
who has seen the official report:

    #696969  distribution bars        #6B8EDB  Gantt bars
    #A0E7E5  table header             #2ecc71 / #e74c3c  +/- feature weights
    #808080  before calibration       #1a1a1a  after calibration
"""

from __future__ import annotations

from typing import Iterator, Optional, Sequence, Tuple

# Plot margins, in SVG user units.
ML, MR, MT, MB = 55, 20, 35, 45
#: Bottom margin when the x labels are rotated run/experiment names.
MB_ROTATED = 70

GRAY = "#696969"
GANTT = "#6B8EDB"
BEFORE, AFTER = "#808080", "#1a1a1a"
POSITIVE, NEGATIVE = "#2ecc71", "#e74c3c"
GRID = "#e8e8e8"
AXIS = "#333"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def nice_scale(lo: float, hi: float, max_ticks: int = 5) -> Tuple[float, float, float]:
    """Round an axis range outward to human-friendly ticks: (min, max, step)."""
    if lo == hi:
        hi = lo + 1
    rough = (hi - lo) / max_ticks
    magnitude = 10 ** _floor_log10(rough)
    residual = rough / magnitude
    if residual <= 1.5:
        step = magnitude
    elif residual <= 3:
        step = 2 * magnitude
    elif residual <= 7:
        step = 5 * magnitude
    else:
        step = 10 * magnitude
    import math
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _floor_log10(value: float) -> int:
    import math
    return int(math.floor(math.log10(value))) if value > 0 else 0


def fmt_tick(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    if abs(value) >= 100:
        return "%.0f" % value
    if abs(value) >= 1:
        return "%.1f" % value
    return "%.2f" % value


def text(x: float, y: float, label: str, anchor: str = "middle",
         size: int = 9, bold: bool = False, fill: Optional[str] = None) -> str:
    return ('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="%d"%s%s>%s</text>\n'
            % (x, y, anchor, size, ' font-weight="bold"' if bold else "",
               ' fill="%s"' % fill if fill else "", esc(label)))


def rot_text(x: float, y: float, label: str, size: int = 11,
             angle: int = -90, anchor: str = "middle") -> str:
    return ('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="%d" '
            'transform="rotate(%d,%.1f,%.1f)">%s</text>\n'
            % (x, y, anchor, size, angle, x, y, esc(label)))


def _line(x1: float, y1: float, x2: float, y2: float, color: str,
          dashed: bool = False) -> str:
    return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="1"%s/>\n'
            % (x1, y1, x2, y2, color, ' stroke-dasharray="4,2"' if dashed else ""))


def _rect(x: float, y: float, w: float, h: float, fill: str, rx: int = 0) -> str:
    return ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"%s/>\n'
            % (x, y, max(w, 0), max(h, 0), fill, ' rx="%d"' % rx if rx else ""))


def _open(width: int, height: int, responsive: bool = False) -> str:
    style = ' style="width:100%%;max-width:%dpx"' % width if responsive else ""
    return ('<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg"%s>\n'
            % (width, height, style))


def _empty(reason: str = "") -> str:
    """Placeholder for a panel with nothing to draw.

    Not an ``<svg>``: callers embedding panels in a page style it, and callers
    saving a single chart to a .svg file get an empty string to test against.
    """
    return '<p class="no-data">%s</p>\n' % esc(reason)


def panel(chart: str) -> str:
    """Wrap a chart for the report's CSS grid."""
    return '<div class="chart-cell">\n%s</div>\n' % chart


def ticks(lo: float, hi: float, step: float) -> Iterator[float]:
    """Tick positions from *lo* to *hi*, inclusive of *hi* despite float drift."""
    tick = lo
    while tick <= hi + step * 0.01:
        yield tick
        tick += step


def _y_axis(ph: int, pw: int, lo: float, hi: float, step: float) -> str:
    """Grid lines and tick labels for a vertical axis spanning [lo, hi]."""
    out = []
    span = (hi - lo) or 1
    for tick in ticks(lo, hi, step):
        y = MT + ph - (tick - lo) / span * ph
        if tick > lo:
            out.append(_line(ML, y, ML + pw, y, GRID))
        out.append(text(ML - 5, y + 4, fmt_tick(tick), "end", 9))
    return "".join(out)


def _frame(ph: int, pw: int) -> str:
    return (_line(ML, MT, ML, MT + ph, AXIS)
            + _line(ML, MT + ph, ML + pw, MT + ph, AXIS))


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
def histogram(series: dict, title: str, xlabel: str, ylabel: str = "# PSM",
              width: int = 400, height: int = 300) -> str:
    """Bar chart of pre-binned counts (``qc_data`` histogram series)."""
    counts = series.get("counts") or []
    edges = series.get("bin_edges") or []
    if not counts or len(edges) != len(counts) + 1:
        return _empty("no data")

    pw, ph = width - ML - MR, height - MT - MB
    _, y_hi, y_step = nice_scale(0, max(counts), 5)
    y_hi = y_hi or 1
    x_lo, x_hi = edges[0], edges[-1]
    x_span = (x_hi - x_lo) or 1

    out = [_open(width, height), text(width / 2.0, 20, title, "middle", 13, True),
           _y_axis(ph, pw, 0, y_hi, y_step)]

    bar_w = pw / len(counts)
    for i, count in enumerate(counts):
        if not count:
            continue
        bar_h = count / y_hi * ph
        out.append(_rect(ML + i * bar_w, MT + ph - bar_h, bar_w, bar_h, GRAY))

    out.append(_frame(ph, pw))

    for tick in ticks(*nice_scale(x_lo, x_hi, 5)):
        if x_lo <= tick <= x_hi:
            out.append(text(ML + (tick - x_lo) / x_span * pw, MT + ph + 15,
                            fmt_tick(tick), "middle", 9))

    out.append(text(ML + pw / 2.0, height - 5, xlabel, "middle", 11))
    out.append(rot_text(12, MT + ph / 2.0, ylabel, 11))
    out.append("</svg>\n")
    return "".join(out)


def category_bar(series: dict, title: str, xlabel: str, ylabel: str = "# PSM",
                 width: int = 400, height: int = 300) -> str:
    """One labelled bar per discrete value (charge, missed cleavages)."""
    cats = series.get("categories") or []
    counts = series.get("counts") or []
    if not cats:
        return _empty("no data")

    pw, ph = width - ML - MR, height - MT - MB
    _, y_hi, y_step = nice_scale(0, max(counts), 5)
    y_hi = y_hi or 1

    out = [_open(width, height), text(width / 2.0, 20, title, "middle", 13, True),
           _y_axis(ph, pw, 0, y_hi, y_step)]

    group_w = pw / len(cats)
    bar_w = group_w * 0.7
    for i, (cat, count) in enumerate(zip(cats, counts)):
        bar_h = count / y_hi * ph
        centre = ML + i * group_w + group_w / 2
        out.append(_rect(centre - bar_w / 2, MT + ph - bar_h, bar_w, bar_h, GRAY))
        out.append(text(centre, MT + ph - bar_h - 4, str(count), "middle", 9))
        out.append(text(centre, MT + ph + 15, str(cat), "middle", 9))

    out.append(_frame(ph, pw))
    out.append(text(ML + pw / 2.0, height - 5, xlabel, "middle", 11))
    out.append(rot_text(12, MT + ph / 2.0, ylabel, 11))
    out.append("</svg>\n")
    return "".join(out)


def experiment_bar(names: Sequence[str], values: Sequence[float], title: str,
                   ylabel: str, width: int = 400, height: int = 300) -> str:
    """One bar per experiment, with the value on top and rotated name labels."""
    pairs = [(n, v) for n, v in zip(names, values) if v is not None]
    if not pairs:
        return _empty("no counts")

    pw, ph = width - ML - MR, height - MT - MB_ROTATED
    _, y_hi, y_step = nice_scale(0, max(v for _, v in pairs), 5)
    y_hi = y_hi or 1

    out = [_open(width, height), text(width / 2.0, 20, title, "middle", 13, True),
           _y_axis(ph, pw, 0, y_hi, y_step)]

    group_w = pw / len(pairs)
    bar_w = group_w * 0.7
    for i, (name, value) in enumerate(pairs):
        bar_h = value / y_hi * ph
        centre = ML + i * group_w + group_w / 2
        out.append(_rect(centre - bar_w / 2, MT + ph - bar_h, bar_w, bar_h, GRAY))
        out.append(text(centre, MT + ph - bar_h - 4, str(int(round(value))),
                        "middle", 9, True))
        label = name if len(name) <= 20 else name[:20] + ".."
        out.append(rot_text(centre, MT + ph + 8, label, 8, -40, "end"))

    out.append(_frame(ph, pw))
    out.append(rot_text(12, MT + ph / 2.0, ylabel, 11))
    out.append("</svg>\n")
    return "".join(out)


def mass_error(rows: Sequence[dict], level: str, title: str, units: str = "ppm",
               y_range: Optional[Tuple[float, float]] = None,
               width: int = 400, height: int = 300) -> str:
    """Median mass error per run, before vs after calibration.

    *rows* are ``qc_data`` mass-calibration entries; *level* is ``"ms1"`` or
    ``"ms2"``.  Passing a shared *y_range* keeps the MS1 and MS2 panels
    comparable side by side.
    """
    if not rows:
        return _empty("no mass calibration in the log")

    before = [r["%s_old_median" % level] for r in rows]
    after = [r["%s_calibrated_median" % level] for r in rows]
    lo, hi = (y_range if y_range else
              (min(before + after + [0]), max(before + after + [0])))

    pw, ph = width - ML - MR, height - MT - MB_ROTATED
    y_lo, y_hi, y_step = nice_scale(lo, hi, 5)
    span = (y_hi - y_lo) or 1

    out = [_open(width, height), text(width / 2.0, 20, title, "middle", 13, True),
           _y_axis(ph, pw, y_lo, y_hi, y_step)]

    baseline = MT + ph - (0 - y_lo) / span * ph
    if y_lo < 0 < y_hi:
        out.append(_line(ML, baseline, ML + pw, baseline, "#999", dashed=True))

    group_w = pw / len(rows)
    bar_w = group_w * 0.35
    for i, row in enumerate(rows):
        centre = ML + i * group_w + group_w / 2
        for value, colour, offset in ((before[i], BEFORE, -bar_w - 1),
                                      (after[i], AFTER, 1)):
            y = MT + ph - (value - y_lo) / span * ph
            out.append(_rect(centre + offset, min(y, baseline), bar_w,
                             abs(y - baseline), colour))
        label = row["run"] if len(row["run"]) <= 15 else row["run"][:15] + ".."
        out.append(rot_text(centre, MT + ph + 8, label, 7, -40, "end"))

    out.append(_frame(ph, pw))
    out.append(_rect(ML + pw - 120, MT + 2, 10, 10, BEFORE))
    out.append(text(ML + pw - 107, MT + 11, "Before", "start", 9))
    out.append(_rect(ML + pw - 60, MT + 2, 10, 10, AFTER))
    out.append(text(ML + pw - 47, MT + 11, "Calibrated", "start", 9))
    out.append(rot_text(12, MT + ph / 2.0, "Median error (%s)" % units, 11))
    out.append("</svg>\n")
    return "".join(out)


def scatter(series: dict, title: str, xlabel: str, ylabel: str,
            width: int = 400, height: int = 300) -> str:
    xs, ys = series.get("x") or [], series.get("y") or []
    if not xs:
        return _empty("no data")

    pw, ph = width - ML - MR, height - MT - MB
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_min == x_max:
        x_min, x_max = x_min - 0.5, x_max + 0.5
    if y_min == y_max:
        y_min, y_max = y_min - 0.5, y_max + 0.5
    x_span, y_span = x_max - x_min, y_max - y_min

    out = [_open(width, height), text(width / 2.0, 20, title, "middle", 13, True)]

    # Grid drawn on the data range, ticks on the rounded range.
    for tick in ticks(*nice_scale(y_min, y_max, 5)):
        if y_min <= tick <= y_max:
            y = MT + ph - (tick - y_min) / y_span * ph
            out.append(_line(ML, y, ML + pw, y, GRID))
            out.append(text(ML - 5, y + 4, fmt_tick(tick), "end", 9))

    out.append('<g fill="#808080" fill-opacity="0.4">\n')
    for x, y in zip(xs, ys):
        out.append('<circle cx="%.1f" cy="%.1f" r="1.5"/>\n'
                   % (ML + (x - x_min) / x_span * pw,
                      MT + ph - (y - y_min) / y_span * ph))
    out.append("</g>\n")

    out.append(_frame(ph, pw))
    for tick in ticks(*nice_scale(x_min, x_max, 5)):
        if x_min <= tick <= x_max:
            out.append(text(ML + (tick - x_min) / x_span * pw, MT + ph + 15,
                            fmt_tick(tick), "middle", 9))

    out.append(text(ML + pw / 2.0, height - 5, xlabel, "middle", 11))
    out.append(rot_text(12, MT + ph / 2.0, ylabel, 11))
    out.append("</svg>\n")
    return "".join(out)


def signed_hbar(labels: Sequence[str], values: Sequence[float], title: str,
                xlabel: str, width: int = 700) -> str:
    """Horizontal bars around zero, green positive / red negative."""
    if not labels:
        return ""
    n = len(labels)
    label_w, bar_h, gap = 180, 20, 5
    ph = n * (bar_h + gap)
    height = MT + ph + MB + 10
    pw = width - label_w - MR

    abs_max = max(abs(v) for v in values) or 1
    x_lo, x_hi, x_step = nice_scale(-abs_max, abs_max, 5)
    span = (x_hi - x_lo) or 1

    out = [_open(width, height, responsive=True),
           text(width / 2.0, 20, title, "middle", 13, True)]

    zero_x = label_w + (0 - x_lo) / span * pw
    out.append(_line(zero_x, MT, zero_x, MT + ph, "#999", dashed=True))

    for i, (label, value) in enumerate(zip(labels, values)):
        vx = label_w + (value - x_lo) / span * pw
        y = MT + i * (bar_h + gap)
        out.append(_rect(min(zero_x, vx), y, abs(vx - zero_x), bar_h,
                         POSITIVE if value > 0 else NEGATIVE, rx=2))
        out.append(text(label_w - 5, y + bar_h / 2.0 + 4, label, "end", 9))
        out.append(text(vx + (4 if value >= 0 else -4), y + bar_h / 2.0 + 4,
                        "%.4f" % value, "start" if value >= 0 else "end", 8))

    out.append(_line(label_w, MT + ph, label_w + pw, MT + ph, AXIS))
    for tick in ticks(x_lo, x_hi, x_step):
        out.append(text(label_w + (tick - x_lo) / span * pw, MT + ph + 15,
                        fmt_tick(tick), "middle", 9))
    out.append(text(label_w + pw / 2.0, height - 5, xlabel, "middle", 11))
    out.append("</svg>\n")
    return "".join(out)


def gantt(tasks: Sequence[dict], finish_time: Optional[object] = None,
          width: int = 800) -> str:
    """Task timeline.

    With a *finish_time* (a ``datetime``) the bars are laid out backwards from
    it on a clock axis, as the official report does; tasks are assumed to run
    back to back, which holds for a normal sequential FragPipe run.  Without
    one, the same bars are drawn against elapsed minutes.
    """
    if not tasks:
        return ""
    return (_gantt_clock(tasks, finish_time, width) if finish_time
            else _gantt_elapsed(tasks, width))


def _gantt_geometry(n: int, width: int) -> Tuple[int, int, int, int, int, int]:
    label_w, bar_h, gap = 200, 22, 6
    ph = n * (bar_h + gap)
    height = MT + ph + MB + 15
    return label_w, bar_h, gap, ph, height, width - label_w - MR


def _gantt_clock(tasks: Sequence[dict], finish_time, width: int) -> str:
    import datetime as dt

    spans, end = [], finish_time
    for task in reversed(list(tasks)):
        start = end - dt.timedelta(minutes=task["minutes"])
        spans.insert(0, (task["task"], start, end, task["minutes"]))
        end = start

    n = len(spans)
    label_w, bar_h, gap, ph, height, pw = _gantt_geometry(n, width)
    origin, finish = spans[0][1], spans[-1][2]
    total_sec = max((finish - origin).total_seconds(), 1)
    total_min = total_sec / 60.0

    for limit, step in ((10, 1), (30, 5), (120, 15), (360, 30)):
        if total_min <= limit:
            interval = step
            break
    else:
        interval = 60

    out = [_open(width, height, responsive=True)]

    tick = origin.replace(second=0, microsecond=0)
    minutes = (tick.hour * 60 + tick.minute) // interval * interval
    tick = tick.replace(hour=minutes // 60, minute=minutes % 60)
    if tick < origin:
        tick += dt.timedelta(minutes=interval)
    while tick <= finish:
        x = label_w + (tick - origin).total_seconds() / total_sec * pw
        out.append(_line(x, MT, x, MT + ph, "#e0e0e0"))
        out.append(text(x, MT + ph + 15, tick.strftime("%H:%M"), "middle", 9))
        tick += dt.timedelta(minutes=interval)

    for i, (name, start, stop, minutes_) in enumerate(spans):
        x1 = label_w + (start - origin).total_seconds() / total_sec * pw
        x2 = label_w + (stop - origin).total_seconds() / total_sec * pw
        y = MT + i * (bar_h + gap)
        bar_w = max(x2 - x1, 1)
        out.append(_rect(x1, y, bar_w, bar_h, GANTT, rx=2))
        label = "%.2f min" % minutes_
        if bar_w > 80:
            out.append(text(x1 + bar_w / 2, y + bar_h / 2.0 + 3, label,
                            "middle", 8, True, fill="#000"))
        else:
            out.append(text(x2 + 3, y + bar_h / 2.0 + 3, label, "start", 8, True))
        out.append(text(label_w - 5, y + bar_h / 2.0 + 4, name, "end", 10))

    out.append(_line(label_w, MT + ph, label_w + pw, MT + ph, AXIS))
    out.append(text(label_w + pw / 2.0, height - 3, "Time", "middle", 11))
    out.append("</svg>\n")
    return "".join(out)


def _gantt_elapsed(tasks: Sequence[dict], width: int) -> str:
    n = len(tasks)
    label_w, bar_h, gap, ph, height, pw = _gantt_geometry(n, width)
    _, x_hi, x_step = nice_scale(0, max(t["minutes"] for t in tasks), 5)
    x_hi = x_hi or 1

    out = [_open(width, height, responsive=True)]
    for tick in ticks(x_step, x_hi - x_step * 0.01, x_step):
        out.append(_line(label_w + tick / x_hi * pw, MT, label_w + tick / x_hi * pw,
                         MT + ph, GRID))

    elapsed = 0.0
    for i, task in enumerate(tasks):
        x1 = label_w + elapsed / x_hi * pw
        elapsed += task["minutes"]
        x2 = label_w + elapsed / x_hi * pw
        y = MT + i * (bar_h + gap)
        out.append(_rect(x1, y, max(x2 - x1, 1), bar_h, GANTT, rx=2))
        out.append(text(x2 + 3, y + bar_h / 2.0 + 3, "%.2f min" % task["minutes"],
                        "start", 8, True))
        out.append(text(label_w - 5, y + bar_h / 2.0 + 4, task["task"], "end", 10))

    out.append(_line(label_w, MT + ph, label_w + pw, MT + ph, AXIS))
    for tick in ticks(0.0, x_hi, x_step):
        out.append(text(label_w + tick / x_hi * pw, MT + ph + 15,
                        fmt_tick(tick), "middle", 9))
    out.append(text(label_w + pw / 2.0, height - 3, "Elapsed (min)", "middle", 11))
    out.append("</svg>\n")
    return "".join(out)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ['<table class="overview">\n<tr>']
    out.extend("<th>%s</th>" % esc(h) for h in headers)
    out.append("</tr>\n")
    for row in rows:
        out.append("<tr>")
        out.extend("<td>%s</td>" % esc(c) for c in row)
        out.append("</tr>\n")
    out.append("</table>\n")
    return "".join(out)
