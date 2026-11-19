"""Fit a rating curve for every gage and build the website.

Reads the files written by scripts/fetch_usgs.py (data/usgs/<site_no>/) and writes _site/:
    index.html                      home page: map + gauge cards
    gauges/<site_no>/index.html     gauge page: info, map, flow/level charts, rating curve
    gauges/<site_no>/*.csv|json     downloads
    assets/                         CSS, JS and vendored Leaflet / Chart.js

Usage:
    python scripts/fetch_usgs.py && python scripts/build_site.py
    python scripts/build_site.py --only 01104500
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import shutil
import sys
import traceback
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import ratingcurve  # noqa: E402
from ratingcurve.ratings import PowerLawRating  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
esc = html.escape


# =========================================================================== modelling
def posterior_params(rating: PowerLawRating) -> list[dict]:
    """Posterior mean, SD and 95% interval of a, b_i, hs_i in real units.

    ln(Q) = a + sum_i b_i * ln(max(h - hs_i, 0) + ho_i), ho_0 = 0, ho_i = 1 for i > 0
    """
    post = rating.idata.posterior
    mu, std = float(rating.q_transform.mean_), float(rating.q_transform.std_)
    draws = {"a": mu + std * post["a"].values.reshape(-1)}
    b = post["b"].values.reshape(-1, rating.segments)
    hs = post["hs"].values.reshape(-1, rating.segments)
    labels = {"a": "a (intercept, ln Q)"}
    for i in range(rating.segments):
        draws[f"b{i}"] = std * b[:, i]
        draws[f"hs{i}"] = hs[:, i]
        labels[f"b{i}"] = "b0 (exponent)" if i == 0 else f"b{i} (exponent change above hs{i})"
        labels[f"hs{i}"] = "hs0 (stage of zero flow)" if i == 0 else f"hs{i} (breakpoint)"
    order = ["a"] + [f"{k}{i}" for i in range(rating.segments) for k in ("b", "hs")]
    return [{"key": k, "name": labels[k], "mean": float(np.mean(draws[k])), "sd": float(np.std(draws[k])),
             "lo": float(np.percentile(draws[k], 2.5)), "hi": float(np.percentile(draws[k], 97.5))}
            for k in order]


def rating_on_grid(rating: PowerLawRating, h: np.ndarray) -> pd.DataFrame:
    """Median and ~95% range of discharge for each stage in h."""
    with np.errstate(divide="ignore", invalid="ignore"):
        t = rating.table(h=h)
        lo = t["median"] / t["gse"] ** 2
        hi = t["median"] * t["gse"] ** 2
    t = t.assign(q_lower_95=lo.fillna(0.0), q_upper_95=hi.fillna(0.0))
    return t.fillna({"median": 0.0, "discharge": 0.0})


def nice_step(span: float, n: int = 400) -> float:
    raw = max(span, 1e-6) / n
    mag = 10 ** math.floor(math.log10(raw))
    return min((s * mag for s in (1, 2, 5, 10) if s * mag >= raw), default=raw)


def fit_gauge(site: dict, ddir: Path, out_dir: Path, defaults: dict, seed: int) -> dict:
    if (ddir / "error.txt").exists():
        raise RuntimeError("USGS download failed: " + (ddir / "error.txt").read_text())
    if not (ddir / "measurements.csv").exists():
        raise RuntimeError("no data downloaded yet — run scripts/fetch_usgs.py first")

    info = json.loads((ddir / "site.json").read_text())
    meas = pd.read_csv(ddir / "measurements.csv", parse_dates=["time"])
    daily = pd.read_csv(ddir / "daily.csv") if (ddir / "daily.csv").exists() else pd.DataFrame(
        columns=["date", "stage"])
    segments = int(site.get("segments", defaults.get("segments", 2)))

    meas = meas.dropna(subset=["stage", "q"])
    n_dry_meas = int((meas["q"] <= 0).sum())  # dry visits can't enter a power-law fit
    meas = meas[meas["q"] > 0].sort_values("time").reset_index(drop=True)
    if len(meas) < 5:
        raise ValueError(f"only {len(meas)} usable field measurements; need at least 5")
    q_sigma = meas["q_sigma"] if "q_sigma" in meas and meas["q_sigma"].notna().all() else None

    rating = PowerLawRating(segments=segments)
    rating.fit(h=meas["stage"], q=meas["q"], q_sigma=q_sigma, progressbar=False, random_seed=seed)
    params = posterior_params(rating)
    hs0 = next(p["mean"] for p in params if p["key"] == "hs0")

    # ---- one fine grid covering both the measurements and the daily water levels
    m_lo, m_hi = float(meas["stage"].min()), float(meas["stage"].max())
    d_stage = pd.to_numeric(daily["stage"], errors="coerce")
    lo = min(m_lo, float(d_stage.min()) if d_stage.notna().any() else m_lo)
    hi = max(m_hi + 0.1 * (m_hi - m_lo), float(d_stage.max()) if d_stage.notna().any() else m_hi)
    lo = max(lo, hs0 + 1e-3)
    step = nice_step(hi - lo)
    grid = np.round(np.arange(lo, hi + step, step), 6)
    tab = rating_on_grid(rating, grid)

    # ---- streamflow estimated from daily water levels
    daily = daily[["date"]].assign(stage=d_stage)
    has_stage = daily["stage"].notna()
    for col, src in (("q_est", "median"), ("q_est_lo", "q_lower_95"), ("q_est_hi", "q_upper_95")):
        daily[col] = np.where(has_stage, np.interp(daily["stage"].fillna(lo), tab["stage"], tab[src]), np.nan)
        daily.loc[has_stage & (daily["stage"] <= hs0), col] = 0.0
    daily["extrapolated"] = has_stage & ((daily["stage"] > m_hi) | (daily["stage"] < m_lo))

    # Headwater streams can run dry: below the zero-flow stage (hs0) flow is 0.
    est = daily.dropna(subset=["q_est"])
    no_flow = {"days": int((est["q_est"] <= 0).sum()), "of": int(len(est)), "dry_measurements": n_dry_meas}

    # ---- downloads
    out_dir.mkdir(parents=True, exist_ok=True)
    table = rating_on_grid(rating, np.round(np.arange(lo, m_hi + 0.1 * (m_hi - m_lo) + 0.01, 0.01), 2))
    table.rename(columns={"discharge": "mean"}).to_csv(out_dir / "rating_table.csv", index=False, float_format="%.4f")
    daily.to_csv(out_dir / "daily_streamflow.csv", index=False, float_format="%.4f")
    meas.to_csv(out_dir / "field_measurements.csv", index=False)
    summary = {
        "usgs_id": info["site_no"], "name": site.get("name") or info["usgs_name"], "segments": segments,
        "n_measurements": len(meas), "measurements_since": info.get("measurements_since"),
        "params": params, "no_flow": no_flow, "zero_flow_stage": hs0,
        "data_through": str(daily.loc[has_stage, "date"].max()) if has_stage.any() else None,
        "equation": "ln(Q) = a + Σ b_i · ln(max(h − hs_i, 0) + ho_i); ho_0 = 0, ho_i = 1 for i > 0",
    }
    (out_dir / "rating_parameters.json").write_text(json.dumps(summary, indent=2))

    # ---- compact data for the browser charts
    def r(x, nd=3):
        return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), nd)

    curve = tab[(tab["stage"] >= lo) & (tab["stage"] <= hi)]
    curve = curve.iloc[:: max(1, len(curve) // 250)]
    chart = {
        "daily": {
            "t": daily["date"].astype(str).tolist(),
            "stage": [r(v) for v in daily["stage"]],
            "q": [r(v) for v in daily["q_est"]], "q_lo": [r(v) for v in daily["q_est_lo"]],
            "q_hi": [r(v) for v in daily["q_est_hi"]],
        },
        "meas": {"t": meas["time"].dt.strftime("%Y-%m-%d").tolist(),
                 "stage": [r(v) for v in meas["stage"]], "q": [r(v) for v in meas["q"]]},
        "rating": {"stage": [r(v) for v in curve["stage"]], "q": [r(v) for v in curve["median"]],
                   "lo": [r(v) for v in curve["q_lower_95"]], "hi": [r(v) for v in curve["q_upper_95"]]},
        "measured_range": [m_lo, m_hi], "zero_flow_stage": r(hs0),
    }
    return {"site": site, "info": info, "summary": summary, "chart": chart, "meas": meas}


# =========================================================================== HTML
def fmt_q(x) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x:,.0f}" if x >= 100 else f"{x:,.1f}" if x >= 10 else f"{x:.2f}"


def fmt_num(x, nd=3) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    s = f"{x:,.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(str(s)[:10]).strftime("%b %-d, %Y")
    except ValueError:
        return str(s)


LOGO = """<svg class="logo-mark" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="19" fill="currentColor" opacity=".14"/>
<path d="M6 22c4-4 8-4 12 0s8 4 12 0 4-2 4-2" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
<path d="M9 28c3.5-3 7-3 10.5 0s7 3 10.5 0" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" opacity=".7"/>
<path d="M20 6l5 9h-10z" fill="currentColor"/></svg>"""


def submit_button(cfg: dict, label: str = "Submit a reading") -> str:
    url = (cfg["site"].get("submit_url") or "").strip()
    if url:
        return f'<a class="btn btn-accent" href="{esc(url)}">{esc(label)}</a>'
    return f'<span class="btn btn-accent disabled" title="Set submit_url in sites.yml">{esc(label)}</span>'


def layout(cfg: dict, title: str, body: str, root: str, active: str = "") -> str:
    s = cfg["site"]
    nav = [("Map", f"{root}index.html#map"), ("Staff gauges", f"{root}index.html#staff"),
           ("How it works", f"{root}index.html#how")]
    links = "".join(f'<a href="{h}"{" class=active" if n == active else ""}>{n}</a>' for n, h in nav)
    built = dt.datetime.now(dt.timezone.utc).strftime("%b %-d, %Y")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{root}assets/vendor/leaflet/leaflet.css"><link rel="stylesheet" href="{root}assets/site.css">
</head><body>
<a class="skip" href="#main">Skip to content</a>
<div class="demo-bar">Concept demo · <a href="{root}index.html#about">about this site</a></div>
<header class="site-header"><div class="container nav-row">
<a class="brand" href="{root}index.html">{LOGO}<span>{esc(s['title'])}</span></a>
<nav class="main-nav">{links}</nav>
<a class="btn btn-accent nav-cta" href="{root}index.html#staff">Submit a reading</a>
</div></header>
<main id="main">{body}</main>
<footer class="site-footer"><div class="container footer-grid">
<div><a class="brand brand-light" href="{root}index.html">{LOGO}<span>{esc(s['title'])}</span></a>
<p>{esc(s.get('intro', ''))}</p></div>
<div><h4>Data</h4><ul><li><a href="https://waterdata.usgs.gov/">USGS Water Data</a></li>
<li><a href="https://github.com/thodson-usgs/ratingcurve">ratingcurve</a> (<a href="https://doi.org/10.3390/hydrology11020014">Hodson et al., 2024</a>)</li>
<li>Basemap: <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">USGS The National Map</a></li></ul></div>
<div><h4>About</h4><p>{esc(s.get('disclaimer', ''))}</p><p class="muted">Built {built}.</p></div>
</div></footer>
<script src="{root}assets/vendor/leaflet/leaflet.js"></script>
<script src="{root}assets/vendor/chart.umd.min.js"></script>
<script src="{root}assets/vendor/chartjs-adapter-date-fns.bundle.min.js"></script>
<script src="{root}assets/site.js"></script>
</body></html>"""


def home_page(cfg: dict, gauges: list[dict]) -> str:
    s = cfg["site"]
    ok = [g for g in gauges if "error" not in g]
    n_meas = sum(g["summary"]["n_measurements"] for g in ok)
    ends = [g["summary"]["data_through"] for g in ok if g["summary"]["data_through"]]

    markers, items = [], []
    for g in gauges:
        site = g["site"]
        gid = str(site["usgs_id"])
        if "error" in g:
            items.append(f"""<li class="gauge-item error"><div><b>{esc(site.get('name', gid))}</b>
<span class="muted small">Not built on the last run: {esc(g['error'])}</span></div></li>""")
            continue
        sm, info = g["summary"], g["info"]
        markers.append({"name": sm["name"], "lat": info["lat"], "lon": info["lon"], "region": site.get("region", ""),
                        "url": f"gauges/{gid}/index.html"})
        items.append(f"""<li class="gauge-item"><a href="gauges/{gid}/index.html"><div><b>{esc(sm['name'])}</b>
<span class="muted small">{esc(site.get('region', ''))}</span></div><span class="arrow">→</span></a></li>""")

    body = f"""
<section class="hero"><div class="container hero-inner">
<p class="eyebrow light">Massachusetts river flow</p>
<h1>{esc(s['tagline'])}</h1>
<p class="lead">{esc(s.get('intro', ''))}</p>
<p><a class="btn btn-accent" href="#map">Explore the map</a> <a class="btn btn-ghost" href="#staff">Visit a staff gauge</a></p>
<dl class="hero-stats"><div><dt>Gauges</dt><dd>{len(ok)}</dd></div><div><dt>Flow measurements</dt><dd>{n_meas:,}</dd></div>
<div><dt>Data through</dt><dd>{fmt_date(max(ends)) if ends else '—'}</dd></div></dl>
</div><svg class="hero-wave" viewBox="0 0 1440 80" preserveAspectRatio="none" aria-hidden="true">
<path d="M0 40c160-30 320-30 480 0s320 30 480 0 320-30 480 0V80H0z"/></svg></section>

<section id="map" class="section"><div class="container">
<p class="eyebrow">Explore your rivers</p><h2>Gauges</h2>
<div class="map-layout"><div id="home-map" class="map map-lg" role="region" aria-label="Map of gauges"></div>
<ul class="gauge-list">{''.join(items)}</ul></div>
</div></section>

<section id="staff" class="section cta"><div class="container cta-inner">
<div><p class="eyebrow light">Get involved</p><h2>Visit a staff gauge</h2>
<p>Each site has a staff gauge, a ruler in the river. Read the water level where the surface meets the gauge and send it in.
Your readings check the loggers and fill gaps between downloads. Dry? Report that too.</p></div>
<div>{submit_button(cfg)}</div>
</div></section>

<section id="how" class="section"><div class="container">
<p class="eyebrow">How it works</p><h2>From water level to streamflow</h2>
<div class="pillars">
<div class="pillar"><span class="num">1</span><h3>Record</h3><p>Loggers in the river record water level. Volunteers download them a few times a year.</p></div>
<div class="pillar"><span class="num">2</span><h3>Read</h3><p>Visitors read the staff gauge and submit readings, a check on the loggers.</p></div>
<div class="pillar"><span class="num">3</span><h3>Fit</h3><p>Flow measurements build a rating curve with <a href="https://github.com/thodson-usgs/ratingcurve">ratingcurve</a>,
which turns water level into streamflow, with uncertainty.</p></div>
</div></div></section>

<section id="about" class="section alt"><div class="container narrow">
<p class="eyebrow">About this demo</p>
<p>This prototype uses USGS streamgage data in place of logger records. Streamflow updates whenever new logger data are added.
Many headwater streams run dry part of the year; those days show as zero flow.</p>
<p class="muted small">{esc(s.get('disclaimer', ''))}</p></div></section>
<script type="application/json" id="home-data">{json.dumps(markers)}</script>"""
    return layout(cfg, f"{s['title']} — River flow across Massachusetts", body, root="", active="Map")


def gauge_page(cfg: dict, g: dict) -> str:
    u, site, info, sm = cfg["units"], g["site"], g["info"], g["summary"]
    su, qu = u["stage"], u["discharge"]
    gid = info["site_no"]
    nf = sm["no_flow"]
    loc = ", ".join(x for x in [info.get("county"), "MA"] if x)

    rows = [
        ("Gauge ID", esc(gid)),
        ("Location", esc(loc)),
        ("Coordinates", f"{info['lat']:.5f}, {info['lon']:.5f}" if info.get("lat") is not None else "—"),
        ("Drainage area", f"{info['drainage_area_mi2']:,.1f} mi²" if info.get("drainage_area_mi2") else "—"),
        ("Data through", fmt_date(sm["data_through"]) if sm["data_through"] else "—"),
        ("Flow measurements", f"{sm['n_measurements']}"
         + (f" + {nf['dry_measurements']} dry visits" if nf["dry_measurements"] else "")),
        ("No-flow days", f"{nf['days']:,} of {nf['of']:,}" if nf["of"] else "—"),
    ]
    info_rows = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in rows)
    staff_note = site.get("staff_gauge") or "Find the staff gauge at the marked spot on the map."

    prow = "".join(f"<tr><td>{esc(p['name'])}</td><td>{fmt_num(p['mean'], 3)}</td>"
                   f"<td>{fmt_num(p['lo'], 3)} – {fmt_num(p['hi'], 3)}</td></tr>" for p in sm["params"])
    mrows = "".join(f"<tr><td>{t.strftime('%Y-%m-%d')}</td><td>{fmt_num(h, 2)}</td><td>{fmt_q(q)}</td></tr>"
                    for t, h, q in zip(g["meas"]["time"], g["meas"]["stage"], g["meas"]["q"]))
    data = dict(g["chart"], units=u, name=sm["name"], lat=info.get("lat"), lon=info.get("lon"))

    body = f"""
<section class="page-head"><div class="container">
<nav class="crumbs"><a href="../../index.html">Home</a> / <a href="../../index.html#map">Gauges</a> / <span>{esc(sm['name'])}</span></nav>
<p class="eyebrow">{esc(site.get('region', ''))}</p><h1>{esc(sm['name'])}</h1>
</div></section>

<section class="section tight"><div class="container gauge-top">
<div class="panel"><h2 class="panel-title">Gauge info</h2><dl class="info">{info_rows}</dl>
<div class="staff-box"><h3>Read the staff gauge</h3><p>{esc(staff_note)} Read the water level and submit it. If the stream is dry, report that too.</p>{submit_button(cfg)}</div></div>
<div class="panel map-panel"><div id="gauge-map" class="map" role="region" aria-label="Gauge location map"></div></div>
</div></section>

<section class="section tight"><div class="container"><div class="panel">
<div class="panel-head"><h2 class="panel-title">Gauge data</h2>
<div class="tabs" role="tablist"><button role="tab" class="tab active" data-series="flow">Streamflow</button>
<button role="tab" class="tab" data-series="level">Water level</button></div></div>
<div class="toolbar"><div class="ranges" role="group" aria-label="Time range">
<button data-range="30">1 month</button><button data-range="91">3 months</button><button data-range="182">6 months</button>
<button data-range="365" class="active">1 year</button><button data-range="0">All</button></div>
<label class="check"><input type="checkbox" id="flow-log"> Log scale</label></div>
<div class="chart-box"><canvas id="ts-chart"></canvas></div>
<p class="muted small" id="ts-note">Updated when the loggers are downloaded. Shading shows the ~95% range.
Flow is 0 when the water level drops below the zero-flow stage ({fmt_num(sm['zero_flow_stage'], 2)} {esc(su)}), the dashed line on the water-level chart.</p>
<p class="downloads"><a href="daily_streamflow.csv" download>Download data (CSV)</a></p>
</div></div></section>

<section class="section tight"><div class="container"><div class="panel">
<div class="panel-head"><h2 class="panel-title">Rating curve</h2>
<label class="check"><input type="checkbox" id="rating-log" checked> Log scale</label></div>
<div class="rating-grid"><div class="chart-box tall"><canvas id="rating-chart"></canvas></div>
<div>
<table class="params"><thead><tr><th>Parameter</th><th>Mean</th><th>95% interval</th></tr></thead><tbody>{prow}</tbody></table>
<p class="muted small"><code>{esc(sm['equation'])}</code></p>
<p class="downloads"><a href="rating_table.csv" download>Rating table (CSV)</a><a href="rating_parameters.json" download>Parameters (JSON)</a></p>
</div></div></div></div></section>

<section class="section tight last"><div class="container"><div class="panel">
<details><summary><h2 class="panel-title">Flow measurements ({sm['n_measurements']})</h2></summary>
<div class="scroll"><table><thead><tr><th>Date</th><th>Water level ({esc(su)})</th><th>Streamflow ({esc(qu)})</th></tr></thead>
<tbody>{mrows}</tbody></table></div>
<p class="downloads"><a href="field_measurements.csv" download>Download (CSV)</a></p></details>
</div></div></section>
<script type="application/json" id="gauge-data">{json.dumps(data)}</script>"""
    return layout(cfg, f"{sm['name']} — {cfg['site']['title']}", body, root="../../")


# =========================================================================== main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "sites.yml"))
    ap.add_argument("--data", default=str(ROOT / "data" / "usgs"))
    ap.add_argument("--out", default=str(ROOT / "_site"))
    ap.add_argument("--only", help="build only this USGS site number")
    ap.add_argument("--seed", type=int, default=2024)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    cfg.setdefault("site", {}).setdefault("title", "River Flow")
    cfg["site"].setdefault("tagline", cfg["site"]["title"])
    cfg["units"] = {"stage": "ft", "discharge": "ft³/s", **(cfg.get("units") or {})}
    cfg["defaults"] = cfg.get("defaults") or {}

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "site_assets", out / "assets")
    (out / ".nojekyll").write_text("")

    gauges, failures = [], 0
    for site in cfg["sites"]:
        gid = str(site["usgs_id"]).strip()
        if args.only and gid != args.only:
            continue
        print(f"Fitting USGS {gid} ...", flush=True)
        gdir = out / "gauges" / gid
        try:
            g = fit_gauge(site, Path(args.data) / gid, gdir, cfg["defaults"], args.seed)
            (gdir / "index.html").write_text(gauge_page(cfg, g))
            gauges.append(g)
            nf = g["summary"]["no_flow"]
            print(f"  ok: {g['summary']['n_measurements']} measurements, {nf['days']} no-flow days")
        except Exception as exc:
            failures += 1
            traceback.print_exc()
            print(f"::warning title=Gauge {gid} not built::{exc}")
            gauges.append({"site": site, "error": f"{type(exc).__name__}: {exc}"})

    (out / "index.html").write_text(home_page(cfg, gauges))
    print(f"Wrote {out} ({len(gauges) - failures} gauges ok, {failures} failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
