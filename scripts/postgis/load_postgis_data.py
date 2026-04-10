#!/usr/bin/env python3
"""
Utilities for PostGIS schema deployment and data loading.

- apply-schema: run DDL from postgis_schema_from_dump.sql (+ optional geo ERD tables).
- apply-geo-tables: run only geo_feature_tables.sql (use after load-dump).
- load-dump: stream a pg_dump plain SQL file (e.g. postgis_env.sql) into psql after
  stripping pg_dump 17 \\restrict lines for older clients.
- upload: insert rows into geo feature tables from CSV or JSON (see geo_feature_tables.sql).
- import-dcc-cctv: map Dublin DCC traffic CCTV CSV (ID,Road_1,Latitude,Longitude) into public.cctv_cameras  using columns camera_id, source_id, road, cctv_pt (updated_at defaults in DB).

Connection: set one of POSTGIS_DATABASE_URL, DATABASE_URL, or POSTGRES_URL (postgresql://...).
SQLAlchemy-style URLs with +asyncpg are normalized for psql/psycopg2.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set

import psycopg2
from psycopg2.extensions import connection as PGConnection

SCRIPT_DIR = Path(__file__).resolve().parent

ALLOWED_UPLOAD_TABLES = frozenset(
    {
        "route_segment",
        "routes",
        "cctv_cameras",
        "street_lights",
        "garda_stations",
        "crime_statistics",
    }
)

# Columns that receive WKT/EWKT and are bound via ST_GeomFromEWKT(...)
GEOMETRY_COLUMNS: Dict[str, Set[str]] = {
    "route_segment": {"geom"},
    "routes": {"origin", "destination", "full_route"},
    "cctv_cameras": {"cctv_pt"},
    "street_lights": {"light_pt"},
    "garda_stations": {"location"},
}

UUID_ARRAY_COLUMNS: Dict[str, Set[str]] = {
    "routes": {"route_segment_ids"},
}

JSON_COLUMNS: Dict[str, Set[str]] = {
    "street_lights": {"unit_type"},
}

TABLE_COLUMNS: Dict[str, Set[str]] = {
    "route_segment": {"route_segment_id", "name", "weight", "geom"},
    "routes": {
        "route_id",
        "route_segment_ids",
        "user_id",
        "transport_mode",
        "origin",
        "destination",
        "full_route",
        "average_safety_score",
        "created_at",
        "updated_at",
    },
    "cctv_cameras": {"camera_id", "source_id", "road", "cctv_pt", "updated_at"},
    "street_lights": {
        "light_id",
        "source_id",
        "site_name",
        "unit_no",
        "unit_type",
        "light_pt",
        "updated_at",
    },
    "garda_stations": {
        "station_id",
        "station_name",
        "address1",
        "address2",
        "address3",
        "phone",
        "website",
        "location",
    },
    "crime_statistics": {
        "crime_stat_id",
        "station_name",
        "incident_count",
    },
}


def normalize_db_url(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip()
    if "+" in u and "://" in u:
        scheme, rest = u.split("://", 1)
        if "+" in scheme:
            scheme = scheme.split("+")[0]
            u = f"{scheme}://{rest}"
    return u


def resolve_dsn(explicit: Optional[str] = None) -> str:
    if explicit:
        n = normalize_db_url(explicit)
        if n:
            return n
    for key in ("POSTGIS_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL"):
        n = normalize_db_url(os.environ.get(key))
        if n:
            return n
    raise SystemExit(
        "Set POSTGIS_DATABASE_URL, DATABASE_URL, or POSTGRES_URL "
        "(postgresql://user:pass@host:port/dbname)."
    )


def find_psql() -> Optional[str]:
    return shutil.which("psql")


def _iter_sql_statements(sql_text: str) -> Iterator[str]:
    """Split a migration-style SQL file on semicolon line endings; skip -- comments."""
    chunk: List[str] = []
    for line in sql_text.splitlines():
        if line.lstrip().startswith("--"):
            continue
        chunk.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(chunk).strip()
            chunk = []
            if stmt:
                yield stmt


def run_sql_file_psycopg2(dsn: str, sql_path: Path) -> None:
    """Run a SQL file without psql (Windows-friendly). Not suitable for COPY-heavy dumps."""
    raw = sql_path.read_text(encoding="utf-8")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for stmt in _iter_sql_statements(raw):
                cur.execute(stmt)
    finally:
        conn.close()


def run_psql_file(dsn: str, sql_path: Path, extra_args: Optional[Sequence[str]] = None) -> None:
    psql_bin = find_psql()
    if psql_bin:
        cmd: List[str] = [
            psql_bin,
            dsn,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(sql_path),
        ]
        if extra_args:
            cmd.extend(extra_args)
        subprocess.run(cmd, check=True)
    else:
        run_sql_file_psycopg2(dsn, sql_path)


def iter_sanitized_pg_dump_lines(path: Path) -> Iterator[str]:
    """Drop pg_dump 17 \\restrict / \\unrestrict directives for psql compatibility."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.lstrip()
            if stripped.startswith("\\restrict ") or stripped.startswith("\\unrestrict "):
                continue
            yield line


def run_psql_stdin(dsn: str, lines: Iterable[str]) -> None:
    psql_bin = find_psql()
    if not psql_bin:
        raise SystemExit(
            "psql not found on PATH; install PostgreSQL client tools, or use kubectl:\n"
            "  kubectl exec -i postgis-0 -n <ns> -- psql -U saferoute -d saferoute_geo "
            "-v ON_ERROR_STOP=1 -f - < dump.sql"
        )
    proc = subprocess.Popen(
        [psql_bin, dsn, "-v", "ON_ERROR_STOP=1"],
        stdin=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin
    try:
        for chunk in lines:
            proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        proc.wait()
        raise SystemExit("psql closed stdin unexpectedly (see errors above).")
    ret = proc.wait()
    if ret != 0:
        raise SystemExit(f"psql exited with code {ret}")


def _connect(dsn: str) -> PGConnection:
    return psycopg2.connect(dsn)


def _parse_uuid_array(value: Any) -> Optional[List[str]]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [str(x) for x in value]
    s = str(value).strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip('"') for p in inner.split(",")]
    raise ValueError(f"Cannot parse uuid[] from {value!r}")


def _scalar_param(table: str, column: str, raw: Any) -> Any:
    if column in JSON_COLUMNS.get(table, set()):
        if isinstance(raw, (dict, list)):
            return json.dumps(raw)
        return str(raw)
    return raw


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def upload_row(cur: Any, table: str, row: Dict[str, Any], on_conflict: str) -> None:
    if table not in ALLOWED_UPLOAD_TABLES:
        raise ValueError(f"Upload not allowed for table {table!r}")

    allowed = TABLE_COLUMNS[table]
    columns: List[str] = []
    fragments: List[str] = []
    params: List[Any] = []

    for key in sorted(row.keys()):
        raw = row[key]
        if raw is None or str(raw).strip() == "":
            continue
        col = key.strip()
        if col not in allowed:
            raise ValueError(f"Column {col!r} is not allowed for table {table!r}")

        if col in UUID_ARRAY_COLUMNS.get(table, set()):
            arr = _parse_uuid_array(raw)
            if arr is None:
                continue
            columns.append(col)
            fragments.append("%s::uuid[]")
            params.append("{" + ",".join(arr) + "}")
        elif col in GEOMETRY_COLUMNS.get(table, set()):
            columns.append(col)
            fragments.append("ST_GeomFromEWKT(%s)")
            params.append(str(raw))
        elif col in JSON_COLUMNS.get(table, set()):
            columns.append(col)
            fragments.append("%s::jsonb")
            params.append(_scalar_param(table, col, raw))
        else:
            columns.append(col)
            fragments.append("%s")
            params.append(raw)

    if not columns:
        return

    cols_sql = ", ".join(_quote_ident(c) for c in columns)
    vals_sql = ", ".join(fragments)
    q = f"INSERT INTO public.{_quote_ident(table)} ({cols_sql}) VALUES ({vals_sql})"
    if on_conflict == "nothing":
        q += " ON CONFLICT DO NOTHING"
    cur.execute(q, params)


def cmd_apply_schema(dsn: str, with_geo: bool) -> None:
    run_psql_file(dsn, SCRIPT_DIR / "postgis_schema_from_dump.sql")
    if with_geo:
        cmd_apply_geo_tables(dsn)


def cmd_apply_geo_tables(dsn: str) -> None:
    run_psql_file(dsn, SCRIPT_DIR / "geo_feature_tables.sql")


def cmd_load_dump(dsn: str, dump_path: Path) -> None:
    if not dump_path.is_file():
        raise SystemExit(f"Dump file not found: {dump_path}")
    print(f"Loading {dump_path} via psql (this may take a long time)...", file=sys.stderr)
    run_psql_stdin(dsn, iter_sanitized_pg_dump_lines(dump_path))
    print("Done.", file=sys.stderr)


def cmd_upload(
    dsn: str,
    table: str,
    path: Path,
    fmt: str,
    on_conflict: str,
) -> None:
    if table not in ALLOWED_UPLOAD_TABLES:
        raise SystemExit(f"Table must be one of: {', '.join(sorted(ALLOWED_UPLOAD_TABLES))}")

    rows: List[Dict[str, Any]]
    if fmt == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SystemExit("JSON upload file must be an array of objects.")
        rows = [r for r in data if isinstance(r, dict)]
    elif fmt == "csv":
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise SystemExit("CSV has no header row.")
            rows = list(reader)
    else:
        raise SystemExit("format must be csv or json")

    conn = _connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                for row in rows:
                    upload_row(cur, table, row, on_conflict)
    finally:
        conn.close()
    print(f"Uploaded {len(rows)} row(s) into public.{table}.", file=sys.stderr)


# Stable UUID for all rows from the DCC traffic CCTV dataset (data lineage).
_DCC_TRAFFIC_CCTV_SOURCE_UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS,
    "saferoute.source.dcc_trafficcctv",
)


def cmd_import_dcc_traffic_cctv(dsn: str, path: Path, on_conflict: str) -> None:
    """
    Import Dublin City Council-style export: ID, Road_1, Latitude, Longitude
    -> public.cctv_cameras (camera_id, source_id, road, cctv_pt).
    """
    if not path.is_file():
        raise SystemExit(f"CSV not found: {path}")

    source_id = str(_DCC_TRAFFIC_CCTV_SOURCE_UUID)
    conn = _connect(dsn)
    n = 0
    try:
        with conn:
            with conn.cursor() as cur:
                with path.open(newline="", encoding="utf-8-sig") as fh:
                    reader = csv.DictReader(fh)
                    if not reader.fieldnames:
                        raise SystemExit("CSV has no header row.")
                    fields = {f.strip() for f in reader.fieldnames if f}
                    required = {"ID", "Road_1", "Latitude", "Longitude"}
                    if not required.issubset(fields):
                        raise SystemExit(
                            f"CSV must include columns {sorted(required)}; got {sorted(fields)}"
                        )
                    for raw in reader:
                        dcc_id = str(raw["ID"]).strip()
                        if not dcc_id:
                            continue
                        road = str(raw["Road_1"]).strip()
                        lat = float(raw["Latitude"])
                        lon = float(raw["Longitude"])
                        camera_id = str(
                            uuid.uuid5(
                                uuid.NAMESPACE_DNS,
                                f"saferoute.cctv.dcc_trafficcctv:{dcc_id}",
                            )
                        )
                        ewkt = f"SRID=4326;POINT({lon} {lat})"
                        upload_row(
                            cur,
                            "cctv_cameras",
                            {
                                "camera_id": camera_id,
                                "source_id": source_id,
                                "road": road,
                                "cctv_pt": ewkt,
                            },
                            on_conflict,
                        )
                        n += 1
    finally:
        conn.close()
    print(f"Imported {n} row(s) into public.cctv_cameras.", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dsn",
        help="postgresql://... (overrides env)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s1 = sub.add_parser("apply-schema", help="Create tables from extracted DDL files.")
    s1.add_argument(
        "--with-geo-tables",
        action="store_true",
        help="Also apply geo_feature_tables.sql (ERD safety layer).",
    )

    sub.add_parser(
        "apply-geo-tables",
        help="Create only geo_feature_tables.sql (after load-dump or existing DB).",
    )

    s2 = sub.add_parser(
        "load-dump",
        help="Restore a pg_dump plain SQL file (e.g. postgis_env.sql). Empty DB recommended.",
    )
    s2.add_argument(
        "dump_file",
        type=Path,
        help="Path to postgis_env.sql",
    )

    s3 = sub.add_parser(
        "upload",
        help="Insert into public geo tables from CSV or JSON.",
    )
    s3.add_argument(
        "table",
        choices=sorted(ALLOWED_UPLOAD_TABLES),
    )
    s3.add_argument("file", type=Path)
    s3.add_argument(
        "--format",
        choices=("csv", "json"),
        required=True,
    )
    s3.add_argument(
        "--on-conflict",
        choices=("error", "nothing"),
        default="error",
        help="nothing = ON CONFLICT DO NOTHING (requires unique/PK conflict).",
    )

    s4 = sub.add_parser(
        "import-dcc-cctv",
        help="Load DCC CSV (ID,Road_1,Latitude,Longitude) into public.cctv_cameras.",
    )
    s4.add_argument("file", type=Path, help="Path to dcc_trafficcctv_*.csv")
    s4.add_argument(
        "--on-conflict",
        choices=("error", "nothing"),
        default="nothing",
        help="Default nothing so re-imports are safe.",
    )

    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    dsn = resolve_dsn(args.dsn)

    if args.command == "apply-schema":
        cmd_apply_schema(dsn, args.with_geo_tables)
    elif args.command == "apply-geo-tables":
        cmd_apply_geo_tables(dsn)
    elif args.command == "load-dump":
        cmd_load_dump(dsn, args.dump_file)
    elif args.command == "upload":
        if args.on_conflict == "nothing":
            cmd_upload(dsn, args.table, args.file, args.format, "nothing")
        else:
            cmd_upload(dsn, args.table, args.file, args.format, "error")
    elif args.command == "import-dcc-cctv":
        mode = "nothing" if args.on_conflict == "nothing" else "error"
        cmd_import_dcc_traffic_cctv(dsn, args.file, mode)
    else:
        raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
