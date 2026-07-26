"""Extract the data behind FragPipe's QC plots from a finished run.

Every series in ``fragpipe-report.html`` comes from ``psm.tsv`` plus the run's
``log_*.txt``; this module derives all of them with the standard library alone,
and ``make_report.py`` draws the report from what it returns.

    python scripts/qc_data.py --workdir D:/run/out                 # JSON
    python scripts/qc_data.py --workdir D:/run/out --tsv D:/qc     # tidy TSVs
    python scripts/qc_data.py --workdir D:/run/out --list          # what is there

Use it directly when the report itself is not the goal: a single publication
figure, one run out of forty, a QC number quoted in prose, or a comparison
across runs the report does not make.

The derivations follow FragPipe's own report generators (dedup keys, bin widths,
the 0.5 Da delta-mass window, seconds -> minutes), so the counts match what
FragPipe would have drawn.  ``references/plots.md`` documents each one, and
lists the few places where this deliberately does something better.

Importable::

    from qc_data import collect
    data = collect("D:/run/out")
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from array import array
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fragpipe_progress import RunProgress  # noqa: E402
from parse_reports import count_rows, discover, iter_rows  # noqa: E402

#: FragSummarizer plots the median of the |Delta Mass| < 0.5 Da window only.
DELTA_MASS_WINDOW = 0.5
#: Points kept per scatter panel; more than this is unreadable and slow to draw.
SCATTER_POINTS = 5000
#: MSBooster PNGs the report embeds, in the order it shows them.
MSBOOSTER_PLOTS = (
    ("RT_calibration_curves", "RT Calibration Curve"),
    ("delta_RT_loess", "Delta RT (LOESS)"),
    ("pred_RT_real_units", "Predicted vs. Observed RT"),
    ("unweighted_spectral_entropy", "Unweighted Spectral Entropy"),
)

_RE_SCAN_LINE = re.compile(r"^(\S+): Scans = \d+;")
_RE_NUMBERED_FILE = re.compile(
    r"^(\d{3})\.\s+(\S+\.(?:mzML|mzXML|mzBIN|raw|d|wiff|mgf))\b", re.IGNORECASE)
_RE_SVM_BLOCK = re.compile(
    r"Learned normalized SVM weights for the 3 cross-validation splits:\s*\n"
    r"(.*?)\nFound\s+\d+", re.DOTALL)
_RE_PIN = re.compile(r"Reading tab-delimited input from datafile\s+(.+?)(?:_edited)?\.pin")
_RE_TIME_MIN = re.compile(r"DONE IN\s+([0-9.]+)\s+MIN")

#: MSFragger's own banners, which the Gantt chart shows in place of the single
#: "MSFragger" task. ``fragpipe_progress`` collects these as milestones with the
#: asterisks stripped; the longer calibration banner must be tested first.
FRAGGER_STAGES = (
    ("FIRST SEARCH DONE IN", "First Search"),
    ("MASS CALIBRATION AND PARAMETER OPTIMIZATION DONE IN", "Mass Calibration & Param Opt"),
    ("MASS CALIBRATION DONE IN", "Mass Calibration"),
    ("MAIN SEARCH DONE IN", "Main Search"),
)
#: Bookkeeping tasks the runtime chart leaves out.
RUNTIME_SKIP = ("WorkspaceCleanInit", "WorkspaceClean")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _f(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def latest_log(workdir: str) -> Optional[str]:
    """The log FragSummarizer would read: the last ``log_*.txt`` by name."""
    logs = sorted(glob.glob(os.path.join(workdir, "log_*.txt")))
    return logs[-1] if logs else None


def log_timeline(log_path: str, total_minutes: Optional[float]) -> Tuple[Optional[str], Optional[str]]:
    """(start, finish) of the run as ISO strings, for the Gantt clock axis.

    FragPipe names its log ``log_<date>_<HH-MM-SS>.txt`` at the moment the run
    starts, and prints the total at the end, which pins both ends exactly.  The
    fallback is the log's last-modified time, i.e. when the run stopped writing.

    (FragPipe's own report generators instead take the largest ``time="..."``
    stamp in the log.  Those come from Philosopher and can sit outside the run's
    own window -- on the reference run they land 10 minutes before the run
    started -- which shifts the whole chart.)
    """
    import datetime as dt

    m = re.search(r"log_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})",
                  os.path.basename(log_path))
    start = None
    if m:
        try:
            start = dt.datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                hour=int(m.group(2)), minute=int(m.group(3)), second=int(m.group(4)))
        except ValueError:
            start = None

    if start is not None and total_minutes:
        return start.isoformat(), (start + dt.timedelta(minutes=total_minutes)).isoformat()
    try:
        finish = dt.datetime.fromtimestamp(os.path.getmtime(log_path))
    except OSError:
        return (start.isoformat() if start else None), None
    return (start.isoformat() if start else None), finish.isoformat()


def run_name(spectrum: str) -> str:
    """``b1923_...122212.00312.00312.2`` -> ``b1923_...122212``."""
    base = os.path.basename(spectrum.replace("\\", "/"))
    return base.split(".")[0]


def stem(path: str) -> str:
    base = os.path.basename(path.replace("\\", "/"))
    return base.rsplit(".", 1)[0] if "." in base else base


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def read_manifest(workdir: str) -> List[dict]:
    """The four-column ``fragpipe-files.fp-manifest``, in file order."""
    path = os.path.join(workdir, "fragpipe-files.fp-manifest")
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            cells = (line.rstrip("\r\n").split("\t") + ["", "", ""])[:4]
            rows.append({"run": i, "file": cells[0], "name": stem(cells[0]),
                         "experiment": cells[1], "bioreplicate": cells[2],
                         "data_type": cells[3]})
    return rows


# --------------------------------------------------------------------------
# log parsing
# --------------------------------------------------------------------------
def _fragger_substages(milestones: Sequence[str]) -> List[Tuple[str, float]]:
    """(label, minutes) for MSFragger's own banners, in the order they ran."""
    out = []
    for text in milestones:
        for marker, label in FRAGGER_STAGES:
            if text.startswith(marker):
                m = _RE_TIME_MIN.search(text)
                if m:
                    out.append((label, float(m.group(1))))
                break
    return out


def parse_runtimes(lines: Sequence[str]) -> Tuple[List[dict], Optional[float]]:
    """Task runtimes for the Gantt chart, plus the run's reported total.

    ``fragpipe_progress`` already parses the ``Task Runtimes`` block, the
    ``ALL JOBS DONE`` total and the MSFragger banners, so this only applies the
    presentation rules on top: MSFragger is replaced in place by its sub-stages,
    the several Percolator entries are merged into one, and bookkeeping and
    zero-length tasks are dropped.
    """
    progress = RunProgress()
    for line in lines:
        progress.feed(line)
    fragger = _fragger_substages(progress.milestones)

    tasks: List[Tuple[str, float]] = []
    for name, minutes in progress.task_runtimes.items():
        task = name.strip()
        if task == "MSFragger":
            tasks.extend(fragger)
        elif task.startswith("Percolator"):
            for i, (existing, mins) in enumerate(tasks):
                if existing == "Percolator":
                    tasks[i] = ("Percolator", mins + minutes)
                    break
            else:
                tasks.append(("Percolator", minutes))
        elif minutes != 0 and task not in RUNTIME_SKIP:
            tasks.append((task, minutes))

    return [{"task": t, "minutes": round(m, 4)} for t, m in tasks], progress.total_minutes


def read_log(log_path: str) -> List[str]:
    """The log as a list of lines. Read once, parsed by several passes below."""
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def parse_run_order(lines: Sequence[str]) -> List[str]:
    """Input files in the order MSFragger processed them (run 001, 002, ...).

    The mass-calibration table names its rows by that index only, so the log has
    to supply the mapping.  Two forms occur: DIA searches number the files
    explicitly (``002. sample.mzML 9.0 s``), DDA searches list them in order
    before the first search (``sample.mzML: Scans = 42672; ...``).  The numbered
    form wins when both are present; the file order is not the manifest order.
    """
    numbered: Dict[int, str] = {}
    listed: List[str] = []
    for raw in lines:
        line = raw.strip()
        m = _RE_NUMBERED_FILE.match(line)
        if m:
            numbered.setdefault(int(m.group(1)), stem(m.group(2)))
            continue
        m = _RE_SCAN_LINE.match(line)
        if m:
            name = stem(m.group(1))
            if name not in listed:
                listed.append(name)
    if numbered:
        return [numbered[k] for k in sorted(numbered)]
    return listed


def parse_mass_calibration(lines: Sequence[str], run_order: Sequence[str] = ()) -> List[dict]:
    """MS1/MS2 median mass error before and after calibration, per run.

    The table lives in the MSFragger banner; values are ppm for MS1, and ppm or
    Da for MS2 depending on ``fragment_mass_units``.
    """
    rows: List[dict] = []
    in_region = False
    part = 0

    for raw in lines:
        line = raw.rstrip("\r\n")
        if (line.startswith("*********************MASS CALIBRATION AND PARAMETER OPTIMIZATION")
                or line.startswith("*********************************MASS CALIBRATION**")):
            in_region = True
            part += 1
        if in_region:
            s = line.strip()
            if s and s[0].isdigit():
                cells = [c.strip() for c in s.split("|")]
                if len(cells) >= 5:
                    pairs = [c.split() for c in cells[1:5]]
                    if all(len(p) >= 2 for p in pairs):
                        idx = cells[0]
                        try:
                            name = run_order[int(idx) - 1]
                        except (ValueError, IndexError):
                            name = idx
                        rows.append({
                            "part": part, "run_index": idx, "run": name,
                            "ms1_old_median": float(pairs[0][0]),
                            "ms1_old_mad": float(pairs[0][1]),
                            "ms1_calibrated_median": float(pairs[1][0]),
                            "ms1_calibrated_mad": float(pairs[1][1]),
                            "ms2_old_median": float(pairs[2][0]),
                            "ms2_old_mad": float(pairs[2][1]),
                            "ms2_calibrated_median": float(pairs[3][0]),
                            "ms2_calibrated_mad": float(pairs[3][1]),
                        })
        if (line.startswith("Finding the optimal parameters:")
                or line.startswith("**************************MASS CALIBRATION DONE")):
            in_region = False
    return rows


def parse_percolator_weights(lines: Sequence[str]) -> Dict[str, List[dict]]:
    """Mean normalized SVM weight per feature, per run, strongest first.

    Percolator prints one weight per cross-validation split; the report shows
    their mean.  Features that are all-zero (unused) and the ``m0`` internal
    features are dropped, as in FragSummarizer.
    """
    text = "".join(lines)
    pins = [stem(m.group(1)) for m in _RE_PIN.finditer(text)]
    out: Dict[str, List[dict]] = {}

    for block_index, m in enumerate(_RE_SVM_BLOCK.finditer(text)):
        lines = [l for l in m.group(1).strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        header = [h.strip() for h in lines[0].split("\t")]
        try:
            fi = header.index("FeatureName")
            si = [header.index("Split%d" % k) for k in (1, 2, 3)]
        except ValueError:
            continue

        features = []
        for line in lines[1:]:
            cells = line.split("\t")
            if len(cells) <= max([fi] + si):
                continue
            name = cells[fi].strip()
            if "m0" in name:
                continue
            splits = [_f(cells[i].strip()) for i in si]
            if any(v is None for v in splits) or all(v == 0 for v in splits):
                continue
            features.append({"feature": name,
                             "mean_weight": round(sum(splits) / 3.0, 4),
                             "splits": splits})
        features.sort(key=lambda d: abs(d["mean_weight"]), reverse=True)
        key = pins[block_index] if block_index < len(pins) else "block%d" % (block_index + 1)
        out[key] = features
    return out


def parse_tolerances(lines: Sequence[str]) -> dict:
    """Search tolerances, for annotating the mass-error panels."""
    out: dict = {}
    keys = {"precursor_true_tolerance": "ms1_tolerance",
            "fragment_mass_tolerance": "ms2_tolerance",
            "precursor_true_units": "ms1_units",
            "fragment_mass_units": "ms2_units"}
    for raw in lines:
        for prefix, name in keys.items():
            if raw.startswith(prefix + " = ") and name not in out:
                out[name] = raw.split(" = ", 1)[1].strip()
    for unit_key, tol_key in (("ms1_units", "ms1_tolerance"), ("ms2_units", "ms2_tolerance")):
        # units: 0 = Da, 1 = ppm, 2 = ppm expressed in thousands
        units = out.get(unit_key)
        tol = _f(out.get(tol_key, ""))
        if tol is not None:
            out[tol_key] = tol * 1000 if units == "2" else tol
        out[unit_key] = {"0": "Da", "1": "ppm", "2": "ppm"}.get(units, units)
    return out


# --------------------------------------------------------------------------
# series builders
# --------------------------------------------------------------------------
def category_series(values: Iterable[float]) -> dict:
    counts: Dict[int, int] = {}
    n = 0
    for v in values:
        counts[int(v)] = counts.get(int(v), 0) + 1
        n += 1
    return {"type": "category", "n": n,
            "categories": [k for k in sorted(counts)],
            "counts": [counts[k] for k in sorted(counts)]}


def histogram_series(values: Sequence[float], bins: Optional[int] = None) -> dict:
    """Bin values the way FragSummarizer does.

    Integer-valued data spanning less than 200 units gets one bin per integer --
    peptide length and charge must not be smeared across fractional bins.
    Otherwise the bin count is ``n/20`` clamped to [10, 50].
    """
    n = len(values)
    if n == 0:
        return {"type": "histogram", "n": 0, "bin_edges": [], "counts": []}

    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 0.5, hi + 0.5

    integral = bins is None and all(float(v).is_integer() for v in values)
    if integral and (hi - lo) < 200:
        start, stop = int(lo), int(hi)
        nbins, width = stop - start + 1, 1.0
        base = float(start)
    else:
        nbins = bins if bins else min(50, max(10, n // 20))
        width = (hi - lo) / nbins
        base = lo

    counts = [0] * nbins
    for v in values:
        b = int((v - base) / width) if width else 0
        counts[min(max(b, 0), nbins - 1)] += 1

    ordered = sorted(values)
    return {
        "type": "histogram", "n": n,
        "bin_edges": [round(base + i * width, 6) for i in range(nbins + 1)],
        "counts": counts,
        "min": round(lo, 6), "max": round(hi, 6),
        "mean": round(sum(values) / n, 6),
        "median": round(ordered[n // 2], 6),
    }


def scatter_series(xs: Sequence[float], ys: Sequence[float],
                   max_points: int = SCATTER_POINTS) -> dict:
    n = len(xs)
    step = max(1, n // max_points) if n > max_points else 1
    return {"type": "scatter", "n_total": n,
            "x": [round(v, 6) for v in xs[::step]],
            "y": [round(v, 6) for v in ys[::step]],
            "downsampled_by": step}


# --------------------------------------------------------------------------
# psm.tsv
# --------------------------------------------------------------------------
#: psm.tsv column -> key in the per-run output. Retention is handled separately
#: (it is in seconds and every plot shows minutes).
PSM_NUMERIC = {
    "Peptide Length": "peptide_length",
    "Charge": "charge",
    "Number of Missed Cleavages": "missed_cleavages",
    "Calculated M/Z": "mz",
    "Hyperscore": "hyperscore",
    "Delta Mass": "delta_mass",
}
CATEGORICAL = ("charge", "missed_cleavages")


class _RunAccumulator:
    """Numeric columns of one raw file, kept as compact ``array`` buffers.

    A list of Python floats costs ~4x more per value, which matters on a
    multi-million-row psm.tsv; ``--stride`` is the escape hatch beyond that.
    """

    def __init__(self) -> None:
        self.cols: Dict[str, array] = {k: array("d") for k in
                                       list(PSM_NUMERIC.values()) + ["retention"]}
        self.psms = 0
        self.peptides: set = set()
        self.proteins: set = set()

    def add(self, row: Dict[str, str]) -> None:
        self.psms += 1
        for column, key in PSM_NUMERIC.items():
            v = _f(row.get(column, ""))
            if v is not None:
                self.cols[key].append(v)
        rt = _f(row.get("Retention", ""))
        if rt is not None:
            self.cols["retention"].append(rt / 60.0)   # seconds -> minutes
        self.peptides.add(row.get("Modified Peptide", "") or row.get("Peptide", ""))
        self.proteins.add(row.get("Protein ID", "") or row.get("Protein", ""))

    def series(self) -> dict:
        out: dict = {}
        for key, values in self.cols.items():
            if not values:
                continue
            out[key] = (category_series(values) if key in CATEGORICAL
                        else histogram_series(values))
        mz, dm = self.cols["mz"], self.cols["delta_mass"]
        if mz and dm and len(mz) == len(dm):
            keep = [(x, y) for x, y in zip(mz, dm) if abs(y) < DELTA_MASS_WINDOW]
            if keep:
                out["mz_vs_delta_mass"] = scatter_series([p[0] for p in keep],
                                                         [p[1] for p in keep])
        rt = self.cols["retention"]
        if rt and mz and len(rt) == len(mz):
            out["rt_vs_mz"] = scatter_series(rt, mz)
        return out


def read_psm(psm_path: str, stride: int = 1) -> Tuple[Dict[str, _RunAccumulator], dict]:
    """One accumulator per raw file, plus the deduplicated overall distributions.

    The overall panels count each (spectrum, modified peptide, length, charge,
    missed cleavages) once, so a spectrum matched by several ranks does not
    inflate the distribution.
    """
    runs: Dict[str, _RunAccumulator] = {}
    seen: set = set()
    overall = {"peptide_length": array("d"), "charge": array("d"),
               "missed_cleavages": array("d")}

    for i, row in enumerate(iter_rows(psm_path)):
        if stride > 1 and i % stride:
            continue
        name = run_name(row.get("Spectrum", ""))
        runs.setdefault(name, _RunAccumulator()).add(row)

        key = (row.get("Spectrum", ""), row.get("Modified Peptide", ""),
               row.get("Peptide Length", ""), row.get("Charge", ""),
               row.get("Number of Missed Cleavages", ""))
        if key in seen:
            continue
        seen.add(key)
        for column, target in (("Peptide Length", "peptide_length"),
                               ("Charge", "charge"),
                               ("Number of Missed Cleavages", "missed_cleavages")):
            v = _f(row.get(column, ""))
            if v is not None:
                overall[target].append(v)
    return runs, overall


# --------------------------------------------------------------------------
# MSBooster
# --------------------------------------------------------------------------
def msbooster_plots(workdir: str) -> Dict[str, List[dict]]:
    """PNGs MSBooster wrote, grouped by run and labelled as the report labels them."""
    root = os.path.join(workdir, "MSBooster", "MSBooster_plots")
    if not os.path.isdir(root):
        return {}
    out: Dict[str, List[dict]] = {}
    for folder in sorted(os.listdir(root)):
        sub = os.path.join(root, folder)
        if not os.path.isdir(sub):
            continue
        for png in sorted(os.listdir(sub)):
            if not png.endswith(".png"):
                continue
            label = None
            for token, text in MSBOOSTER_PLOTS:
                if token == folder or token in png:
                    label = text
                    break
            if label is None:
                continue
            name = png.split("_edited")[0] if "_edited" in png else png[:-len(".png")]
            # RT_calibration_curves/ holds <run>.png and <run>_calibrated.png.
            # Both belong to the same run, and the labels have to say which is
            # which. (FragPipe's report keys on the file name, so the calibrated
            # curve lands under a run that does not exist and is never shown.)
            run = name
            if name.endswith("_calibrated"):
                run, label = name[:-len("_calibrated")], label + " (calibrated)"
            out.setdefault(run, []).append(
                {"path": os.path.join(sub, png), "label": label, "file": png})
    return out


# --------------------------------------------------------------------------
# identification counts
# --------------------------------------------------------------------------
def ids_per_run(data: dict) -> List[dict]:
    """PSM/peptide/protein counts per raw file, labelled by experiment.

    DIA and spectral-library workflows collapse every input into the workdir
    root, so counting report files gives a single bar for the whole run.
    Splitting psm.tsv by raw file restores one bar per run -- what FragPipe
    added to its own report for exactly this case.
    """
    run_to_exp = {}
    for row in data.get("manifest") or []:
        run_to_exp[row["name"]] = row["experiment"].strip() or row["name"]
    out = []
    for name, series in sorted((data.get("runs") or {}).items()):
        out.append({"run": name, "experiment": run_to_exp.get(name, name),
                    "psms": series.get("psms"), "peptides": series.get("peptides"),
                    "proteins": series.get("proteins")})
    return out


def id_counts(workdir: str, inv: Optional[dict] = None) -> List[dict]:
    """Rows in psm/peptide/protein.tsv per experiment folder.

    These are the numbers the report's ID bar charts show.  ``experiment`` is
    the folder name, or "" for a run whose reports sit in the workdir root
    (one unnamed experiment, or a spectral-library/DIA run).
    """
    inv = inv if inv is not None else discover(workdir)
    rows = []
    for exp, files in sorted(inv["experiments"].items()):
        row = {"experiment": exp}
        for name, key in (("psm.tsv", "psms"), ("peptide.tsv", "peptides"),
                          ("protein.tsv", "proteins")):
            path = files.get(name)
            row[key] = count_rows(path) if path else None
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------
def collect(workdir: str, stride: int = 1, skip_psm: bool = False) -> dict:
    """Every series the FragPipe report plots, as plain JSON-able data."""
    workdir = os.path.abspath(workdir)
    log = latest_log(workdir)
    data: dict = {"workdir": workdir, "log": log, "manifest": read_manifest(workdir),
                  "notes": []}

    if log:
        lines = read_log(log)
        run_order = parse_run_order(lines)
        runtimes, total = parse_runtimes(lines)
        data["run_order"] = run_order
        data["runtimes"] = runtimes
        data["total_minutes"] = total
        data["started_at"], data["finished_at"] = log_timeline(log, total)
        data["mass_calibration"] = parse_mass_calibration(lines, run_order)
        data["percolator_features"] = parse_percolator_weights(lines)
        data["search"] = parse_tolerances(lines)
    else:
        data["notes"].append("no log_*.txt: runtimes, mass calibration and "
                             "Percolator weights are unavailable")

    inv = discover(workdir)
    data["ids"] = id_counts(workdir, inv)
    data["msbooster_plots"] = msbooster_plots(workdir)

    data["runs"] = {}
    data["overall"] = {}
    if not skip_psm:
        psm_paths = [f["psm.tsv"] for f in inv["experiments"].values() if "psm.tsv" in f]
        if not psm_paths:
            data["notes"].append("no psm.tsv: per-run distributions are unavailable")
        overall_all = {"peptide_length": array("d"), "charge": array("d"),
                       "missed_cleavages": array("d")}
        for path in psm_paths:
            runs, overall = read_psm(path, stride=stride)
            for name, acc in runs.items():
                data["runs"][name] = acc.series()
                data["runs"][name]["psms"] = acc.psms
                data["runs"][name]["peptides"] = len(acc.peptides - {""})
                data["runs"][name]["proteins"] = len(acc.proteins - {""})
            for key, values in overall.items():
                overall_all[key].extend(values)
        for key, values in overall_all.items():
            if values:
                data["overall"][key] = (category_series(values)
                                        if key in CATEGORICAL
                                        else histogram_series(values))
        data["ids_per_run"] = ids_per_run(data)
        if stride > 1:
            data["notes"].append("psm.tsv sampled with stride %d: counts are "
                                 "roughly 1/%d of the true values" % (stride, stride))
    return data


# --------------------------------------------------------------------------
# TSV output
# --------------------------------------------------------------------------
def _write_tsv(path: str, header: Sequence[str], rows: Iterable[Sequence]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\t".join(header) + "\n")
        for row in rows:
            fh.write("\t".join("" if v is None else str(v) for v in row) + "\n")


def write_tsvs(data: dict, outdir: str) -> List[str]:
    """Tidy, long-format TSVs -- one file per plot family, ready for pandas or R."""
    os.makedirs(outdir, exist_ok=True)
    written = []

    def out(name: str, header: Sequence[str], rows: List[Sequence]) -> None:
        if not rows:
            return
        path = os.path.join(outdir, name)
        _write_tsv(path, header, rows)
        written.append(path)

    out("runtimes.tsv", ("task", "minutes"),
        [(r["task"], r["minutes"]) for r in data.get("runtimes", [])])

    out("ids.tsv", ("experiment", "psms", "peptides", "proteins"),
        [(r["experiment"], r["psms"], r["peptides"], r["proteins"])
         for r in data.get("ids", [])])

    out("ids_per_run.tsv", ("run", "experiment", "psms", "peptides", "proteins"),
        [(r["run"], r["experiment"], r["psms"], r["peptides"], r["proteins"])
         for r in data.get("ids_per_run", [])])

    mc = data.get("mass_calibration", [])
    out("mass_calibration.tsv",
        ("part", "run_index", "run", "level", "state", "median", "mad"),
        [(r["part"], r["run_index"], r["run"], level, state,
          r["ms%s_%s_median" % (level[-1], state)], r["ms%s_%s_mad" % (level[-1], state)])
         for r in mc for level in ("MS1", "MS2") for state in ("old", "calibrated")])

    out("percolator_features.tsv", ("run", "feature", "mean_weight"),
        [(run, f["feature"], f["mean_weight"])
         for run, feats in sorted(data.get("percolator_features", {}).items())
         for f in feats])

    rows: List[Sequence] = []
    for scope, series_map in [("overall", data.get("overall", {}))] + \
            [(run, s) for run, s in sorted(data.get("runs", {}).items())]:
        for key, series in sorted(series_map.items()):
            if not isinstance(series, dict):
                continue
            if series.get("type") == "category":
                for cat, count in zip(series["categories"], series["counts"]):
                    rows.append((scope, key, cat, cat, count))
            elif series.get("type") == "histogram":
                edges = series["bin_edges"]
                for i, count in enumerate(series["counts"]):
                    rows.append((scope, key, edges[i], edges[i + 1], count))
    out("distributions.tsv", ("scope", "series", "bin_start", "bin_end", "count"), rows)

    rows = []
    for run, series_map in sorted(data.get("runs", {}).items()):
        for key in ("mz_vs_delta_mass", "rt_vs_mz"):
            s = series_map.get(key)
            if s:
                rows.extend((run, key, x, y) for x, y in zip(s["x"], s["y"]))
    out("scatter.tsv", ("run", "series", "x", "y"), rows)

    out("msbooster_plots.tsv", ("run", "label", "path"),
        [(run, p["label"], p["path"])
         for run, plots in sorted(data.get("msbooster_plots", {}).items())
         for p in plots])
    return written


def describe(data: dict) -> str:
    """One-screen inventory of what was extracted."""
    L = ["workdir : %s" % data["workdir"],
         "log     : %s" % (data.get("log") or "none")]
    ids = data.get("ids", [])
    if ids:
        L.append("")
        L.append("Identifications")
        for r in ids:
            L.append("  %-28s PSM %-9s peptides %-9s proteins %s"
                     % (r["experiment"] or "(workdir root)", r["psms"],
                        r["peptides"], r["proteins"]))
    runs = data.get("runs", {})
    if runs:
        L.append("")
        L.append("Per-run series (%d run%s)" % (len(runs), "" if len(runs) == 1 else "s"))
        for name, series in sorted(runs.items()):
            keys = sorted(k for k, v in series.items() if isinstance(v, dict))
            L.append("  %-40s %d PSMs" % (name, series.get("psms", 0)))
            L.append("      %s" % ", ".join(keys))
    if data.get("runtimes"):
        L.append("")
        L.append("Runtimes: %d tasks, %s min total"
                 % (len(data["runtimes"]), data.get("total_minutes")))
    mc = data.get("mass_calibration", [])
    if mc:
        L.append("")
        L.append("Mass calibration (median, %s)"
                 % (data.get("search", {}).get("ms1_units") or "ppm"))
        for r in mc:
            L.append("  %-40s MS1 %6.2f -> %6.2f   MS2 %6.2f -> %6.2f"
                     % (r["run"], r["ms1_old_median"], r["ms1_calibrated_median"],
                        r["ms2_old_median"], r["ms2_calibrated_median"]))
    pf = data.get("percolator_features", {})
    if pf:
        L.append("")
        L.append("Percolator features: %d run(s), top by |weight|" % len(pf))
        for run, feats in sorted(pf.items()):
            top = ", ".join("%s %.3f" % (f["feature"], f["mean_weight"]) for f in feats[:5])
            L.append("  %-40s %s" % (run, top))
    mb = data.get("msbooster_plots", {})
    if mb:
        L.append("")
        L.append("MSBooster plots: %d run(s), %d PNGs"
                 % (len(mb), sum(len(v) for v in mb.values())))
    for note in data.get("notes", []):
        L.append("")
        L.append("NOTE: %s" % note)
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract the data behind FragPipe's QC plots.")
    ap.add_argument("--workdir", required=True, help="FragPipe output directory")
    ap.add_argument("--tsv", metavar="DIR", help="write tidy TSVs into DIR")
    ap.add_argument("--json", metavar="FILE", nargs="?", const="-",
                    help="write JSON to FILE (default: stdout)")
    ap.add_argument("--list", action="store_true",
                    help="print an inventory instead of the data")
    ap.add_argument("--stride", type=int, default=1,
                    help="keep every Nth PSM. Use on very large psm.tsv files; "
                         "shapes survive, absolute counts scale by 1/N.")
    ap.add_argument("--no-psm", action="store_true",
                    help="skip psm.tsv (log-derived series only, instant)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.workdir):
        print("not a directory: %s" % args.workdir, file=sys.stderr)
        return 2

    data = collect(args.workdir, stride=max(1, args.stride), skip_psm=args.no_psm)

    if args.tsv:
        for path in write_tsvs(data, args.tsv):
            print("wrote %s" % path)
    if args.list or (not args.tsv and not args.json):
        print(describe(data))
    if args.json:
        if args.json == "-":
            json.dump(data, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
