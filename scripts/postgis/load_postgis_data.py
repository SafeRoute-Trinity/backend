#!/usr/bin/env python3
"""
Utilities for PostGIS schema deployment and data loading.

- apply-schema: run DDL from postgis_schema_from_dump.sql (+ optional geo ERD tables).
- apply-geo-tables: run only geo_feature_tables.sql (use after load-dump).
- load-dump: stream a pg_dump plain SQL file (e.g. postgis_env.sql) into psql after
  stripping pg_dump 17 \\restrict lines for older clients.
- upload: insert rows into any existing public table from CSV or JSON. Header names must
  match PostgreSQL column names (after strip). Types are read from pg_catalog; geometry
  columns accept WKT/EWKT; json/jsonb accept JSON text or objects; uuid[] accepts
  Postgres array text e.g. {uuid1,uuid2}.
- import-dcc-cctv: convenience import for DCC traffic CCTV CSV into cctv_cameras.

Connection: set one of POSTGIS_DATABASE_URL, DATABASE_URL, or POSTGRES_URL (postgresql://...).
SQLAlchemy-style URLs with +asyncpg are normalized for psql/psycopg2.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extensions import connection as PGConnection

SCRIPT_DIR = Path(__file__).resolve().parent

# public table names: letters, digits, underscore; must start with letter or _
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def _safe_array_cast(pg_type: str) -> bool:
    """format_type from pg_catalog; block obvious SQL injection in CAST(... AS t)."""
    if not pg_type or pg_type.strip() != pg_type:
        return False
    if any(x in pg_type for x in (";", "--", "/*", "'", '"', "\n", "\x00")):
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_ ()\[\],]+", pg_type))


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


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fetch_public_table_columns(cur: Any, table: str) -> Dict[str, str]:
    """
    column_name -> pg_catalog.format_type(atttypid, atttypmod) e.g. uuid, geometry(Point,4326), text[].
    """
    cur.execute(
        """
        SELECT a.attname AS column_name,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS pg_type
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (table,),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"Table public.{table} does not exist or has no columns.")
    return {r[0]: r[1] for r in rows}


def _binding_for_pg_type(pg_type: str) -> Tuple[str, Optional[str]]:
    """
    Returns (kind, optional_cast_suffix).
    kind: geometry | jsonb | uuid_array | array_cast | scalar
    """
    t = pg_type.strip()
    tl = t.lower()
    if tl.startswith("geometry") or tl == "geometry":
        return "geometry", None
    if tl.startswith("geography"):
        return "geography", None
    if tl in ("json", "jsonb") or tl.startswith("json "):
        return "jsonb", None
    if "jsonb" in tl or tl.startswith("jsonb"):
        return "jsonb", None
    if tl.endswith("[]"):
        inner = tl[:-2].strip()
        if inner == "uuid":
            return "uuid_array", None
        if not _safe_array_cast(t):
            raise ValueError(f"Unsupported or unsafe array type for cast: {pg_type!r}")
        return "array_cast", t
    return "scalar", None


def _parse_uuid_array(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    s = str(value).strip()
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip('"') for p in inner.split(",")]
    raise ValueError(f"Expected uuid[] as {{...}} or JSON list, got {value!r}")


def upload_row(
    cur: Any,
    table: str,
    row: Dict[str, Any],
    col_types: Dict[str, str],
    on_conflict: str,
 *,
    warn_unknown: bool = True,
) -> None:
    """Insert one row; keys must match public.table columns (case-sensitive after strip)."""
    columns: List[str] = []
    fragments: List[str] = []
    params: List[Any] = []

    unknown = [k for k in row if k.strip() and k.strip() not in col_types]
    if unknown and warn_unknown:
        print(
            f"Warning: skipping unknown columns not in public.{table}: {unknown}",
            file=sys.stderr,
        )

    for key in sorted(row.keys()):
        raw = row[key]
        if raw is None or str(raw).strip() == "":
            continue
        col = key.strip()
        if col not in col_types:
            continue

        pg_type = col_types[col]
        kind, cast_type = _binding_for_pg_type(pg_type)

        if kind == "geometry":
            columns.append(col)
            fragments.append("ST_GeomFromEWKT(%s)")
            params.append(str(raw))
        elif kind == "geography":
            columns.append(col)
            fragments.append("ST_GeogFromEWKT(%s)")
            params.append(str(raw))
        elif kind == "jsonb":
            columns.append(col)
            fragments.append("%s::jsonb")
            if isinstance(raw, (dict, list)):
                params.append(json.dumps(raw))
            else:
                params.append(str(raw))
        elif kind == "uuid_array":
            arr = _parse_uuid_array(raw)
            columns.append(col)
            fragments.append("%s::uuid[]")
            params.append("{" + ",".join(arr) + "}")
        elif kind == "array_cast":
            assert cast_type is not None
            columns.append(col)
            fragments.append("CAST(%s AS " + cast_type + ")")
            params.append(str(raw))
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
    encoding: str,
    *,
    strict_columns: bool,
) -> None:
    if not _TABLE_NAME_RE.match(table):
        raise SystemExit(
            f"Invalid table name {table!r}; use a single identifier [A-Za-z_][A-Za-z0-9_]*."
        )

    rows: List[Dict[str, Any]]
    if fmt == "json":
        data = json.loads(path.read_text(encoding=encoding))
        if not isinstance(data, list):
            raise SystemExit("JSON upload file must be an array of objects.")
        rows = [r for r in data if isinstance(r, dict)]
    elif fmt == "csv":
        with path.open(newline="", encoding=encoding) as fh:
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
                col_types = fetch_public_table_columns(cur, table)
                all_keys: set[str] = set()
                for r in rows:
                    all_keys |= {k.strip() for k in r if k and k.strip()}
                unknown = sorted(all_keys - set(col_types))
                if unknown:
                    if strict_columns:
                        raise SystemExit(
                            f"Strict mode: columns not in public.{table}: {unknown}. "
                            f"Table has: {sorted(col_types)}"
                        )
                    print(
                        f"Warning: skipping columns not in public.{table}: {unknown}",
                        file=sys.stderr,
                    )
                for row in rows:
                    upload_row(
                        cur,
                        table,
                        row,
                        col_types,
                        on_conflict,
                        warn_unknown=False,
                    )
    finally:
        conn.close()
    print(f"Uploaded {len(rows)} row(s) into public.{table}.", file=sys.stderr)


_DCC_TRAFFIC_CCTV_SOURCE_UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS,
    "saferoute.source.dcc_trafficcctv",
)


def cmd_import_dcc_traffic_cctv(dsn: str, path: Path, on_conflict: str) -> None:
    """DCC CSV ID,Road_1,Latitude,Longitude -> public.cctv_cameras."""
    if not path.is_file():
        raise SystemExit(f"CSV not found: {path}")

    source_id = str(_DCC_TRAFFIC_CCTV_SOURCE_UUID)
    conn = _connect(dsn)
    n = 0
    try:
        with conn:
            with conn.cursor() as cur:
                col_types = fetch_public_table_columns(cur, "cctv_cameras")
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
                            col_types,
                            on_conflict,
                            warn_unknown=False,
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
        help="Insert into any public table from CSV or JSON (headers = column names).",
    )
    s3.add_argument(
        "table",
        help="Destination table name in schema public (e.g. cctv_cameras).",
    )
    s3.add_argument("file", type=Path)
    s3.add_argument(
        "--format",
        choices=("csv", "json"),
        default="csv",
        help="Default csv.",
    )
    s3.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="Text encoding for CSV/JSON (default utf-8-sig for Excel-friendly CSV).",
    )
    s3.add_argument(
        "--strict-columns",
        action="store_true",
        help="Fail if the file has columns that are not in the table (default: warn and skip).",
    )
    s3.add_argument(
        "--on-conflict",
        choices=("error", "nothing"),
        default="error",
        help="nothing = ON CONFLICT DO NOTHING (PK/unique violation).",
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
        mode = "nothing" if args.on_conflict == "nothing" else "error"
        cmd_upload(
            dsn,
            args.table,
            args.file,
            args.format,
            mode,
            args.encoding,
            strict_columns=args.strict_columns,
        )
    elif args.command == "import-dcc-cctv":
        mode = "nothing" if args.on_conflict == "nothing" else "error"
        cmd_import_dcc_traffic_cctv(dsn, args.file, mode)
    else:
        raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
