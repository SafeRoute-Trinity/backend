#!/usr/bin/env python3
"""
Export PostGIS ways table to OSM XML, preserving safety_factor as a custom tag.

Usage:
  python3 scripts/export_ways_to_osm.py \
    --output ./graphhopper/data/map.osm \
    --table ways

Optional DB args:
  --host 127.0.0.1 --port 5432 --db saferoute_geo --user saferoute --password saferoute
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from xml.sax.saxutils import quoteattr

import psycopg2
from dotenv import load_dotenv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export PostGIS ways table to OSM XML")
    parser.add_argument("--output", required=True, help="Output OSM XML path (e.g. map.osm)")
    parser.add_argument("--table", default="ways", help="Source table name (default: ways)")
    parser.add_argument("--schema", default="public", help="Schema name (default: public)")

    parser.add_argument("--host", default=os.getenv("DATABASE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DATABASE_PORT", "5432")))
    parser.add_argument("--db", default=os.getenv("DATABASE_NAME", "saferoute_geo"))
    parser.add_argument("--user", default=os.getenv("DATABASE_USER", "saferoute"))
    parser.add_argument("--password", default=os.getenv("DATABASE_PASSWORD", ""))
    parser.add_argument(
        "--default-highway",
        default="residential",
        help="Fallback highway value when source row has no highway tag",
    )
    return parser.parse_args()


def get_columns(conn, schema: str, table: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]


def build_query(schema: str, table: str, columns: List[str]) -> str:
    geom_expr = (
        "CASE WHEN ST_SRID(geometry)=4326 THEN geometry ELSE ST_Transform(geometry, 4326) END"
    )
    select_cols = [
        "gid",
        f"ST_AsGeoJSON({geom_expr}) AS geojson",
        (
            "safety_factor"
            if "safety_factor" in columns
            else "NULL::double precision AS safety_factor"
        ),
        "highway" if "highway" in columns else "NULL::text AS highway",
        "name" if "name" in columns else "NULL::text AS name",
        "oneway" if "oneway" in columns else "NULL::text AS oneway",
    ]
    return f"""
        SELECT {", ".join(select_cols)}
        FROM {schema}.{table}
        WHERE geometry IS NOT NULL
        ORDER BY gid
    """


def normalize_lines(geojson_obj: Dict) -> List[List[List[float]]]:
    gtype = geojson_obj.get("type")
    coords = geojson_obj.get("coordinates")
    if gtype == "LineString" and isinstance(coords, list):
        return [coords]
    if gtype == "MultiLineString" and isinstance(coords, list):
        return [c for c in coords if isinstance(c, list)]
    return []


def to_key(lon: float, lat: float) -> Tuple[float, float]:
    return (round(lon, 7), round(lat, 7))


def sanitize_xml_text(value: str) -> str:
    """
    Keep only XML 1.0 legal chars.
    """

    def is_xml10_char(ch: str) -> bool:
        cp = ord(ch)
        return (
            cp == 0x9
            or cp == 0xA
            or cp == 0xD
            or (0x20 <= cp <= 0xD7FF)
            or (0xE000 <= cp <= 0xFFFD)
            or (0x10000 <= cp <= 0x10FFFF)
        )

    return "".join(ch for ch in value if is_xml10_char(ch))


def write_osm(
    out_path: Path,
    rows: List[Tuple],
    default_highway: str,
) -> None:
    nodes: Dict[Tuple[float, float], int] = {}
    ways: List[Tuple[int, List[int], Dict[str, str]]] = []
    next_node_id = 1
    next_way_id = 1

    for gid, geojson_text, safety_factor, highway, name, oneway in rows:
        try:
            gj = json.loads(geojson_text)
        except Exception:
            continue

        line_parts = normalize_lines(gj)
        if not line_parts:
            continue

        for part in line_parts:
            if len(part) < 2:
                continue

            nd_refs: List[int] = []
            for p in part:
                if not isinstance(p, list) or len(p) < 2:
                    continue
                lon = float(p[0])
                lat = float(p[1])
                key = to_key(lon, lat)
                if key not in nodes:
                    nodes[key] = next_node_id
                    next_node_id += 1
                nd_refs.append(nodes[key])

            if len(nd_refs) < 2:
                continue

            tags = {
                "highway": str(highway) if highway else default_highway,
                "foot": "yes",
                "safety_factor": str(safety_factor) if safety_factor is not None else "1.0",
                "gid": str(gid),
            }
            if name:
                name_text = sanitize_xml_text(str(name))
                if name_text and name_text.lower() != "nan":
                    tags["name"] = name_text
            if oneway:
                oneway_text = sanitize_xml_text(str(oneway))
                if oneway_text and oneway_text.lower() != "nan":
                    tags["oneway"] = oneway_text

            ways.append((next_way_id, nd_refs, tags))
            next_way_id += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with out_path.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<osm version="0.6" generator="saferoute-export">\n')

        for (lon, lat), node_id in nodes.items():
            f.write(
                f'  <node id="{node_id}" visible="true" version="1" timestamp="{ts}" '
                f'lat="{lat}" lon="{lon}" />\n'
            )

        for way_id, nd_refs, tags in ways:
            f.write(f'  <way id="{way_id}" visible="true" version="1" timestamp="{ts}">\n')
            for ref in nd_refs:
                f.write(f'    <nd ref="{ref}" />\n')
            for k, v in tags.items():
                safe_k = sanitize_xml_text(k)
                safe_v = sanitize_xml_text(v)
                f.write(f"    <tag k={quoteattr(safe_k)} v={quoteattr(safe_v)} />\n")
            f.write("  </way>\n")

        f.write("</osm>\n")

    print(f"Export complete: {out_path}")
    print(f"Nodes: {len(nodes)}")
    print(f"Ways:  {len(ways)}")
    print(
        "Next step (optional): convert XML to PBF for GraphHopper, e.g. `osmium cat map.osm -o map.osm.pbf`"
    )


def main() -> int:
    backend_env = Path(__file__).resolve().parents[1] / ".env"
    if backend_env.exists():
        load_dotenv(backend_env)

    args = parse_args()
    out_path = Path(args.output).resolve()

    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.db,
        user=args.user,
        password=args.password,
    )

    try:
        columns = get_columns(conn, args.schema, args.table)
        if "geometry" not in columns:
            print("Error: source table must contain `geometry` column.", file=sys.stderr)
            return 1
        if "gid" not in columns:
            print("Error: source table must contain `gid` column.", file=sys.stderr)
            return 1

        query = build_query(args.schema, args.table, columns)
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
        write_osm(out_path, rows, args.default_highway)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
