# Future Feature Ideas

Ideas for future additions to the Hydrological Analysis and Watershed Kit.
Not prioritized — add, remove, or reorder as the project evolves.

---

## 1. Terrain / Hydrologically Conditioned DEM

**Dataset:** WWF HydroSHEDS — Hydrologically Conditioned DEM (3 arc-second, ~90 m)
**Source:** https://developers.google.com/earth-engine/datasets/catalog/WWF_HydroSHEDS_03CONDEM

**What it is:**
A globally consistent digital elevation model that has been hydrologically conditioned —
sinks filled, flow directions enforced — so that derived products like flow accumulation and
stream networks are topologically correct. 3 arc-second (~90 m) resolution.

**Ideas for the app:**
- Display a hillshade or elevation raster overlay on the map for the selected AOI.
- Extract slope and aspect statistics for the watershed.
- Derive a simple flow accumulation map to visually confirm watershed drainage direction.
- Show elevation profile (min / mean / max / std) in a summary panel.
- Could underpin a future curve number or runoff estimation feature.

**Notes:**
- Available via Google Earth Engine; may also be accessible via the HydroSHEDS direct download
  (hydrosheds.org) or cloud-optimized GeoTIFF on AWS/Azure.
- CRS: WGS 84 geographic. Reproject to EPSG:3857 for area calculations.

---

## 2. Population Count / Density

**Dataset:** CIESIN Gridded Population of the World v4.11 (GPWv4) — Population Count
**Source:** https://developers.google.com/earth-engine/datasets/catalog/CIESIN_GPWv411_GPW_Population_Count

**What it is:**
Estimates of human population (number of persons) per grid cell at approximately
30 arc-second (~1 km) resolution, for reference years 2000, 2005, 2010, 2015, and 2020.
Produced by CIESIN at Columbia University.

**Ideas for the app:**
- Report total population within the selected AOI or watershed.
- Show population density (persons/km²) as a map overlay colored by density class.
- Time-series comparison: how did population in the watershed change from 2000 to 2020?
- Combine with Buildings Analysis: cross-check building count against population estimate
  as a data quality sanity check.
- Useful for flood risk context — how many people live in a flood-prone watershed?

**Notes:**
- Data is available as cloud-optimized GeoTIFF from SEDAC (NASA Earthdata).
- Requires authentication for SEDAC; check if a public mirror exists before implementation.
- For a simpler alternative, the US Census TIGER/ACS data (via Census API) may be
  sufficient for CONUS-only use cases.

---

## 3. Lake / Reservoir Bathymetry

**Dataset:** GLOBathy — Global Lakes Bathymetry Dataset
**Source:** https://developers.google.com/earth-engine/datasets/catalog/projects_sat-io_open-datasets_GLOBathy_GLOBathy_bathymetry

**What it is:**
Global bathymetric maps (water depth) for 1.4 million lakes and reservoirs, derived by
combining HydroLAKES polygons with modeled depth estimates. Raster format, ~90 m resolution.

**Ideas for the app:**
- When the AOI or watershed contains a lake or reservoir, automatically detect it and
  display the bathymetric depth map as a map overlay.
- Report max depth, mean depth, and estimated water volume in a summary card.
- Useful for reservoir operations context alongside the streamflow gauge data.
- Could support a simple water storage estimation feature.

**Notes:**
- Companion dataset: HydroLAKES (polygon boundaries) — use to detect lake presence in AOI
  before attempting to load bathymetry.
- Available as cloud-optimized GeoTIFF on Google Cloud Storage (sat-io open datasets).

---

## 4. Historical Flood Events

**Dataset:** Global Flood Database — MODIS-derived Flood Events v1
**Source:** https://developers.google.com/earth-engine/datasets/catalog/GLOBAL_FLOOD_DB_MODIS_EVENTS_V1

**What it is:**
A database of 913 large flood events (2000–2018) mapped using MODIS 250 m imagery.
Each event includes the flood extent polygon, duration, and affected area. Produced by
the Dartmouth Flood Observatory / Cloud to Street.

**Ideas for the app:**
- Overlay historical flood extents on the map for events that intersect the selected AOI.
- Show a timeline of flood events in a chart (count per year, duration distribution).
- For each event: flooded area (km²), duration (days), date range.
- Cross-reference with AORC precipitation data — load AORC for the same dates as a
  major flood event to show the causative rainfall.
- Export clipped flood extents as a shapefile (same pattern as Buildings export).

**Notes:**
- Access via Google Earth Engine API (requires GEE account/authentication) or via the
  pre-exported GeoTIFF tiles available through Cloud to Street / DFO.
- 913 events is manageable — could download all event metadata as a GeoJSON once and
  query spatially in the browser without a backend call.

---

## General Notes for Implementation

- All four datasets are global but the app currently targets CONUS. Clip to CONUS bounds
  before any heavy data loading.
- Each new dataset should follow the established sidebar pattern:
  numbered section → button → spinner → status → summary stats → export button.
- Add a `<div class="ref-card">` entry in the Data Sources modal for each new dataset.
- GEE-backed datasets will require either a server-side GEE Python client
  (`earthengine-api`) or a pre-exported cloud-optimized GeoTIFF alternative — prefer
  the latter to avoid GEE authentication friction for end users.
