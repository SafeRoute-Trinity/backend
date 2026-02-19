# Export From PostGIS (ways -> OSM -> PBF -> graph-cache)

If your routing edges live in PostGIS and include custom `safety_factor`, use this flow.

## 1) Export `ways` table to OSM XML

```bash
cd /Users/yuanchenfan/code/saferoute/backend
python3 scripts/export_ways_to_osm.py \
  --host 127.0.0.1 \
  --port 5432 \
  --db saferoute_geo \
  --user saferoute \
  --password 'YOUR_PASSWORD' \
  --schema public \
  --table ways \
  --output ./graphhopper/data/map.osm
```

This keeps `safety_factor` as a custom OSM tag.

## 2) Convert OSM XML to OSM PBF

Requires `osmium-tool`:

```bash
brew install osmium-tool
osmium cat ./graphhopper/data/map.osm -o ./graphhopper/data/map.osm.pbf
```

## 3) Build GraphHopper CH graph-cache

```bash
./scripts/graphhopper_build_cache.sh --jar ./graphhopper/graphhopper-web-11.0.jar
```

## 4) Start GraphHopper server

```bash
./scripts/graphhopper_run_server.sh --jar ./graphhopper/graphhopper-web-11.0.jar
```

Then check:

```bash
curl 'http://127.0.0.1:8989/route?point=53.35,-6.26&point=53.34,-6.27&profile=foot&points_encoded=false'
```

## Notes

- GraphHopper does not read PostGIS tables directly at query time.
- It reads preprocessed graph files from `graphhopper/cache`.
- If you update `safety_factor` yearly, re-run steps 1-3 yearly.
