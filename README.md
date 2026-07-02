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

Full pipeline implemented end-to-end for the Dubai emirate: collection,
validation, distance features, scoring, Google-verified spot-checking, and
an interactive map. See Known limitations for what to trust vs. treat as
approximate.

## Pipeline

1. **Collect** — extract all `building=apartments|residential` ways for
   Dubai from a local Geofabrik `.osm.pbf` extract (`extract_from_pbf.py`,
   offline, no rate limits). A grid-based live Overpass API collector
   (`fetch_buildings.py`) also exists as an alternative/fallback path, but
   proved too slow and rate-limit-prone for a full-city run — see Known
   limitations.
2. **Validate** — OSM tags for building size (`building:flats`,
   `building:levels`, `height`) are inconsistent and sometimes outright
   wrong. `validate_buildings.py` cross-validates them against each other
   and against footprint area (computed from the building polygon), and
   flags each building with a confidence level (high/medium/low/suspect).
3. **Feature engineering** — `extract_pois.py` pulls supermarkets,
   convenience stores, and existing vending machines from the same
   `.pbf`; `compute_distances.py` computes distance-to-nearest-supermarket
   and competitor density per building via a KD-tree.
4. **Scoring** — `score.py` combines estimated resident count and distance
   to the nearest supermarket into a single ranked score (see the
   methodology note in the script — an earlier version of this had the
   distance direction backwards, since corrected).
5. **Google verification** — OSM's grocery-store tagging turned out to
   have serious gaps in some areas (see below). `verify_groceries_google.py`
   spot-checks the current top-N buildings against the Google Places API;
   `iterative_verify.py` runs that in multiple rounds, re-ranking between
   rounds so buildings that get promoted after a correction also get
   checked, instead of trusting a ranking that was only checked once.
6. **Map** — `make_map.py` generates a single self-contained interactive
   HTML map with a live-adjustable score (sliders for population weight
   and "far enough" distance cutoff, recomputed client-side in the
   browser), a combined grocery layer (OSM + Google-verified), and an
   approximate foot-traffic heatmap (residential-density proxy — not real
   pedestrian counts).

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

## A second, bigger data quality problem: grocery coverage

The scoring model rewards buildings that are far from a supermarket (more
of a captive audience for a lobby vending machine). That only works if
"distance to nearest supermarket" is accurate — and OSM's coverage of
small grocery shops turned out to have serious gaps, concentrated in newer
developments.

Spot-checking the initial top-50 ranked buildings (mostly in **Azizi
Riviera**, a large newer complex) against the Google Places API found that
**50/50** had a real grocery store within 100m — most within 15-90m —
while OSM's data suggested the nearest supermarket was 2000m+ away. That's
not a minor gap: it meant the entire initial top-50 was largely an
artifact of one area's poor OSM tagging, not a genuine "underserved"
signal. Ground-floor retail in new developments gets leased out and
opened fast; volunteer OSM mapping of small, unbranded shops doesn't keep
up anywhere near as quickly as Google's crowd-sourced business listings
do.

`iterative_verify.py` corrects for this: it verifies the current top-N
against Google, recomputes the score using the corrected distance,
re-ranks, and repeats for a few rounds — because correcting the first
batch promotes different buildings into the top-N, and those haven't been
checked either. The interactive map's default view already uses
Google-verified distances wherever available.

## Usage

```bash
pip install -r requirements.txt

# 1. Collect all buildings in Dubai from a local OSM extract (fast, offline)
curl -L -o data/raw/gcc-states-latest.osm.pbf \
  https://download.geofabrik.de/asia/gcc-states-latest.osm.pbf
python src/collect/extract_from_pbf.py --pbf data/raw/gcc-states-latest.osm.pbf

# 2. Validate and estimate apartment counts
python src/features/validate_buildings.py

# 3. Collect POIs and compute distance/competition features
python src/collect/extract_pois.py --pbf data/raw/gcc-states-latest.osm.pbf
python src/features/compute_distances.py

# 4. Score
python src/scoring/score.py

# 5. (optional, needs a Google Places API key) Verify top candidates
export GOOGLE_MAPS_API_KEY="your-key-here"
python src/collect/iterative_verify.py

# 6. Generate the interactive map
python src/visualization/make_map.py
open map.html   # or just open the file in any browser
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
- **Google verification only covers a spot-checked subset**, not the full
  city. `iterative_verify.py` defaults to 3 rounds x 50 buildings = 150
  Google Places requests, comfortably inside the free pay-as-you-go tier
  (10,000 calls/month at the Essentials field-mask level used here) — but
  a full-city Google sweep would cost real money and wasn't in scope. The
  ~4,600 buildings never checked against Google still rely on OSM's
  (known to be gappy) grocery data.
- This is a discovery/exploration tool built on crowd-sourced OSM data, not
  a substitute for ground-truth foot traffic measurement or negotiation
  with building management.

## License

MIT — see `LICENSE`. Building data is from
[OpenStreetMap](https://www.openstreetmap.org), © OpenStreetMap
contributors, available under the [ODbL](https://opendatacommons.org/licenses/odbl/).
