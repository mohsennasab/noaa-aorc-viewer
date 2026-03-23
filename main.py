import asyncio
from typing import Literal

import numpy as np
import pandas as pd
import requests as http_requests
import s3fs
import shapely
import xarray as xr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from rasterio.io import MemoryFile
from rasterio.mask import mask as rio_mask
from shapely.geometry import mapping, shape

# ---------------------------------------------------------------------------
# AORC dataset config
# ---------------------------------------------------------------------------
BUCKET = "noaa-nws-aorc-v1-1-1km"
MAX_DAYS = 180
VARIABLE_META = {
    "APCP_surface":        {"label": "Precipitation",              "units": "mm/hr"},
    "TMP_2maboveground":   {"label": "Temperature",                "units": "K"},
    "SPFH_2maboveground":  {"label": "Specific Humidity",          "units": "g/g"},
    "DLWRF_surface":       {"label": "Downw. Longwave Radiation",  "units": "W/m²"},
    "DSWRF_surface":       {"label": "Downw. Shortwave Radiation", "units": "W/m²"},
    "PRES_surface":        {"label": "Surface Pressure",           "units": "Pa"},
    "UGRD_10maboveground": {"label": "U-Wind (10 m)",              "units": "m/s"},
    "VGRD_10maboveground": {"label": "V-Wind (10 m)",              "units": "m/s"},
}

# ---------------------------------------------------------------------------
# NLCD class definitions  (code → name + NLCD standard hex color)
# ---------------------------------------------------------------------------
NLCD_CLASSES = {
    11: {"name": "Open Water",                      "color": "#476BA1"},
    12: {"name": "Perennial Ice/Snow",              "color": "#D1DEF8"},
    21: {"name": "Developed, Open Space",           "color": "#DDC9C9"},
    22: {"name": "Developed, Low Intensity",        "color": "#D89382"},
    23: {"name": "Developed, Medium Intensity",     "color": "#ED0000"},
    24: {"name": "Developed, High Intensity",       "color": "#AA0000"},
    31: {"name": "Barren Land",                     "color": "#B2ADA3"},
    41: {"name": "Deciduous Forest",                "color": "#68AB5F"},
    42: {"name": "Evergreen Forest",                "color": "#1C5F2C"},
    43: {"name": "Mixed Forest",                    "color": "#B5C58F"},
    51: {"name": "Dwarf Scrub",                     "color": "#CCBA7C"},
    52: {"name": "Shrub/Scrub",                     "color": "#CCBA7C"},
    71: {"name": "Grassland/Herbaceous",            "color": "#E2E2C1"},
    72: {"name": "Sedge/Herbaceous",                "color": "#C9C977"},
    73: {"name": "Lichens",                         "color": "#99C147"},
    74: {"name": "Moss",                            "color": "#77AD1C"},
    81: {"name": "Pasture/Hay",                     "color": "#DBD83D"},
    82: {"name": "Cultivated Crops",                "color": "#AA7028"},
    90: {"name": "Woody Wetlands",                  "color": "#BAD9EB"},
    95: {"name": "Emergent Herbaceous Wetlands",    "color": "#70A3BA"},
}

NLCD_WCS_BASE = "https://www.mrlc.gov/geoserver/NLCD_Land_Cover/wcs"
NLCD_NATIVE_RES_DEG = 30.0 / 111_320   # ~0.000270 degrees per pixel at 30 m
NLCD_MAX_PIXELS = 2000                  # cap per axis to keep memory reasonable

app = FastAPI(title="NOAA AORC Viewer")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Shared AOI model (used by both endpoints)
# ---------------------------------------------------------------------------
class AOIRequest(BaseModel):
    min_lon: float | None = None
    min_lat: float | None = None
    max_lon: float | None = None
    max_lat: float | None = None
    geometry: dict | None = None   # GeoJSON Polygon / MultiPolygon

    @model_validator(mode="after")
    def check_aoi_provided(self):
        has_bbox = all(
            v is not None for v in [self.min_lon, self.min_lat, self.max_lon, self.max_lat]
        )
        if not has_bbox and self.geometry is None:
            raise ValueError(
                "Provide either bbox (min_lon/min_lat/max_lon/max_lat) or a GeoJSON geometry."
            )
        return self

    def resolve_bounds(self):
        """Return (polygon_or_None, min_lon, min_lat, max_lon, max_lat)."""
        if self.geometry:
            polygon = shape(self.geometry)
            return polygon, *polygon.bounds
        return None, self.min_lon, self.min_lat, self.max_lon, self.max_lat


# ---------------------------------------------------------------------------
# AORC query request
# ---------------------------------------------------------------------------
class QueryRequest(AOIRequest):
    variable: Literal[
        "APCP_surface",
        "TMP_2maboveground",
        "SPFH_2maboveground",
        "DLWRF_surface",
        "DSWRF_surface",
        "PRES_surface",
        "UGRD_10maboveground",
        "VGRD_10maboveground",
    ]
    start_date: str
    end_date: str


# ---------------------------------------------------------------------------
# Land cover request
# ---------------------------------------------------------------------------
class LandCoverRequest(AOIRequest):
    year: Literal[2001, 2004, 2006, 2008, 2011, 2013, 2016, 2019, 2021]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def open_year(year: int, fs: s3fs.S3FileSystem) -> xr.Dataset:
    store = s3fs.S3Map(f"{BUCKET}/{year}.zarr", s3=fs)
    return xr.open_zarr(store, consolidated=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/api/query")
async def query(req: QueryRequest):
    try:
        t0 = pd.Timestamp(req.start_date)
        t1 = pd.Timestamp(req.end_date)
    except Exception:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")

    if t1 <= t0:
        raise HTTPException(400, "end_date must be after start_date.")
    if (t1 - t0).days > MAX_DAYS:
        raise HTTPException(400, f"Date range cannot exceed {MAX_DAYS} days.")

    years = list(range(t0.year, t1.year + 1))

    try:
        polygon, min_lon, min_lat, max_lon, max_lat = req.resolve_bounds()
    except Exception as e:
        raise HTTPException(400, f"Invalid geometry: {e}")

    fs = s3fs.S3FileSystem(anon=True)
    loop = asyncio.get_event_loop()

    def _load():
        datasets = []
        for year in years:
            try:
                ds = open_year(year, fs)
            except Exception as e:
                raise HTTPException(500, f"Failed to open {year} data: {e}")

            var = ds[req.variable]

            year_t0 = max(t0, pd.Timestamp(f"{year}-01-01"))
            year_t1 = min(t1, pd.Timestamp(f"{year}-12-31 23:59"))
            var = var.sel(time=slice(year_t0, year_t1))
            if var.sizes.get("time", 0) == 0:
                continue

            var = var.sel(
                latitude=slice(min_lat, max_lat),
                longitude=slice(min_lon, max_lon),
            )
            if var.sizes.get("latitude", 0) == 0 or var.sizes.get("longitude", 0) == 0:
                continue

            if polygon is not None:
                lats = var.latitude.values
                lons = var.longitude.values
                lons_2d, lats_2d = np.meshgrid(lons, lats)
                mask = shapely.contains_xy(
                    polygon, lons_2d.ravel(), lats_2d.ravel()
                ).reshape(lats_2d.shape)
                if not mask.any():
                    continue
                xr_mask = xr.DataArray(
                    mask,
                    dims=["latitude", "longitude"],
                    coords={"latitude": lats, "longitude": lons},
                )
                var = var.where(xr_mask)

            datasets.append(var.mean(dim=["latitude", "longitude"]).compute())

        if not datasets:
            raise HTTPException(404, "No data found for the given AOI and date range.")
        return xr.concat(datasets, dim="time")

    try:
        result = await loop.run_in_executor(None, _load)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Data processing error: {e}")

    times = [str(t) for t in pd.DatetimeIndex(result.time.values)]
    values = [None if np.isnan(v) else v for v in result.values.tolist()]
    meta = VARIABLE_META[req.variable]
    return JSONResponse({
        "times": times,
        "values": values,
        "variable": req.variable,
        "label": meta["label"],
        "units": meta["units"],
        "n_times": len(times),
    })


@app.post("/api/landcover")
async def landcover(req: LandCoverRequest):
    try:
        polygon, min_lon, min_lat, max_lon, max_lat = req.resolve_bounds()
    except Exception as e:
        raise HTTPException(400, f"Invalid geometry: {e}")

    loop = asyncio.get_event_loop()

    def _fetch_and_analyze():
        # Calculate request dimensions at native 30 m, capped
        lon_span = max_lon - min_lon
        lat_span = max_lat - min_lat
        width = max(64, min(NLCD_MAX_PIXELS, int(lon_span / NLCD_NATIVE_RES_DEG)))
        height = max(64, min(NLCD_MAX_PIXELS, int(lat_span / NLCD_NATIVE_RES_DEG)))

        layer = f"NLCD_{req.year}_Land_Cover_L48"
        url = (
            f"{NLCD_WCS_BASE}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage"
            f"&COVERAGE={layer}"
            f"&BBOX={min_lon},{min_lat},{max_lon},{max_lat}"
            f"&CRS=EPSG:4326&WIDTH={width}&HEIGHT={height}&FORMAT=GeoTIFF"
        )

        resp = http_requests.get(url, timeout=90)
        if resp.status_code != 200:
            raise HTTPException(502, f"NLCD WCS returned HTTP {resp.status_code}")

        with MemoryFile(resp.content) as memfile:
            with memfile.open() as ds:
                if polygon is not None:
                    # rasterio.mask expects geometries in raster CRS (EPSG:4326 here)
                    out_image, _ = rio_mask(
                        ds, [mapping(polygon)], crop=False, nodata=0, filled=True
                    )
                    arr = out_image[0]
                else:
                    arr = ds.read(1)

        valid = arr[arr > 0]
        if valid.size == 0:
            raise HTTPException(404, "No NLCD data found inside the selected AOI.")

        vals, counts = np.unique(valid, return_counts=True)
        total = int(counts.sum())
        classes = {}
        for v, c in zip(vals.tolist(), counts.tolist()):
            code = int(v)
            meta = NLCD_CLASSES.get(code, {"name": f"Class {code}", "color": "#888888"})
            classes[code] = {
                "name": meta["name"],
                "color": meta["color"],
                "count": c,
                "percent": round(c / total * 100, 2),
            }
        return {"year": req.year, "total_pixels": total, "classes": classes}

    try:
        result = await loop.run_in_executor(None, _fetch_and_analyze)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Land cover analysis error: {e}")

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# USGS NWIS streamflow gauge models
# ---------------------------------------------------------------------------
NWIS_IV_BASE = "https://waterservices.usgs.gov/nwis/iv/"
NWIS_PARAM = "00060"  # discharge, ft³/s
NWIS_MISSING = -999999


class GaugeListRequest(BaseModel):
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    limit: int = 50


class GaugeTimeseriesRequest(BaseModel):
    site_no: str
    start_date: str
    end_date: str


# ---------------------------------------------------------------------------
# Routes — gauges
# ---------------------------------------------------------------------------
@app.post("/api/gauges")
async def gauge_list(req: GaugeListRequest):
    loop = asyncio.get_event_loop()

    def _fetch():
        bbox = f"{round(req.min_lon,4)},{round(req.min_lat,4)},{round(req.max_lon,4)},{round(req.max_lat,4)}"
        params = {
            "format": "json",
            "parameterCd": NWIS_PARAM,
            "bBox": bbox,
            "siteStatus": "active",
        }
        resp = http_requests.get(NWIS_IV_BASE, params=params, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(502, f"NWIS returned HTTP {resp.status_code}")
        ts_list = resp.json().get("value", {}).get("timeSeries", [])
        gauges = []
        for ts in ts_list[: req.limit]:
            si = ts["sourceInfo"]
            site_no = si.get("siteCode", [{}])[0].get("value", "")
            name = si.get("siteName", "")
            geo = si.get("geoLocation", {}).get("geogLocation", {})
            lat = geo.get("latitude")
            lon = geo.get("longitude")
            values_list = ts.get("values", [{}])[0].get("value", [])
            latest = values_list[-1] if values_list else {}
            raw = float(latest.get("value", NWIS_MISSING)) if latest else NWIS_MISSING
            flow = None if raw == NWIS_MISSING else raw
            obs_time = latest.get("dateTime") if latest else None
            gauges.append({"site_no": site_no, "name": name, "lat": lat, "lon": lon,
                            "flow_cfs": flow, "obs_time": obs_time})
        return gauges

    try:
        result = await loop.run_in_executor(None, _fetch)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gauge list error: {e}")

    return JSONResponse(result)


@app.post("/api/gauge_timeseries")
async def gauge_timeseries(req: GaugeTimeseriesRequest):
    try:
        t0 = pd.Timestamp(req.start_date)
        t1 = pd.Timestamp(req.end_date)
    except Exception:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if (t1 - t0).days > MAX_DAYS:
        raise HTTPException(400, f"Date range cannot exceed {MAX_DAYS} days.")

    loop = asyncio.get_event_loop()

    def _fetch():
        params = {
            "format": "json",
            "sites": req.site_no,
            "parameterCd": NWIS_PARAM,
            "startDT": req.start_date,
            "endDT": req.end_date,
        }
        resp = http_requests.get(NWIS_IV_BASE, params=params, timeout=60)
        if resp.status_code != 200:
            raise HTTPException(502, f"NWIS returned HTTP {resp.status_code}")
        ts_list = resp.json().get("value", {}).get("timeSeries", [])
        if not ts_list:
            raise HTTPException(404, "No streamflow data found for this site and date range.")
        ts = ts_list[0]
        site_name = ts["sourceInfo"].get("siteName", req.site_no)
        records = ts.get("values", [{}])[0].get("value", [])
        times, values = [], []
        for r in records:
            raw = float(r["value"])
            times.append(r["dateTime"])
            values.append(None if raw == NWIS_MISSING else raw)
        return {
            "site_no": req.site_no,
            "name": site_name,
            "times": times,
            "values": values,
            "units": "ft³/s",
            "n_times": len(times),
        }

    try:
        result = await loop.run_in_executor(None, _fetch)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gauge timeseries error: {e}")

    return JSONResponse(result)
