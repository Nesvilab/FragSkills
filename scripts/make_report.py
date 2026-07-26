"""Generate FragPipe's HTML summary report, in pure Python.

    python scripts/make_report.py --workdir D:/run/out

Writes ``<workdir>/fragpipe-report.html``: one self-contained page with the
experiment design table, the runtime Gantt chart, overall and per-run PSM
statistics, mass calibration before/after, Percolator feature weights, and the
MSBooster plots embedded as base64.

**No FragPipe installation, no Java, no third-party packages.**  It needs only
the run's own output -- ``psm.tsv``, ``log_*.txt``, the manifest, and whatever
MSBooster wrote -- so it works on a copied results folder, a cluster login node,
or a machine that has never had FragPipe on it.

This is a port of FragPipe's own report generators: ``generate_reports_pdf.py``,
which shipped in ``tools/`` through FragPipe 24.0, and the FragSummarizer jar
that replaced it in 24.1.  Same pages, same panels, same derivations -- the
counts have been checked panel by panel against the jar's output.  The parsing
lives in ``qc_data.py`` and the drawing in ``svg_charts.py``; both are usable on
their own when a single custom figure is what is wanted.

Exit codes: 0 report written, 1 nothing to report on, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import sys
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import svg_charts as svg  # noqa: E402
from qc_data import collect  # noqa: E402

REPORT_NAME = "fragpipe-report.html"

#: The nine per-run panels, in the order the official report lays them out:
#: (series key in qc_data, chart kind, title, x label).
RUN_PANELS = (
    ("peptide_length", "hist", "Peptide Length Distribution", "Peptide Length"),
    ("missed_cleavages", "cat", "Missed Cleavages", "Missed Cleavages"),
    ("charge", "cat", "Charge", "Charge"),
    ("mz", "hist", "M/Z Distribution", "M/Z"),
    ("retention", "hist", "Retention Time Distribution", "Retention Time (min)"),
    ("hyperscore", "hist", "Hyperscore Distribution", "Hyperscore"),
    ("delta_mass", "hist", "Delta Mass Distribution", "Delta Mass (Da)"),
    ("mz_vs_delta_mass", "scatter", "M/Z vs Delta Mass", "M/Z"),
    ("rt_vs_mz", "scatter", "RT vs M/Z", "Retention Time (min)"),
)
SCATTER_YLABEL = {"mz_vs_delta_mass": "Delta Mass (Da)", "rt_vs_mz": "M/Z"}

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f5f5f5; color: #333; }
.page { background: #fff; max-width: 1200px; margin: 24px auto; padding: 32px;
  border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
.page h1 { font-size: 24px; margin-bottom: 6px; }
.page h2 { font-size: 20px; margin-bottom: 16px; color: #1a1a1a;
  border-bottom: 2px solid #A0E7E5; padding-bottom: 8px; }
.subtitle { font-size: 13px; color: #666; word-break: break-all; }
.subtitle span { color: #999; }
table.overview { width: 100%; border-collapse: collapse; font-size: 13px; }
table.overview th { background: #A0E7E5; padding: 8px 10px; text-align: left;
  font-weight: 600; border-bottom: 2px solid #fff; }
table.overview td { padding: 7px 10px; border-bottom: 1px solid #e8e8e8;
  word-break: break-all; }
table.overview tr:hover td { background: #f8fffe; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 8px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 8px; }
.chart-cell svg { width: 100%; height: auto; display: block; }
.chart-cell .no-data { font-size: 12px; color: #999; padding: 24px 8px; text-align: center; }
.total-badge { display: inline-block; background: #fff3f3; border: 1px solid #e74c3c;
  border-radius: 6px; padding: 4px 12px; font-weight: 600; color: #e74c3c; float: right;
  font-size: 14px; }
.sub-heading { font-size: 15px; font-weight: 600; color: #444; margin: 22px 0 12px;
  padding: 6px 0 6px 10px; border-left: 3px solid #A0E7E5; background: #f8fdfd; }
.msb-grid { display: flex; flex-direction: column; gap: 18px; margin-top: 4px; }
.msb-card { background: #fafcfd; border: 1px solid #e4ecef; border-radius: 8px;
  padding: 14px 14px 10px; display: flex; flex-direction: column; align-items: center; }
.msb-card img { width: 100%; max-width: 820px; height: auto; object-fit: contain;
  border-radius: 4px; background: #fff; }
.msb-card figcaption { margin-top: 10px; font-size: 12px; color: #555;
  font-weight: 600; text-align: center; }
.note { font-size: 12px; color: #8a6d3b; background: #fcf8e3; border: 1px solid #faebcc;
  border-radius: 4px; padding: 8px 12px; margin-top: 12px; }
@media print { .page { box-shadow: none; margin: 0; page-break-after: always; } }
"""


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
def _page(title: str, body: str, badge: str = "") -> str:
    return ('<div class="page">\n<h2>%s%s</h2>\n%s</div>\n'
            % (svg.esc(title), badge, body))


def title_page(data: dict) -> str:
    started = data.get("started_at") or ""
    when = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ['<div class="page">\n<h1>FragPipe Report</h1>\n',
             '<p class="subtitle">%s</p>\n' % svg.esc(data["workdir"])]
    meta = []
    if started:
        meta.append("run started %s" % started.replace("T", " "))
    if data.get("total_minutes"):
        meta.append("%.1f min total" % data["total_minutes"])
    meta.append("report generated %s" % when)
    lines.append('<p class="subtitle"><span>%s</span></p>\n'
                 % svg.esc(" · ".join(meta)))
    lines.append("</div>\n")
    return "".join(lines)


def experiment_design_page(data: dict) -> str:
    rows = data.get("manifest") or []
    if not rows:
        return ""
    headers = ("Run", "Spectrum File", "Experiment", "Bioreplicate", "Data Type")
    body = svg.table(headers, [(r["run"], r["file"], r["experiment"],
                                r["bioreplicate"], r["data_type"]) for r in rows])
    return _page("Experiment Design", body)


def runtime_page(data: dict) -> str:
    tasks = data.get("runtimes") or []
    if not tasks:
        return ""
    plotted = sum(t["minutes"] for t in tasks)
    badge = '<span class="total-badge">Total: %.2f min</span>' % plotted

    finish = data.get("finished_at")
    finish_dt = None
    if finish:
        try:
            finish_dt = dt.datetime.fromisoformat(finish)
        except ValueError:
            finish_dt = None

    body = svg.gantt(tasks, finish_dt)
    if data.get("total_minutes") and abs(data["total_minutes"] - plotted) > 0.05:
        body += ('<p class="note">Bars cover %.2f min of the %.2f min the run '
                 'reported. The difference is workspace bookkeeping and gaps '
                 'between tasks, which the chart omits.</p>\n'
                 % (plotted, data["total_minutes"]))
    return _page("Workflow Processing Time", body, badge)


def overall_page(data: dict) -> str:
    overall = data.get("overall") or {}
    ids = data.get("ids") or []
    mass = data.get("mass_calibration") or []
    if not (overall or ids or mass):
        return ""

    body = ['<div class="grid2">\n']
    if overall.get("charge"):
        body.append(svg.panel(svg.category_bar(overall["charge"], "Charge",
                                               "Charge States")))
    if overall.get("peptide_length"):
        body.append(svg.panel(svg.histogram(overall["peptide_length"],
                                            "Peptide Length", "Peptide Length")))
    body.append("</div>\n")

    # Per-experiment file counts, except when the whole run collapsed into the
    # workdir root (DIA, spectral library): one bar for everything says nothing,
    # so fall back to the per-run counts split out of psm.tsv.
    per_run = data.get("ids_per_run") or []
    if len(ids) < 2 and len(per_run) > 1:
        ids, source = per_run, "per run"
    else:
        source = "per experiment"

    if ids:
        names = [r.get("experiment") or r.get("run") or "(root)" for r in ids]
        body.append('<div class="grid3">\n')
        for key, title, ylabel in (("psms", "PSM IDs", "# PSM"),
                                   ("peptides", "Peptide IDs", "# Peptides"),
                                   ("proteins", "Protein IDs", "# Proteins")):
            body.append(svg.panel(svg.experiment_bar(
                names, [r.get(key) for r in ids],
                "%s (%s)" % (title, source), ylabel)))
        body.append("</div>\n")

    body.append('<div class="grid3">\n')
    if overall.get("missed_cleavages"):
        body.append(svg.panel(svg.category_bar(overall["missed_cleavages"],
                                               "Missed Cleavage", "Missed Cleavages")))
    if mass:
        # Each level gets its own y range. Sharing one (as generate_reports_pdf.py
        # did) is wrong here: MS2 can be in Da while MS1 is in ppm, and a DIA run
        # with MS2 at -12 ppm against MS1 at +2.5 flattens the MS1 panel to
        # nothing -- exactly the panel that says whether calibration worked.
        search = data.get("search") or {}
        body.append(svg.panel(svg.mass_error(mass, "ms1", "MS1 Mass Error",
                                             search.get("ms1_units") or "ppm")))
        body.append(svg.panel(svg.mass_error(mass, "ms2", "MS2 Mass Error",
                                             search.get("ms2_units") or "ppm")))
    body.append("</div>\n")
    return _page("Overall Statistics", "".join(body))


def run_page(name: str, series: dict) -> str:
    body = []
    for start in (0, 3, 6):
        body.append('<div class="grid3">\n')
        for key, kind, title, xlabel in RUN_PANELS[start:start + 3]:
            data = series.get(key)
            if not data:
                continue
            if kind == "hist":
                chart = svg.histogram(data, title, xlabel)
            elif kind == "cat":
                chart = svg.category_bar(data, title, xlabel)
            else:
                chart = svg.scatter(data, title, xlabel, SCATTER_YLABEL[key])
            body.append(svg.panel(chart))
        body.append("</div>\n")

    counts = ("%s PSMs · %s peptides · %s proteins"
              % (series.get("psms", 0), series.get("peptides", 0),
                 series.get("proteins", 0)))
    badge = '<span class="total-badge">%s</span>' % svg.esc(counts)
    return _page("%s Statistics" % name, "".join(body), badge)


def msbooster_page(name: str, plots: Sequence[dict], features: Sequence[dict],
                   embed: bool = True) -> str:
    body = []
    if features:
        # qc_data sorts by |weight| descending; the report draws the list top
        # down, and reversing here puts the strongest feature at the bottom --
        # the order the official report uses.
        labels = [f["feature"] for f in reversed(features)]
        values = [f["mean_weight"] for f in reversed(features)]
        body.append(svg.signed_hbar(labels, values, "Percolator Feature Weights",
                                    "Mean Weight"))
    if plots:
        body.append('<h3 class="sub-heading">MSBooster Plots</h3>\n<div class="msb-grid">\n')
        for plot in plots:
            src = _img_src(plot["path"], embed)
            if not src:
                continue
            body.append('<figure class="msb-card">\n'
                        '<img src="%s" alt="%s" loading="lazy">\n'
                        '<figcaption>%s</figcaption>\n</figure>\n'
                        % (src, svg.esc(plot["label"]), svg.esc(plot["label"])))
        body.append("</div>\n")
    if not body:
        return ""
    # Title what the page actually holds. FragPipe's report skips the Percolator
    # weights entirely when a run has no MSBooster plots (TMT runs, MSBooster
    # off); the weights are worth showing on their own.
    if features and plots:
        heading = "MSBooster & Percolator Features"
    elif features:
        heading = "Percolator Features"
    else:
        heading = "MSBooster Plots"
    return _page("%s — %s" % (heading, name), "".join(body))


def _img_src(path: str, embed: bool) -> Optional[str]:
    if not embed:
        return path.replace("\\", "/")
    try:
        with open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def build_html(data: dict, embed_images: bool = True) -> str:
    parts = ["<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"UTF-8\">\n",
             '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n',
             "<title>FragPipe Report</title>\n<style>%s</style>\n</head>\n<body>\n" % CSS,
             title_page(data), experiment_design_page(data), runtime_page(data),
             overall_page(data)]

    runs = data.get("runs") or {}
    for name in sorted(runs):
        parts.append(run_page(name, runs[name]))

    plots = data.get("msbooster_plots") or {}
    features = data.get("percolator_features") or {}
    for name in sorted(set(runs) | set(plots) | set(features)):
        parts.append(msbooster_page(name, plots.get(name, []),
                                    features.get(name, []), embed_images))

    parts.append("</body>\n</html>\n")
    return "".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate fragpipe-report.html from a FragPipe output directory.")
    ap.add_argument("--workdir", required=True, help="FragPipe output directory")
    ap.add_argument("--out", help="output file (default: <workdir>/%s)" % REPORT_NAME)
    ap.add_argument("--stride", type=int, default=1,
                    help="keep every Nth PSM. Use on a very large psm.tsv; the "
                         "shapes survive, the counts scale by 1/N.")
    ap.add_argument("--link-images", action="store_true",
                    help="link MSBooster PNGs instead of embedding them. Smaller "
                         "file, but no longer self-contained.")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(args.workdir)
    if not os.path.isdir(workdir):
        print("[fragskills] ERROR: not a directory: %s" % workdir, file=sys.stderr)
        return 2

    data = collect(workdir, stride=max(1, args.stride))

    if not (data.get("runs") or data.get("runtimes") or data.get("ids")):
        print("[fragskills] ERROR: nothing to report on in %s.\n"
              "           Expected a log_*.txt and psm.tsv from a finished run."
              % workdir, file=sys.stderr)
        return 1
    for note in data.get("notes", []):
        print("[fragskills] note: %s" % note, file=sys.stderr)

    out_path = os.path.abspath(args.out) if args.out else os.path.join(workdir, REPORT_NAME)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(build_html(data, embed_images=not args.link_images))

    size_mb = os.path.getsize(out_path) / (1024.0 * 1024.0)
    runs = len(data.get("runs") or {})
    print("[fragskills] wrote %s (%.1f MB, %d run%s)"
          % (out_path, size_mb, runs, "" if runs == 1 else "s"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
