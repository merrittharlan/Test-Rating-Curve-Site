"""Download the data each gage needs from the USGS Water Data APIs.

For every site in sites.yml this writes data/usgs/<site_no>/:
    site.json           name, coordinates, drainage area, county
    measurements.csv    paired field measurements: time, stage, q
    daily.csv           daily mean gage height (stage)
    error.txt           only if something essential failed

Usage:
    python scripts/fetch_usgs.py [--config sites.yml] [--out data/usgs]

Optional: set API_USGS_PAT to a USGS API key (https://api.waterdata.usgs.gov/signup/)
for higher rate limits. Not needed for a handful of gages.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

import pandas as pd
import yaml

# A blank key (e.g. an unset GitHub secret) should mean "no key", not an empty key.
if not os.environ.get("API_USGS_PAT", "").strip():
    os.environ.pop("API_USGS_PAT", None)

from dataretrieval import waterdata as wd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GAGE_HEIGHT, DISCHARGE = "00065", "00060"


# --------------------------------------------------------------------------- parsing helpers
def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def location_info(loc: pd.DataFrame, site_no: str) -> dict:
    """Name, coordinates and descriptors from a monitoring-locations response."""
    if loc is None or loc.empty:
        raise ValueError(f"USGS has no monitoring location {site_no}")
    row = loc.iloc[0]
    lon = lat = None
    geom = row.get("geometry")
    if geom is not None:
        if hasattr(geom, "x"):  # shapely Point (geopandas installed)
            lon, lat = float(geom.x), float(geom.y)
        else:  # plain list of coordinates [lon, lat]
            coords = list(geom)
            if len(coords) >= 2:
                lon, lat = float(coords[0]), float(coords[1])
    for lat_col, lon_col in (("latitude", "longitude"), ("dec_lat_va", "dec_long_va")):
        if (lat is None or lon is None) and lat_col in row and lon_col in row:
            lat, lon = float(row[lat_col]), float(row[lon_col])

    def get(*cols):
        for c in cols:
            v = row.get(c)
            if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
                return v
        return None

    da = get("drainage_area")
    return {
        "site_no": site_no,
        "usgs_name": get("monitoring_location_name") or site_no,
        "lat": lat,
        "lon": lon,
        "drainage_area_mi2": float(da) if da is not None else None,
        "county": get("county_name"),
        "state": get("state_name"),
        "huc": get("hydrologic_unit_code"),
    }


QUALITY_SIGMA = {"excellent": 0.02, "good": 0.05, "fair": 0.08, "poor": 0.12}


def pair_measurements(fm: pd.DataFrame) -> pd.DataFrame:
    """Turn field-measurement rows (one per parameter) into one row per visit:
    time, stage, q, quality, q_sigma."""
    if fm is None or fm.empty:
        return pd.DataFrame(columns=["time", "stage", "q", "quality", "q_sigma"])
    fm = fm.copy()
    fm["value"] = _num(fm["value"])
    fm["time"] = pd.to_datetime(fm["time"], utc=True, errors="coerce")
    fm = fm.dropna(subset=["value", "time"])
    key = "field_visit_id" if "field_visit_id" in fm.columns and fm["field_visit_id"].notna().any() else "time"
    if key == "time":  # no visit ids: group by calendar date
        fm["_visit"] = fm["time"].dt.floor("D")
        key = "_visit"

    # Look for a measurement-quality rating anywhere in the text columns.
    text_cols = [c for c in fm.columns if c not in ("value", "time", key)
                 and (fm[c].dtype == object or pd.api.types.is_string_dtype(fm[c]))]

    def quality(rows: pd.DataFrame) -> str | None:
        for c in text_cols:
            for v in rows[c].dropna().astype(str):
                lv = v.lower()
                for q in QUALITY_SIGMA:
                    if q in lv:
                        return q
        return None

    out = []
    for _, g in fm.groupby(key):
        gh = g.loc[g["parameter_code"].astype(str) == GAGE_HEIGHT, "value"]
        q = g.loc[g["parameter_code"].astype(str) == DISCHARGE]
        if gh.empty or q.empty:
            continue
        qual = quality(q)
        qv = float(q["value"].mean())
        out.append({
            "time": q["time"].min(),
            "stage": float(gh.mean()),
            "q": qv,
            "quality": qual or "",
            "q_sigma": qv * QUALITY_SIGMA.get(qual or "good", 0.05),
        })
    df = pd.DataFrame(out)
    if df.empty:
        return pd.DataFrame(columns=["time", "stage", "q", "quality", "q_sigma"])
    return df[(df["q"] > 0)].sort_values("time").reset_index(drop=True)


def daily_series(dv: pd.DataFrame) -> pd.DataFrame:
    """Daily means -> columns date, stage."""
    if dv is None or dv.empty:
        return pd.DataFrame(columns=["date", "stage"])
    dv = dv.copy()
    dv["value"] = _num(dv["value"])
    dv["date"] = pd.to_datetime(dv["time"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "statistic_id" in dv.columns:
        dv = dv[dv["statistic_id"].astype(str).isin(["00003", "3"])]
    wide = dv.pivot_table(index="date", columns=dv["parameter_code"].astype(str), values="value", aggfunc="mean")
    wide = wide.rename(columns={GAGE_HEIGHT: "stage", DISCHARGE: "q_usgs"})
    for c in ("stage", "q_usgs"):
        if c not in wide.columns:
            wide[c] = pd.NA
    return wide[["stage"]].reset_index().sort_values("date")


# --------------------------------------------------------------------------- fetching
def fetch_site(site: dict, out: Path, defaults: dict) -> None:
    site_no = str(site["usgs_id"]).strip()
    mlid = f"USGS-{site_no}"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        f.unlink()

    today = dt.date.today()
    since = str(site.get("measurements_since", defaults.get("measurements_since", "2015-10-01")))
    years = int(site.get("timeseries_years", defaults.get("timeseries_years", 3)))
    ts_start = (today - dt.timedelta(days=365 * years)).isoformat()

    loc, _ = wd.get_monitoring_locations(monitoring_location_id=mlid)
    info = location_info(loc, site_no)

    fm, _ = wd.get_field_measurements(
        monitoring_location_id=mlid, parameter_code=[GAGE_HEIGHT, DISCHARGE],
        time=f"{since}/..", skip_geometry=True)
    meas = pair_measurements(fm)
    if len(meas) < 5:
        raise ValueError(f"only {len(meas)} paired stage/discharge field measurements since {since}")
    meas.to_csv(out / "measurements.csv", index=False)

    try:
        dv, _ = wd.get_daily(
            monitoring_location_id=mlid, parameter_code=GAGE_HEIGHT,
            statistic_id="00003", time=f"{ts_start}/..", skip_geometry=True)
        daily = daily_series(dv)
    except Exception as exc:  # the page still works without a time series
        print(f"  ! daily values unavailable: {exc}")
        daily = daily_series(None)
    daily.to_csv(out / "daily.csv", index=False)


    info.update({
        "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "measurements_since": since,
        "n_measurements": len(meas),
        "n_daily_stage": int(daily["stage"].notna().sum()),
    })
    (out / "site.json").write_text(json.dumps(info, indent=2, default=str))
    print(f"  {info['usgs_name']}: {len(meas)} measurements, "
          f"{info['n_daily_stage']} daily water levels")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "sites.yml"))
    ap.add_argument("--out", default=str(ROOT / "data" / "usgs"))
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    defaults = cfg.get("defaults") or {}
    for site in cfg["sites"]:
        site_no = str(site["usgs_id"]).strip()
        out = Path(args.out) / site_no
        print(f"Fetching USGS {site_no} ...", flush=True)
        try:
            fetch_site(site, out, defaults)
        except Exception as exc:
            traceback.print_exc()
            out.mkdir(parents=True, exist_ok=True)
            (out / "error.txt").write_text(f"{type(exc).__name__}: {exc}")
            print(f"::warning title=USGS download failed ({site_no})::{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
