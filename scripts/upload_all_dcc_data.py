#!/usr/bin/env python3
"""
Upload DCC (Dublin City Council) datasets to PostGIS database.

This script loads public safety infrastructure data into four tables:
    - cctv_cameras      (from dcc_trafficcctv.csv)
    - garda_stations    (from dcc_garda.csv)
    - crime_statistics  (from crime_statistics_processed.csv)
    - street_lights     (from dcc_street_lights.csv)

Usage:
    python scripts/upload_all_dcc_data.py              # Upload all tables

Environment Variables:
    POSTGIS_DATABASE_URL or POSTGIS_HOST/PORT/USER/PASSWORD/DATABASE

Dependencies:
    pip install asyncpg
"""

import asyncio
import csv
import os
import sys
import uuid
from pathlib import Path
from typing import Dict

# Allow imports from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import asyncpg
except ImportError:
    print("Error: asyncpg is required. Install it with: pip install asyncpg")
    sys.exit(1)


def generate_uuid_from_id(prefix: str, csv_id: str) -> uuid.UUID:
    """
    Generate deterministic UUID from prefix and ID.

    Args:
        prefix: Identifier prefix (e.g., 'cctv', 'light')
        csv_id: Original ID from CSV file

    Returns:
        UUID v5 based on DNS namespace
    """
    namespace = uuid.NAMESPACE_DNS
    name = f"{prefix}-{csv_id}"
    return uuid.uuid5(namespace, name)


async def upload_cctv_cameras(conn: asyncpg.Connection, csv_path: str) -> Dict[str, int]:
    """
    Upload CCTV camera locations to cctv_cameras table.

    CSV Mapping:
        ID → source_id (UUID)
        Road_1 → road
        Latitude, Longitude → cctv_pt (PostGIS POINT)

    Returns:
        Dict with counts: inserted, updated, failed
    """
    stats = {"inserted": 0, "updated": 0, "failed": 0}

    print(f"\n{'='*80}")
    print("Uploading CCTV Cameras")
    print(f"{'='*80}")

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cameras = list(reader)

    print(f"Found {len(cameras)} records in CSV")

    query = """
    INSERT INTO cctv_cameras (source_id, road, cctv_pt)
    VALUES ($1, $2, ST_SetSRID(ST_MakePoint($3, $4), 4326))
    ON CONFLICT (source_id) DO UPDATE SET
        road = EXCLUDED.road,
        cctv_pt = EXCLUDED.cctv_pt,
        updated_at = now()
    RETURNING (xmax = 0) AS inserted;
    """

    for idx, row in enumerate(cameras, 1):
        try:
            source_id = generate_uuid_from_id("cctv", row["ID"].strip())
            road = row.get("Road_1", "").strip() or None
            lat = float(row["Latitude"])
            lon = float(row["Longitude"])

            result = await conn.fetchval(query, str(source_id), road, lon, lat)
            if result:
                stats["inserted"] += 1
            else:
                stats["updated"] += 1

            if idx % 50 == 0:
                print(f"  Progress: {idx}/{len(cameras)}")

        except Exception as e:
            stats["failed"] += 1
            print(f"  Error on row {idx}: {str(e)[:100]}")

    print(
        f"  ✓ Inserted: {stats['inserted']}, Updated: {stats['updated']}, Failed: {stats['failed']}"
    )
    return stats


async def upload_garda_stations(conn: asyncpg.Connection, csv_path: str) -> Dict[str, int]:
    """
    Upload Garda (police) station data to garda_stations table.

    CSV Mapping:
        Station  → station_name (unique key)
        Address1/2/3, Phone, Website  → respective columns
        Latitude, Longitude → location (PostGIS POINT)

    Returns:
        Dict with counts: inserted, updated, failed
    """
    stats = {"inserted": 0, "updated": 0, "failed": 0}

    print(f"\n{'='*80}")
    print("Uploading Garda Stations")
    print(f"{'='*80}")

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        stations = list(reader)

    print(f"Found {len(stations)} records in CSV")

    query = """
    INSERT INTO garda_stations (station_name, address1, address2, address3, phone, website, location)
    VALUES ($1, $2, $3, $4, $5, $6, ST_SetSRID(ST_MakePoint($7, $8), 4326))
    ON CONFLICT (station_name) DO UPDATE SET
        address1 = EXCLUDED.address1,
        address2 = EXCLUDED.address2,
        address3 = EXCLUDED.address3,
        phone = EXCLUDED.phone,
        website = EXCLUDED.website,
        location = EXCLUDED.location
    RETURNING (xmax = 0) AS inserted;
    """

    for idx, row in enumerate(stations, 1):
        try:
            station_name = row["Station "].strip()  # Note the space in column name
            address1 = row.get("Address1", "").strip() or None
            address2 = row.get("Address2", "").strip() or None
            address3 = row.get("Address3", "").strip() or None
            phone = row.get("Phone", "").strip() or None
            website = row.get("Website ", "").strip() or None  # Note the space
            lat = float(row["Latitude"])
            lon = float(row["Longitude "])  # Note the space

            result = await conn.fetchval(
                query, station_name, address1, address2, address3, phone, website, lon, lat
            )

            if result:
                stats["inserted"] += 1
            else:
                stats["updated"] += 1

        except Exception as e:
            stats["failed"] += 1
            print(f"  Error on row {idx}: {str(e)[:100]}")

    print(
        f"  ✓ Inserted: {stats['inserted']}, Updated: {stats['updated']}, Failed: {stats['failed']}"
    )
    return stats


async def upload_crime_statistics(conn: asyncpg.Connection, csv_path: str) -> Dict[str, int]:
    """
    Upload crime statistics to crime_statistics table.

    Note: Requires pre-processed CSV (run process_crime_stats.py first).

    CSV Mapping:
        station_name → station_name (FK to garda_stations)
        incident_count → incident_count (aggregated total)

    Returns:
        Dict with counts: inserted, updated, failed
    """
    stats = {"inserted": 0, "updated": 0, "failed": 0}

    print(f"\n{'='*80}")
    print("Uploading Crime Statistics")
    print(f"{'='*80}")

    # Validate that processed file is used
    if "processed" not in str(csv_path):
        print("  ⚠️  ERROR: Please use processed crime statistics file")
        print("  Run first: python3 scripts/process_crime_stats.py")
        print("  Then update the file path to use: crime_statistics_processed.csv")
        return {"inserted": 0, "updated": 0, "failed": 0, "skipped": 1}

    crime_stats = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                crime_stats.append(row)
    except FileNotFoundError:
        print(f"  ⚠️  File not found: {csv_path}")
        print("  Run first: python3 scripts/process_crime_stats.py")
        return stats
    except Exception as e:
        print(f"  Error reading CSV: {e}")
        return stats

    if not crime_stats:
        print("  No data to upload")
        return stats

    print(f"Found {len(crime_stats)} records in processed CSV")

    # Upsert query (requires UNIQUE constraint on station_name)
    query_with_upsert = """
        INSERT INTO crime_statistics (station_name, incident_count)
        VALUES ($1, $2)
        ON CONFLICT (station_name) 
        DO UPDATE SET 
            incident_count = EXCLUDED.incident_count
        RETURNING crime_static_id
    """

    # Fallback query if no unique constraint exists
    query = """
        INSERT INTO crime_statistics (station_name, incident_count)
        VALUES ($1, $2)
        RETURNING crime_static_id
    """

    use_simple_insert = False

    for idx, row in enumerate(crime_stats, 1):
        try:
            station_name = row["station_name"].strip()
            incident_count = int(row["incident_count"])

            if not use_simple_insert:
                try:
                    result = await conn.fetchval(query_with_upsert, station_name, incident_count)
                    if result:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1
                except Exception as e:
                    if "no unique or exclusion constraint" in str(e):
                        use_simple_insert = True  # Fallback for remaining rows
                        print("  ⚠️  No UNIQUE constraint on station_name, using simple INSERT")
                        result = await conn.fetchval(query, station_name, incident_count)
                        if result:
                            stats["inserted"] += 1
                    else:
                        raise
            else:
                result = await conn.fetchval(query, station_name, incident_count)
                if result:
                    stats["inserted"] += 1

        except Exception as e:
            stats["failed"] += 1
            if idx <= 5:  # Limit error output
                print(f"  Error on row {idx}: {str(e)[:100]}")

    print(
        f"  ✓ Inserted: {stats['inserted']}, Updated: {stats['updated']}, Failed: {stats['failed']}"
    )
    return stats


async def upload_street_lights(conn: asyncpg.Connection, csv_path: str) -> Dict[str, int]:
    """
    Upload street light locations to street_lights table.

    CSV Mapping:
        ID → source_id
        site_name, unit_no, unit_type → respective columns
        Latitude, Longitude → light_pt (PostGIS POINT)

    Note: Handles multiple encodings and validates coordinates.

    Returns:
        Dict with counts: inserted, updated, failed
    """
    stats = {"inserted": 0, "updated": 0, "failed": 0}

    print(f"\n{'='*80}")
    print("Uploading Street Lights")
    print(f"{'='*80}")

    # Detect Excel files masquerading as CSV
    if csv_path.endswith(".csv"):
        with open(csv_path, "rb") as f:
            header = f.read(4)
            if header == b"PK\x03\x04":  # ZIP/Excel magic bytes
                print("  ⚠️  ERROR: File is Microsoft Excel format (.xlsx), not CSV")
                print("  Please convert to CSV first using:")
                print("    - Excel: File → Save As → CSV UTF-8")
                print("    - Command line: ssconvert file.xlsx file.csv")
                print("    - Python: pandas.read_excel('file.xlsx').to_csv('file.csv')")
                stats["failed"] = -1
                return stats

    try:
        # Attempt reading with multiple encodings
        lights = []
        encodings = ["utf-8", "latin-1", "cp1252"]

        for encoding in encodings:
            try:
                with open(csv_path, "r", encoding=encoding, errors="replace") as f:
                    content = f.read().replace("\x00", "")  # Strip NUL bytes
                    reader = csv.DictReader(content.splitlines())
                    lights = list(reader)
                    break
            except Exception:
                continue

        if not lights:
            raise Exception("Could not read CSV with any encoding")

    except Exception as e:
        print(f"  Error reading CSV: {e}")
        stats["failed"] = -1
        return stats

    print(f"Found {len(lights)} records in CSV")

    # Upsert query (requires UNIQUE constraint on source_id)
    query_with_upsert = """
    INSERT INTO street_lights (source_id, site_name, unit_no, unit_type, light_pt)
    VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($5, $6), 4326))
    ON CONFLICT (source_id) DO UPDATE SET
        site_name = EXCLUDED.site_name,
        unit_no = EXCLUDED.unit_no,
        unit_type = EXCLUDED.unit_type,
        light_pt = EXCLUDED.light_pt
    RETURNING (xmax = 0) AS inserted;
    """

    # Fallback query if no unique constraint exists
    query = """
    INSERT INTO street_lights (source_id, site_name, unit_no, unit_type, light_pt)
    VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($5, $6), 4326))
    RETURNING light_id;
    """

    use_simple_insert = False

    for idx, row in enumerate(lights, 1):
        try:
            # Handle various ID column names
            source_id_val = row.get("ID", row.get("id", str(idx)))
            if source_id_val:
                source_id_val = str(source_id_val).strip()
            else:
                source_id_val = str(idx)

            # Parse as integer (handles "5.0" format)
            try:
                source_id = int(float(source_id_val))
            except (ValueError, TypeError):
                source_id = idx

            site_name = row.get("site_name", row.get("Site_Name", ""))
            site_name = str(site_name).strip() if site_name else None

            unit_no = row.get("unit_no", row.get("Unit_No", ""))
            unit_no = str(unit_no).strip() if unit_no else None

            unit_type = row.get("unit_type", row.get("Unit_Type", ""))
            unit_type = str(unit_type).strip() if unit_type else None

            # Handle various coordinate column names
            lat_str = row.get("Latitude", row.get("latitude", row.get("LAT", "0")))
            lon_str = row.get("Longitude", row.get("longitude", row.get("LON", "0")))

            try:
                lat = float(lat_str) if lat_str and str(lat_str).strip() else 0
                lon = float(lon_str) if lon_str and str(lon_str).strip() else 0
            except (ValueError, TypeError):
                lat, lon = 0, 0

            if lat == 0 or lon == 0:
                stats["failed"] += 1
                continue

            if not use_simple_insert:
                try:
                    result = await conn.fetchval(
                        query_with_upsert, source_id, site_name, unit_no, unit_type, lon, lat
                    )
                    if result:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1
                except Exception as e:
                    if "no unique or exclusion constraint" in str(e):
                        use_simple_insert = True  # Fallback for remaining rows
                        print("  ⚠️  No UNIQUE constraint on source_id, using simple INSERT")
                        result = await conn.fetchval(
                            query, source_id, site_name, unit_no, unit_type, lon, lat
                        )
                        if result:
                            stats["inserted"] += 1
                    else:
                        raise
            else:
                result = await conn.fetchval(
                    query, source_id, site_name, unit_no, unit_type, lon, lat
                )
                if result:
                    stats["inserted"] += 1

            if idx % 1000 == 0:  # Progress update
                print(f"  Progress: {idx}/{len(lights)}")

        except Exception as e:
            stats["failed"] += 1
            if stats["failed"] <= 5:  # Limit error output
                print(f"  Error on row {idx}: {str(e)[:100]}")

    print(
        f"  ✓ Inserted: {stats['inserted']}, Updated: {stats['updated']}, Failed: {stats['failed']}"
    )
    return stats


def get_postgis_url() -> str:
    """
    Build PostGIS connection URL from environment variables.

    Checks POSTGIS_DATABASE_URL first, then falls back to individual vars:
    POSTGIS_HOST, POSTGIS_PORT, POSTGIS_USER, POSTGIS_PASSWORD, POSTGIS_DATABASE
    """
    url = os.getenv("POSTGIS_DATABASE_URL")
    if url:
        if url.startswith("postgresql+asyncpg://"):
            url = url.replace("postgresql+asyncpg://", "postgresql://")
        return url

    host = os.getenv("POSTGIS_HOST", "127.0.0.1")
    port = os.getenv("POSTGIS_PORT", "5433")
    user = os.getenv("POSTGIS_USER", "saferoute")
    password = os.getenv("POSTGIS_PASSWORD", "")
    database = os.getenv("POSTGIS_DATABASE", "saferoute_geo")

    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def main():
    """
    Main execution flow.

    Parses CLI arguments, validates file paths, connects to PostGIS,
    and uploads selected table(s) with progress reporting.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Upload DCC data to PostGIS database")
    parser.add_argument(
        "--table",
        choices=["cctv_cameras", "garda_stations", "crime_statistics", "street_lights", "all"],
        default="all",
        help="Which table to upload (default: all)",
    )
    args = parser.parse_args()

    # Define CSV file paths
    base_dir = Path(__file__).parent / "dcc_data"
    files = {
        "cctv_cameras": base_dir / "dcc_trafficcctv.csv",
        "garda_stations": base_dir / "dcc_garda.csv",
        "crime_statistics": base_dir / "crime_statistics_processed.csv",
        "street_lights": base_dir / "dcc_street_lights.csv",
    }

    # Validate file existence
    for table, path in files.items():
        if args.table in ["all", table] and not path.exists():
            print(f"Error: CSV file not found: {path}")
            sys.exit(1)

    db_url = get_postgis_url()

    print("=" * 80)
    print("DCC DATA UPLOAD UTILITY")
    print("=" * 80)
    print(f"Database: {db_url.split('@')[1] if '@' in db_url else 'N/A'}")
    print(f"Upload mode: {args.table}")
    print("=" * 80)

    conn = await asyncpg.connect(db_url)

    try:
        all_stats = {}

        if args.table in ["all", "cctv_cameras"]:
            all_stats["cctv_cameras"] = await upload_cctv_cameras(conn, str(files["cctv_cameras"]))

        if args.table in ["all", "garda_stations"]:
            all_stats["garda_stations"] = await upload_garda_stations(
                conn, str(files["garda_stations"])
            )

        if args.table in ["all", "crime_statistics"]:
            all_stats["crime_statistics"] = await upload_crime_statistics(
                conn, str(files["crime_statistics"])
            )

        if args.table in ["all", "street_lights"]:
            all_stats["street_lights"] = await upload_street_lights(
                conn, str(files["street_lights"])
            )

        # Print summary
        print(f"\n{'='*80}")
        print("UPLOAD SUMMARY")
        print(f"{'='*80}")

        for table, stats in all_stats.items():
            print(
                f"{table:20s} - Inserted: {stats['inserted']:4d}, Updated: {stats['updated']:4d}, Failed: {stats['failed']:4d}"
            )

        print(f"{'='*80}\n")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
