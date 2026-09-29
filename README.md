# Deccansoft Home · Live ads dashboard

Live at https://lenkasacademy-pixel.github.io/deccansoft-live/

- `fetch.py` pulls ad account 1864644430272738 from the Meta Marketing API and writes `data.json`.
- `.github/workflows/refresh.yml` runs it every 5 minutes (GitHub's schedule is best-effort, so
  gaps of 5–15 minutes are normal) and force-pushes `data.json` alone to the `data` branch.
- `index.html` (served by Pages from `main`) re-reads that file from raw.githubusercontent.com
  every minute. The dot next to "updated" goes amber after 20 minutes without new data, red after 60.

The Meta token is the `META_TOKEN` repository secret. It never appears in the page or in `data.json`.
To rotate it: Settings → Secrets and variables → Actions → META_TOKEN → Update, then
Actions → Refresh ad data → Run workflow.

Reporting starts `START_DAY` in `fetch.py` (22 Sep 2026); earlier days are deliberately left out.
Run locally: `META_TOKEN=... python3 fetch.py && python3 -m http.server`.
