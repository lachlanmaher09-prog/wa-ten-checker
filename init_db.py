"""
init_db.py
Run this ONCE to create the database. Safe to re-run anytime - it only
creates tables if they don't already exist, never deletes data.
"""

import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "menzies.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenement_snapshots (
            snapshot_date TEXT,
            tenement_id TEXT,
            status TEXT,
            holder TEXT,
            tenement_type TEXT,
            centroid_lat REAL,
            centroid_lon REAL,
            raw_json TEXT,
            PRIMARY KEY (snapshot_date, tenement_id)
        )
    """)

    # Migration: add geometry_json to existing databases without losing any
    # accumulated history. Older rows will simply have NULL geometry until
    # the next update.py run recaptures them with real boundaries.
    cur.execute("PRAGMA table_info(tenement_snapshots)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "geometry_json" not in existing_cols:
        cur.execute("ALTER TABLE tenement_snapshots ADD COLUMN geometry_json TEXT")
        print("Migrated: added geometry_json column (existing rows unaffected).")

    # The permanent record - every change, every company, statewide, forever.
    # Never pruned. This is what you're actually querying when you ask for
    # an export.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS changes_log (
            detected_date TEXT,
            tenement_id TEXT,
            change_type TEXT,
            old_status TEXT,
            new_status TEXT,
            holder TEXT,
            tenement_type TEXT,
            centroid_lat REAL,
            centroid_lon REAL
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database ready at {DB_PATH}")


if __name__ == "__main__":
    main()
