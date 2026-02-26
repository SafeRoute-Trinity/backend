# GraphHopper Graph Cache

This folder stores local GraphHopper artifacts used by CH routing:

- `config.yml`: GraphHopper server config (CH enabled for `foot` profile)
- `data/map.osm.pbf`: input OSM extract (you provide this file)
- `cache/`: generated graph-cache (created by build script)

Use scripts in `../scripts/`:

- `graphhopper_build_cache.sh`
- `graphhopper_run_server.sh`

Example:

```bash
cd backend
./scripts/graphhopper_build_cache.sh --jar /path/to/graphhopper-web-11.0.jar --pbf ./graphhopper/data/map.osm.pbf
./scripts/graphhopper_run_server.sh --jar /path/to/graphhopper-web-11.0.jar
```
