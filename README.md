# Streamflow Rating Curves on GitHub Pages

This repo fits stage–discharge rating curves with the USGS
[`ratingcurve`](https://github.com/thodson-usgs/ratingcurve) package (a segmented power law fit
with PyMC) and publishes the results as a website. Each time you push new measurements, a GitHub
Action refits every site and redeploys the site.

Each gage gets its own page with:
- the fitted rating curve, its uncertainty band, and the measurements
- a residuals plot for checking the fit
- fitted parameters in real units (intercept, exponents, breakpoints) with 95% intervals
- a rating table (median, mean, ≈95% range), with downloads as CSV, JSON, and the full posterior (NetCDF)

## One-time setup

1. Create a new GitHub repository (for example `rating-curves`) and push these files to `main`:
   ```bash
   git init && git add . && git commit -m "Rating curve site"
   git branch -M main
   git remote add origin https://github.com/<you>/rating-curves.git
   git push -u origin main
   ```
2. On GitHub, go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
3. Open the **Actions** tab. If the first run started before you changed the Pages setting, click
   **Fit rating curves and publish site → Run workflow**. A run takes about 3–5 minutes (mostly
   installing PyMC) plus about 30 seconds per site.
4. Your site will be at `https://<you>.github.io/rating-curves/`.

## Adding your own gage

1. Put a CSV in `data/` with these columns (the header names matter, the order doesn't):

   | column     | required | meaning                                        |
   |------------|----------|------------------------------------------------|
   | `stage`    | yes      | gage height                                    |
   | `q`        | yes      | measured discharge                             |
   | `q_sigma`  | no       | 1-σ uncertainty of each measurement (same units as `q`) |
   | `datetime` | no       | shown in the measurement table only            |

2. Add an entry to `sites.yml`:
   ```yaml
   - id: my-creek            # used in the URL
     name: My Creek near Town
     file: data/my_creek.csv
     segments: 2             # number of power-law segments (control changes)
   ```
3. Commit and push. The Action refits and redeploys.

You can delete the two demo sites (Green River and Provo River, from the package's test data)
whenever you like. Stage and discharge units are set once at the top of `sites.yml`.

**Choosing `segments`:** start with 1 or 2. If the residuals plot bends across a stage range, add a
segment. If a breakpoint's 95% interval is very wide or sits where you have no data, use fewer.
Five or more measurements are needed. A good fit usually needs measurements across the whole stage range.

## Running locally

```bash
pip install -r requirements.txt
python scripts/build_site.py              # all sites -> _site/
python scripts/build_site.py --only my-creek
python -m http.server -d _site 8000       # open http://localhost:8000
```

If one site fails (bad CSV, too few points), the rest still publish. The failed site appears on the
home page with its error, and the Action shows a warning.

## Notes

- Fits use ADVI (the package default) with a fixed random seed (`--seed`), so reruns on the same
  data give the same results.
- Rating tables extend 10% above the highest measurement. Treat that extrapolation with caution.
- Results are provisional and should be reviewed by a hydrographer before operational use.

## Citation

Hodson, T.O., Doore, K.J., Kenney, T.A., Over, T.M., and Yeheyis, M.B., 2024, Ratingcurve: A Python
package for fitting streamflow rating curves: *Hydrology*, v. 11, no. 2, p. 14,
https://doi.org/10.3390/hydrology11020014
