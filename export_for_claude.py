"""
export_for_claude.py
Run this whenever you want data to hand to Claude. Two modes:

  Statewide - everything, no filtering:
      python export_for_claude.py

  A specific area (lat, lon, radius in km - radius optional, defaults to
  config.json's default_export_radius_km):
      python export_for_claude.py -29.70 121.90 25

Writes two CSV files to output/:
  - current_tenements_<scope>_<date>.csv   - current status of everything in scope
  - change_history_<scope>_<date>.csv      - every change ever logged in scope

Upload both to Claude along with whatever you want researched.
"""

import csv
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from common import haversine_km

BASE = Path(__file__).parent
DB_PATH = BASE / "menzies.db"
CONFIG_PATH = BASE / "config.json"
OUTPUT_DIR = BASE / "output"

TODAY = date.today().isoformat()


def get_latest_snapshot_date(conn):
    cur = conn.cursor()
    cur.execute("SELECT MAX(snapshot_date) FROM tenement_snapshots")
    row = cur.fetchone()
    return row[0] if row else None


def export_current(conn, lat, lon, radius_km, scope_label):
    latest = get_latest_snapshot_date(conn)
    if not latest:
        print("No data yet - run update.py at least once first.")
        return None, 0

    cur = conn.cursor()
    cur.execute(
        "SELECT tenement_id, status, holder, tenement_type, centroid_lat, centroid_lon "
        "FROM tenement_snapshots WHERE snapshot_date = ?",
        (latest,),
    )
    rows = cur.fetchall()

    out_rows = []
    for tid, status, holder, ttype, clat, clon in rows:
        if lat is not None:
            if clat is None or clon is None:
                continue
            if haversine_km(lat, lon, clat, clon) > radius_km:
                continue
        out_rows.append([tid, status, holder, ttype, clat, clon])

    path = OUTPUT_DIR / f"current_tenements_{scope_label}_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tenement_id", "status", "holder", "tenement_type", "centroid_lat", "centroid_lon"])
        w.writerows(out_rows)

    print(f"Current status data is from: {latest}")
    return path, len(out_rows)


def export_history(conn, lat, lon, radius_km, scope_label):
    cur = conn.cursor()
    cur.execute(
        "SELECT detected_date, tenement_id, change_type, old_status, new_status, "
        "holder, tenement_type, centroid_lat, centroid_lon FROM changes_log "
        "ORDER BY detected_date DESC"
    )
    rows = cur.fetchall()

    out_rows = []
    for r in rows:
        detected_date, tid, change_type, old_status, new_status, holder, ttype, clat, clon = r
        if lat is not None:
            if clat is None or clon is None:
                continue
            if haversine_km(lat, lon, clat, clon) > radius_km:
                continue
        out_rows.append(r)

    path = OUTPUT_DIR / f"change_history_{scope_label}_{TODAY}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["detected_date", "tenement_id", "change_type", "old_status", "new_status",
                     "holder", "tenement_type", "centroid_lat", "centroid_lon"])
        w.writerows(out_rows)

    return path, len(out_rows)


def export_polygons(conn, lat, lon, radius_km, scope_label, types=None):
    """Writes real tenement BOUNDARIES (not just centroid points) as a proper
    GeoJSON file. Works statewide or area-scoped.

    types: a set of tenement ID prefix letters to include, e.g. {"E","M","P"}
    for Exploration/Mining/Prospecting - the three that actually matter for
    prospecting decisions (access gates and prospectivity signal). Pass
    None for every type including the minor ones (Miscellaneous Licence,
    General Purpose Lease, etc.) - only sensible for an area-scoped export;
    statewide + None is a genuinely large file, so main() warns first.

    Filtering is done on the tenement ID's leading letter (e.g. "E 37/1318"
    -> "E"), not the free-text type name field, since the ID prefix is a
    fixed, reliable format we've confirmed against real DASC data - the
    type name field can vary in capitalisation/wording."""
    latest = get_latest_snapshot_date(conn)
    if not latest:
        return None, 0

    cur = conn.cursor()
    cur.execute(
        "SELECT tenement_id, status, holder, tenement_type, centroid_lat, "
        "centroid_lon, geometry_json FROM tenement_snapshots WHERE snapshot_date = ?",
        (latest,),
    )
    rows = cur.fetchall()

    features = []
    skipped_no_geometry = 0
    for tid, status, holder, ttype, clat, clon, geom_json in rows:
        if lat is not None:
            if clat is None or clon is None:
                continue
            if haversine_km(lat, lon, clat, clon) > radius_km:
                continue
        if types is not None:
            prefix = (tid or "").strip()[:1].upper()
            if prefix not in types:
                continue
        if not geom_json:
            # This tenement was captured before geometry_json existed (an
            # older snapshot from before this fix) - it'll have a real
            # boundary again after the next update.py run.
            skipped_no_geometry += 1
            continue
        features.append({
            "type": "Feature",
            "geometry": json.loads(geom_json),
            "properties": {
                "tenement_id": tid,
                "status": status,
                "holder": holder,
                "tenement_type": ttype,
            },
        })

    fc = {"type": "FeatureCollection", "features": features}
    suffix = "".join(sorted(types)).lower() if types else "all_types"
    path = OUTPUT_DIR / f"tenement_boundaries_{suffix}_{scope_label}_{TODAY}.geojson"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f)

    if skipped_no_geometry:
        print(f"Note: {skipped_no_geometry} tenement(s) in scope have no stored boundary yet "
              f"(captured before this feature existed) - run update.py again to pick up their "
              f"real geometry next time.")

    return path, len(features)


def main():
    args = sys.argv[1:]
    lat, lon, radius_km = None, None, None
    scope_label = "statewide"
    all_types = "--all-types" in args
    args = [a for a in args if a != "--all-types"]

    # Default polygon type set: Exploration, Mining, Prospecting - the
    # three that actually matter for access gates and prospectivity signal.
    poly_types = {"E", "M", "P"}
    for a in list(args):
        if a.startswith("--types="):
            poly_types = {c.strip().upper()[:1] for c in a.split("=", 1)[1].split(",") if c.strip()}
            args.remove(a)

    if len(args) >= 2:
        lat, lon = float(args[0]), float(args[1])
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        radius_km = float(args[2]) if len(args) >= 3 else config.get("default_export_radius_km", 25)
        scope_label = f"{lat}_{lon}_r{radius_km}km"
    elif len(args) == 1:
        print("Usage: python export_for_claude.py [lat lon [radius_km]] [--all-types] [--types=E,M,P]")
        print("       (no arguments = statewide export)")
        print("       (default polygon types: E, M, P - override with --types=... or use --all-types for everything)")
        sys.exit(1)

    if not DB_PATH.exists():
        print("No database found - run init_db.py first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    if lat is not None:
        print(f"Exporting scope: {radius_km}km around {lat}, {lon}")
    else:
        print("Exporting scope: entire state (no filtering)")

    current_path, current_n = export_current(conn, lat, lon, radius_km, scope_label)
    history_path, history_n = export_history(conn, lat, lon, radius_km, scope_label)

    print(f"\nCurrent tenements in scope: {current_n} -> {current_path}")
    print(f"Historical changes in scope: {history_n} -> {history_path}")

    types_filter = None if all_types else poly_types
    if lat is None and all_types:
        print("\nWARNING: statewide + --all-types can be a genuinely large file "
              "(tens of MB, ~30,000 tenement boundaries). Proceeding anyway, "
              "but if this fails or is too slow to upload, re-run without "
              "--all-types for the smaller E/M/P-only version.")

    poly_path, poly_n = export_polygons(conn, lat, lon, radius_km, scope_label, types=types_filter)
    label = "all tenement types" if all_types else "/".join(sorted(poly_types))
    scope_desc = "statewide" if lat is None else f"{radius_km}km radius"
    print(f"Real tenement boundaries ({label}, {scope_desc}): {poly_n} -> {poly_path}")

    conn.close()
    print("\nUpload the files to Claude along with what you want researched.")


if __name__ == "__main__":
    main()