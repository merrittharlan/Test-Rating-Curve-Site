"""Fit a rating curve for every site in sites.yml and write a static website.

Usage:
    python scripts/build_site.py                # reads sites.yml, writes _site/
    python scripts/build_site.py --config sites.yml --out _site --only green-channel

Uses the USGS `ratingcurve` package (segmented power law, fit with PyMC).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import shutil
import sys
import traceback
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import ratingcurve  # noqa: E402
from ratingcurve.ratings import PowerLawRating  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
esc = html.escape


# --------------------------------------------------------------------------- data
def load_measurements(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = {"stage", "q"} - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing required column(s): {', '.join(sorted(missing))}")
    df = df.dropna(subset=["stage", "q"])
    df = df[df["q"] > 0]
    if len(df) < 5:
        raise ValueError(f"{path.name} has only {len(df)} usable measurements; need at least 5")
    return df.sort_values("stage").reset_index(drop=True)


# --------------------------------------------------------------------------- fitting
def posterior_params(rating: PowerLawRating) -> list[dict]:
    """Posterior mean, SD and 95% interval of a, b_i, hs_i in real units."""
    post = rating.idata.posterior
    mu, std = float(rating.q_transform.mean_), float(rating.q_transform.std_)
    draws = {"a": mu + std * post["a"].values.reshape(-1)}
    b = post["b"].values.reshape(-1, rating.segments)
    hs = post["hs"].values.reshape(-1, rating.segments)
    labels = {"a": "a (intercept, ln Q)"}
    for i in range(rating.segments):
        draws[f"b{i}"] = std * b[:, i]
        draws[f"hs{i}"] = hs[:, i]
        labels[f"b{i}"] = f"b{i} (exponent)" if i == 0 else f"b{i} (exponent change at hs{i})"
        labels[f"hs{i}"] = f"hs{i} (gage height of zero flow)" if i == 0 else f"hs{i} (breakpoint)"
    order = ["a"] + [f"{k}{i}" for i in range(rating.segments) for k in ("b", "hs")]
    return [
        {
            "name": labels[k],
            "mean": float(np.mean(draws[k])),
            "sd": float(np.std(draws[k])),
            "lo": float(np.percentile(draws[k], 2.5)),
            "hi": float(np.percentile(draws[k], 97.5)),
        }
        for k in order
    ]


def fit_site(site: dict, out_dir: Path, units: dict, seed: int) -> dict:
    df = load_measurements(ROOT / site["file"])
    segments = int(site.get("segments", 2))
    q_sigma = df["q_sigma"] if "q_sigma" in df.columns and df["q_sigma"].notna().all() else None

    rating = PowerLawRating(segments=segments)
    rating.fit(h=df["stage"], q=df["q"], q_sigma=q_sigma, progressbar=False, random_seed=seed)

    out_dir.mkdir(parents=True, exist_ok=True)

    # Rating curve plot
    fig, ax = plt.subplots(figsize=(7, 6))
    rating.plot(ax=ax)
    ax.set_xlabel(f"Discharge ({units['discharge']})")
    ax.set_ylabel(f"Stage ({units['stage']})")
    ax.set_title(site["name"], fontsize=11)
    fig.tight_layout()
    fig.savefig(out_dir / "rating.png", dpi=150)
    plt.close(fig)

    # Residuals plot
    fig, ax = plt.subplots(figsize=(7, 4))
    rating.plot_residuals(ax=ax)
    ax.set_ylabel(f"Stage ({units['stage']})")
    fig.tight_layout()
    fig.savefig(out_dir / "residuals.png", dpi=150)
    plt.close(fig)

    # Rating table (full resolution CSV)
    table = rating.table()
    lo95 = table["median"] / table["gse"] ** 2
    hi95 = table["median"] * table["gse"] ** 2
    table = table.assign(q_lower_95=lo95, q_upper_95=hi95)
    table.to_csv(out_dir / "rating_table.csv", index=False, float_format="%.4f")

    # Parameters in real (denormalized) units, per posterior draw:
    #   ln(Q) = a + sum_i b_i * ln(max(h - hs_i, 0) + ho_i),  ho_0 = 0, ho_i = 1
    params = posterior_params(rating)

    rating.save(str(out_dir / "rating_fit.nc"))
    df.to_csv(out_dir / "measurements.csv", index=False)

    result = {
        "id": site["id"],
        "name": site["name"],
        "segments": segments,
        "n_obs": len(df),
        "stage_range": [float(df["stage"].min()), float(df["stage"].max())],
        "q_range": [float(df["q"].min()), float(df["q"].max())],
        "used_q_sigma": q_sigma is not None,
        "params": params,
        "equation": "ln(Q) = a + Σ b_i · ln(max(h − hs_i, 0) + ho_i), ho_0 = 0, ho_i = 1 for i > 0",
    }
    (out_dir / "params.json").write_text(json.dumps(result, indent=2))
    return {"result": result, "table": table, "measurements": df}


# --------------------------------------------------------------------------- HTML
CSS = """
:root{--fg:#1d2733;--muted:#5d6b7a;--line:#dfe4ea;--bg:#f7f9fb;--accent:#1f6f8b;--bad:#b3261e}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg)}
header{background:#fff;border-bottom:1px solid var(--line)}header .wrap{padding:20px 24px}
header h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:var(--muted)}
.wrap{max-width:1040px;margin:0 auto;padding:24px}a{color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden;text-decoration:none;color:inherit;display:block}
.card:hover{border-color:var(--accent)}.card img{width:100%;display:block;border-bottom:1px solid var(--line)}
.card .body{padding:12px 14px}.card h2{font-size:16px;margin:0 0 4px}.meta{color:var(--muted);font-size:13px}
.err{border-color:var(--bad)}.err h2{color:var(--bad)}pre{white-space:pre-wrap;font-size:12px;background:#fbeaea;padding:8px;border-radius:6px}
section{background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:18px}
section h2{margin:0 0 10px;font-size:17px}.plots{display:grid;grid-template-columns:1fr;gap:12px}.plots img{max-width:100%;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
th,td{padding:5px 10px;border-bottom:1px solid var(--line);text-align:right}th{background:#f1f4f7;position:sticky;top:0}
th:first-child,td:first-child{text-align:left}.scroll{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:6px}
.dl a{display:inline-block;margin:0 10px 6px 0;padding:6px 12px;border:1px solid var(--accent);border-radius:6px;text-decoration:none}
.stats{display:flex;flex-wrap:wrap;gap:22px}.stats div b{display:block;font-size:18px}
footer{color:var(--muted);font-size:12.5px;text-align:center;padding:10px 24px 30px}
"""


def page(title: str, body: str, subtitle: str = "", root: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><link rel="stylesheet" href="{root}style.css"></head>
<body><header><div class="wrap"><h1>{esc(title)}</h1>{f'<p>{subtitle}</p>' if subtitle else ''}</div></header>
<main class="wrap">{body}</main>
<footer>Fit with <a href="https://github.com/thodson-usgs/ratingcurve">ratingcurve</a> {esc(ratingcurve.__version__ if hasattr(ratingcurve, '__version__') else '')}
(Hodson et al., 2024, <a href="https://doi.org/10.3390/hydrology11020014">Hydrology 11(2):14</a>) ·
built {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. Provisional — review before operational use.</footer>
</body></html>"""


def fmt(x: float, nd: int = 3) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:.{nd}g}" if abs(x) < 1 else f"{x:,.{nd}f}".rstrip("0").rstrip(".")


def site_page(fit: dict, units: dict, title: str) -> str:
    r, table, meas = fit["result"], fit["table"], fit["measurements"]
    su, qu = units["stage"], units["discharge"]

    stats = f"""<div class="stats">
<div><b>{r['n_obs']}</b><span class="meta">measurements</span></div>
<div><b>{r['segments']}</b><span class="meta">segments</span></div>
<div><b>{fmt(r['stage_range'][0])}–{fmt(r['stage_range'][1])} {esc(su)}</b><span class="meta">measured stage</span></div>
<div><b>{fmt(r['q_range'][0])}–{fmt(r['q_range'][1])} {esc(qu)}</b><span class="meta">measured discharge</span></div>
<div><b>{'yes' if r['used_q_sigma'] else 'no'}</b><span class="meta">measurement uncertainty used</span></div></div>"""

    prow = "".join(
        f"<tr><td>{esc(p['name'])}</td><td>{fmt(p['mean'],4)}</td><td>{fmt(p['sd'],3)}</td>"
        f"<td>{fmt(p['lo'],4)} – {fmt(p['hi'],4)}</td></tr>" for p in r["params"])
    ptable = f"""<table><thead><tr><th>Parameter</th><th>Mean</th><th>SD</th><th>95% interval</th></tr></thead>
<tbody>{prow}</tbody></table>
<p class="meta">Model (segmented power law): <code>{esc(r['equation'])}</code>, with h and hs in {esc(su)}
and Q in {esc(qu)}. Each b<sub>i</sub> after the first is the change in exponent above breakpoint hs<sub>i</sub>,
so the exponent of the top segment is b0 + b1 + …</p>"""

    # Show rating table at ~0.1 stage increments; full 0.01 table is in the CSV.
    step = max(1, len(table) // 120)
    shown = table.iloc[::step]
    trows = "".join(
        f"<tr><td>{row.stage:.2f}</td><td>{fmt(row.median)}</td><td>{fmt(row.discharge)}</td>"
        f"<td>{fmt(row.q_lower_95)} – {fmt(row.q_upper_95)}</td><td>{row.gse:.4f}</td></tr>"
        for row in shown.itertuples())
    rtable = f"""<div class="scroll"><table><thead><tr><th>Stage ({esc(su)})</th><th>Median Q</th><th>Mean Q</th>
<th>≈95% range</th><th>GSE</th></tr></thead><tbody>{trows}</tbody></table></div>
<p class="meta">Discharge in {esc(qu)}. GSE = geometric standard error; the ≈95% range is median ÷ GSE² to median × GSE².
Table extends 10% beyond the highest measurement — treat that extrapolation with caution.</p>"""

    mcols = [c for c in ["datetime", "stage", "q", "q_sigma"] if c in meas.columns]
    mhead = "".join(f"<th>{esc(c)}</th>" for c in mcols)
    mrows = "".join("<tr>" + "".join(
        f"<td>{esc(str(v)) if c == 'datetime' else fmt(float(v))}</td>" for c, v in zip(mcols, row)) + "</tr>"
        for row in meas[mcols].itertuples(index=False))

    body = f"""<p><a href="../../index.html">← All sites</a></p>
<section><h2>Summary</h2>{stats}
<p class="dl" style="margin-top:14px"><a href="rating_table.csv" download>Rating table (CSV, 0.01 {esc(su)} steps)</a>
<a href="params.json" download>Parameters (JSON)</a><a href="rating_fit.nc" download>Full posterior (NetCDF)</a>
<a href="measurements.csv" download>Measurements (CSV)</a></p></section>
<section><h2>Rating curve</h2><div class="plots"><img src="rating.png" alt="Fitted rating curve with measurements and uncertainty band">
<img src="residuals.png" alt="Residuals by stage"></div>
<p class="meta">Gray band: posterior uncertainty. Dotted lines: fitted segment breakpoints. Residuals should scatter
evenly around zero; a systematic bend suggests trying another segment.</p></section>
<section><h2>Fitted parameters</h2>{ptable}</section>
<section><h2>Rating table</h2>{rtable}</section>
<section><h2>Measurements</h2><div class="scroll"><table><thead><tr>{mhead}</tr></thead><tbody>{mrows}</tbody></table></div></section>"""
    return page(r["name"], body, subtitle=esc(title), root="../../")


def index_page(title: str, results: list[dict]) -> str:
    cards = []
    for item in results:
        if "error" in item:
            cards.append(f"""<div class="card err"><div class="body"><h2>{esc(item['name'])}</h2>
<p class="meta">Fit failed — fix the data or settings in sites.yml and push again.</p><pre>{esc(item['error'])}</pre></div></div>""")
        else:
            r = item["result"]
            cards.append(f"""<a class="card" href="sites/{esc(r['id'])}/index.html"><img src="sites/{esc(r['id'])}/rating.png" alt="">
<div class="body"><h2>{esc(r['name'])}</h2><div class="meta">{r['n_obs']} measurements · {r['segments']} segment(s)</div></div></a>""")
    body = f'<div class="cards">{"".join(cards)}</div>'
    return page(title, body, subtitle="Segmented power-law stage–discharge ratings, refit automatically when data change.")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "sites.yml"))
    ap.add_argument("--out", default=str(ROOT / "_site"))
    ap.add_argument("--only", help="fit only this site id")
    ap.add_argument("--seed", type=int, default=2024)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    title = cfg.get("title", "Rating Curves")
    units = {"stage": "ft", "discharge": "ft³/s", **(cfg.get("units") or {})}
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "style.css").write_text(CSS)
    (out / ".nojekyll").write_text("")

    results, failures = [], 0
    for site in cfg["sites"]:
        if args.only and site["id"] != args.only:
            continue
        print(f"Fitting {site['id']} ...", flush=True)
        site_dir = out / "sites" / site["id"]
        try:
            fit = fit_site(site, site_dir, units, args.seed)
            (site_dir / "index.html").write_text(site_page(fit, units, title))
            results.append(fit)
        except Exception as exc:  # keep building the other sites
            failures += 1
            traceback.print_exc()
            print(f"::error title=Rating fit failed ({site['id']})::{exc}")
            results.append({"name": site.get("name", site["id"]), "error": f"{type(exc).__name__}: {exc}"})

    (out / "index.html").write_text(index_page(title, results))
    print(f"Wrote {out} ({len(results) - failures} ok, {failures} failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
