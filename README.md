# Mass River Flow — a rating-curve website demo

A prototype website that turns water levels into streamflow for a network of river gauges. When the
site is built (on each push, or from the Actions tab), a GitHub Action:

1. downloads water levels and flow measurements for each gauge. The prototype uses USGS streamgauges
   as stand-ins for logger data, via [`dataretrieval`](https://github.com/DOI-USGS/dataretrieval-python).
2. fits a rating curve for each gauge with [`ratingcurve`](https://github.com/thodson-usgs/ratingcurve)
   (a Bayesian segmented power law),
3. converts water levels to streamflow with an uncertainty range, with zero flow when the stream is dry,
4. publishes the site to GitHub Pages.

**Home page:** a map and list of gauges, a "Visit a staff gauge" section with a link for submitting
readings, and a short "how it works" section.

**Gauge pages:** gauge info (including no-flow days), a staff-gauge box, a map, streamflow and
water-level charts with time-range buttons, the rating curve and its parameters, and CSV/JSON downloads.

> This is a concept demo, not an official Massachusetts Rivers Alliance product. The footer says so.

## Setup

1. Push these files to the `main` branch of a GitHub repository, including the hidden `.github` folder.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.** Don't pick one of
   GitHub's suggested workflows. The workflow is already in `.github/workflows/build-ratings.yml`.
3. Open the **Actions** tab. The push starts a run; if not, click **Run workflow**.
   A run takes about 6–10 minutes.
4. The site is at `https://<user>.github.io/<repo>/`.

The site rebuilds on every push, for example after new logger data are added. To rebuild by hand,
click **Actions → Fit rating curves and publish site → Run workflow**.

**Staff-gauge readings:** set `submit_url` at the top of `sites.yml` to a form (a Google Form works)
so the "Submit a reading" buttons go somewhere. Until then they're grayed out. Each gauge can also have
a `staff_gauge:` line describing where to find the gauge.

**Optional — USGS API key:** without a key, USGS limits how many requests you can make per hour.
That's plenty for a handful of gauges. For more, get a free key at https://api.waterdata.usgs.gov/signup/
and add it as a repository secret named `API_USGS_PAT` (**Settings → Secrets and variables → Actions**).

## Adding or changing gauges

Edit `sites.yml`:

```yaml
sites:
  - usgs_id: "01104500"          # USGS site number (keep the quotes and leading zero)
    name: Charles River at Waltham
    river: Charles River
    region: Greater Boston
    segments: 2                  # optional; overrides the default
    measurements_since: "2018-10-01"   # optional
```

Find site numbers on https://waterdata.usgs.gov/. Pick gauges that report gage height and have field
measurements. Most continuous streamgauges do.

- `segments`: the number of power-law segments (control changes). Start with 2. Add one if the fitted
  curve misses a bend in the measurements; use 1 if the second breakpoint's interval is very wide.
- `measurements_since`: rating curves shift as channels change. Using only the last ~10 years of
  measurements keeps the fit close to the current channel. Shorten it if the fit disagrees with
  recent measurements.

The site title, tagline, intro, disclaimer, and units are at the top of `sites.yml`.

If a gage fails (no measurements, a USGS outage), the rest of the site still publishes. The gage's card
appears in the list with the error, and the Actions run shows a warning.

## Running locally

```bash
pip install -r requirements.txt
python scripts/fetch_usgs.py        # downloads into data/usgs/
python scripts/build_site.py        # writes _site/
python -m http.server -d _site 8000 # open http://localhost:8000
```

## How the numbers are made

- **Rating curve:** `ln(Q) = a + Σ bᵢ · ln(max(h − hsᵢ, 0) + hoᵢ)`, fitted with ADVI and a fixed random seed.
  Measurement uncertainty comes from the measurement's quality rating when one is available, otherwise 5%.
- **Dry streams:** flow is 0 whenever the water level is at or below hs0, the fitted zero-flow stage.
  Dry site visits (flow = 0) are counted, but a power law can't be fitted to them, so they aren't
  used in the fit.
- **Streamflow:** the daily water level run through the rating. The ~95% range is median ÷ GSE² to
  median × GSE².

## Files

```
sites.yml                       gauges and site text
scripts/fetch_usgs.py           USGS download
scripts/build_site.py           fitting + page generation
site_assets/site.css, site.js   styles, maps, charts
site_assets/vendor/             Leaflet, Chart.js (bundled; their licenses are alongside)
.github/workflows/              GitHub Actions workflow
```

## Credits

Hodson, T.O., Doore, K.J., Kenney, T.A., Over, T.M., and Yeheyis, M.B., 2024, Ratingcurve: A Python
package for fitting streamflow rating curves: *Hydrology*, v. 11, no. 2, p. 14,
https://doi.org/10.3390/hydrology11020014. Data: U.S. Geological Survey. Basemap © OpenStreetMap
contributors, © CARTO.
