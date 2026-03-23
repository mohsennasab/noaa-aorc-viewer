import asyncio
from typing import Literal

import numpy as np
import pandas as pd
import s3fs
import shapely
import xarray as xr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from shapely.geometry import shape

BUCKET = "noaa-nws-aorc-v1-1-1km"
MAX_DAYS = 180
VARIABLE_META = {
    "APCP_surface": {"label": "Precipitation", "units": "mm/hr"},
    "TMP_2maboveground": {"label": "Temperature", "units": "K"},
}

app = FastAPI(title="NOAA AORC Viewer")
app.mount("/static", StaticFiles(directory="static"), name="static")


class QueryRequest(BaseModel):
    # Bbox mode — all four required together
    min_lon: float | None = None
    min_lat: float | None = None
    max_lon: float | None = None
    max_lat: float | None = None
    # Watershed mode — GeoJSON geometry object (Polygon or MultiPolygon)
    geometry: dict | None = None
    # Shared
    variable: Literal["APCP_surface", "TMP_2maboveground"]
    start_date: str
    end_date: str

    @model_validator(mode="after")
    def check_aoi_provided(self):
        has_bbox = all(
            v is not None for v in [self.min_lon, self.min_lat, self.max_lon, self.max_lat]
        )
        has_geom = self.geometry is not None
        if not has_bbox and not has_geom:
            raise ValueError(
                "Provide either a bbox (min_lon/min_lat/max_lon/max_lat) or a GeoJSON geometry."
            )
        return self


def open_year(year: int, fs: s3fs.S3FileSystem) -> xr.Dataset:
    store = s3fs.S3Map(f"{BUCKET}/{year}.zarr", s3=fs)
    return xr.open_zarr(store, consolidated=True)


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/api/query")
async def query(req: QueryRequest):
    # Parse dates — tz-naive to match xarray's datetime64[ns]
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

    # Resolve AOI bounds and optional polygon
    if req.geometry:
        try:
            polygon = shape(req.geometry)
        except Exception as e:
            raise HTTPException(400, f"Invalid GeoJSON geometry: {e}")
        min_lon, min_lat, max_lon, max_lat = polygon.bounds
    else:
        polygon = None
        min_lon, min_lat = req.min_lon, req.min_lat
        max_lon, max_lat = req.max_lon, req.max_lat

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

            # Subset time
            year_t0 = max(t0, pd.Timestamp(f"{year}-01-01"))
            year_t1 = min(t1, pd.Timestamp(f"{year}-12-31 23:59"))
            var = var.sel(time=slice(year_t0, year_t1))

            if var.sizes.get("time", 0) == 0:
                continue

            # Subset to bounding box first (cheap)
            var = var.sel(
                latitude=slice(min_lat, max_lat),
                longitude=slice(min_lon, max_lon),
            )

            if var.sizes.get("latitude", 0) == 0 or var.sizes.get("longitude", 0) == 0:
                continue

            # Apply watershed polygon mask if provided
            if polygon is not None:
                lats = var.latitude.values
                lons = var.longitude.values
                lons_2d, lats_2d = np.meshgrid(lons, lats)
                # shapely.contains_xy(geom, x=lon, y=lat)
                mask = shapely.contains_xy(
                    polygon, lons_2d.ravel(), lats_2d.ravel()
                ).reshape(lats_2d.shape)

                if not mask.any():
                    continue  # no grid cells inside polygon for this year slice

                xr_mask = xr.DataArray(
                    mask,
                    dims=["latitude", "longitude"],
                    coords={"latitude": lats, "longitude": lons},
                )
                var = var.where(xr_mask)

            # Spatial mean (skipna=True by default → ignores masked NaNs)
            spatial_mean = var.mean(dim=["latitude", "longitude"]).compute()
            datasets.append(spatial_mean)

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
