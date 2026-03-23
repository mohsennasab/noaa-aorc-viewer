# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
cd "D:/Claude Code Test/aorc_viewer"
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

The app is then available at http://localhost:8000.

On Windows, background processes started with `&` in Git Bash are invisible to `netstat` from cmd/PowerShell. To find and kill a stale server use PowerShell:
```powershell
Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess
Stop-Process -Id <PID> -Force
```

## Installing dependencies

```bash
pip install -r requirements.txt
```

## Architecture

The project is two files: `main.py` (FastAPI backend) and `static/index.html` (single-page frontend). There is no build step.

### Data flow

1. User draws a rectangle **or** clicks to select a USGS watershed on the map.
2. Frontend POSTs to `/api/query` with either a bbox (`min_lon/min_lat/max_lon/max_lat`) or a GeoJSON `geometry` object.
3. Backend opens one consolidated Zarr store per calendar year from the public S3 bucket `noaa-nws-aorc-v1-1-1km` (anonymous access, us-east-1). Each store lives at `s3://noaa-nws-aorc-v1-1-1km/{year}.zarr` and contains 8 variables × 8 784 hourly time steps × 4 201 latitudes × 8 401 longitudes.
4. For each year, the backend subsets by time then by bounding box (cheap label-based Zarr reads), optionally applies a Shapely `contains_xy` pixel mask for watershed queries, then calls `.compute()` for the spatial mean.
5. Results are returned as `{times, values, label, units}` JSON and rendered by Plotly.

### Key backend details (`main.py`)

- `open_year()` — opens a single year's Zarr store with `consolidated=True` (`.zmetadata` exists on S3).
- `_load()` — synchronous inner function run in a thread executor to avoid blocking the event loop during Zarr/S3 I/O.
- Timestamps must be **tz-naive** (`pd.Timestamp(...)` without `tz=`) to match xarray's `datetime64[ns]` coordinate.
- Watershed polygon mask: bbox subset first → `shapely.contains_xy(polygon, lons_2d, lats_2d)` → `xr.DataArray.where()` → `mean(skipna=True)`. Requires Shapely ≥ 2.0.
- `MAX_DAYS = 180` caps query size.

### Key frontend details (`static/index.html`)

- Two mutually exclusive AOI modes toggled by sidebar tabs: **Draw AOI** (Leaflet.Draw rectangle) and **Select Watershed** (map click).
- Watershed queries hit the USGS WBD ArcGIS REST service directly from the browser:
  `https://hydrowfs.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layerId}/query`
  Layer IDs 2–6 correspond to HUC-4 through HUC-12. The full GeoJSON geometry returned is passed straight to the backend.
- Switching modes clears the other mode's selection and toggles draw-control visibility.
- All state is in plain JS variables: `currentMode`, `currentBbox`, `watershedGeometry`, `watershedLayer`.

## Git & GitHub workflow

Every meaningful change must be committed and pushed:
```bash
git add <files>
git commit -m "short imperative subject"
git push
```

Remote: `https://github.com/mohsennasab/noaa-aorc-viewer`

## Adding a new AORC variable

1. Add an entry to `VARIABLE_META` in `main.py`.
2. Add an `<option>` to the `#variable` select in `index.html`.
3. Add a colour branch in `renderChart()` if the default blue/orange isn't appropriate.
