# vending-finder

Data pipeline for identifying the best residential building lobbies in Dubai
to place a vending machine, based on open geospatial data (OpenStreetMap).

## Motivation

Placing a vending machine in a residential building lobby is a bet on foot
traffic: how many residents live there, and how inconvenient it is for them
to just walk to the nearest supermarket instead. This project builds a
reproducible pipeline to estimate that bet across every residential
building in Dubai, using only free, open data sources.

## Status

🚧 Work in progress. Currently implemented: building data collection and
validation. Distance-to-supermarket features and the final scoring model
are next.

## Pipeline

1. **Collect** — query the [Overpass API](https://overpass-api.de) for all
   `building=apartments|residential` ways across the Dubai emirate,
   grid-partitioned to stay within public server timeouts.
2. **Validate** — OSM tags for building size (`building:flats`,
   `building:levels`, `height`) are inconsistent and sometimes outright
   wrong. This step cross-validates them against each other and flags each
   building with a confidence level.
3. **Feature engineering** *(planned)* — distance to nearest supermarket,
   competitor density, transit proximity, mixed-use bonus.
4. **Scoring** *(planned)* — a weighted score combining estimated resident
   count and accessibility factors.
5. **Dashboard** *(planned)* — interactive map (Streamlit) to explore
   ranked buildings.

## A real data quality problem found along the way

While manually inspecting Dubai Marina through Overpass Turbo, several
buildings turned out to have internally inconsistent tags — e.g. **The
Torch** is tagged `building:levels=4` and `height=15` (both wildly wrong;
the actual tower has ~87 floors), while `building:flats=682` is roughly
correct. Trusting `building:levels` at face value would have produced
absurd building-density estimates for otherwise perfectly good buildings.

`validate_buildings.py` catches these by cross-checking `flats`, `levels`,
and `height` against each other, using ranges calibrated on manually
verified Marina towers (see the script's docstring for the full
methodology and the exact thresholds used).

## Usage

```bash
pip install -r requirements.txt

# 1. Collect all buildings in Dubai (grid-based Overpass queries, resumable)
python src/collect/fetch_buildings.py

# quick test on a small area (Marina) instead of the whole emirate:
python src/collect/fetch_buildings.py --bbox 25.075 55.130 25.095 55.150 --grid-size 0.02

# 2. Validate and clean
python src/features/validate_buildings.py
```

See `data/sample/` for a small example dataset (real Dubai Marina buildings)
and its validated output, so you can see what the pipeline produces without
running a full collection first.

## Known limitations

- **Tag coverage is much worse than Dubai Marina suggested.** A full-city
  run (11,395 buildings) found only 27 buildings (0.2%) with consistent
  `flats`+`levels` tags, and 6,649 (58%) with neither tag at all. Marina's
  ~13% `flats` coverage was not representative — it's a showcase district
  that got careful manual tagging (most towers even have a `wikidata`
  link), unlike the rest of the city.
- **The footprint-area calibration is itself skewed toward luxury
  high-rises.** The only buildings with a trustworthy `flats` count to
  calibrate against are, again, mostly Marina/Downtown towers — the
  resulting "sqm of floor area per apartment" coefficient (~225 sqm,
  observed range 146-971) reflects large luxury units. Applying it
  city-wide likely **undercounts** apartments in non-luxury districts
  (Deira, Al Nahda, International City, etc.), where real units are
  considerably smaller. Stratifying calibration by building class or
  district before trusting `medium`-confidence estimates in those areas is
  a planned improvement — for now, treat `medium` estimates outside
  Marina/Downtown/JBR as a floor, not a point estimate.
- **58% of buildings have no size signal at all** (no `building:levels`,
  so footprint area alone can't be turned into a floor count) and are
  excluded from any estimate. This is a hard ceiling for a
  tags-plus-geometry-only approach; closing this gap would need either a
  district-level default level count or a different data source entirely.
- A full collection run against the public Overpass API can take anywhere
  from ~30 minutes to a few hours depending on server load and the
  `--sleep` setting — this is why the project switched to offline
  extraction from a local Geofabrik `.osm.pbf` file (see
  `extract_from_pbf.py`), which has no rate limits and finishes in
  seconds.
- This is a discovery/exploration tool built on crowd-sourced OSM data, not
  a substitute for ground-truth foot traffic measurement or negotiation
  with building management.

## License

MIT — see `LICENSE`. Building data is from
[OpenStreetMap](https://www.openstreetmap.org), © OpenStreetMap
contributors, available under the [ODbL](https://opendatacommons.org/licenses/odbl/).
