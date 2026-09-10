"""
update.py
Silently pulls the current WA tenement dataset and logs whatever changed
since the last successful pull into the permanent record - every tenement,
every company, statewide. No report, no popup, nothing to read. This is
meant to run in the background (Task Scheduler) so history keeps building
even across the weeks you're out of signal - it just diffs against whatever
the last successful pull was, however long ago that was.

When you actually want data, use export_for_claude.py instead.
"""

import io
import json
import sqlite3
import sys
import zipfile
from datetime import date
from pathlib import Path

import requests

from common import (
    ID_FIELDS, STATUS_FIELDS, HOLDER_FIELDS, TYPE_FIELDS,
    get_field, get_holder, rough_centroid,
)

BASE = Path(__file__).parent
DB_PATH = BASE / "menzies.db"
CONFIG_PATH = BASE / "config.json"
DATA_DIR = BASE / "data"

TODAY = date.today().isoformat()


TOKEN_URL = "https://sso.slip.wa.gov.au/as/token.oauth2"
# This is a published, public client identifier from WA's own documentation
# for exactly this use case (scripted access to open data) - it is not a
# secret and is the same for every user. See:
# https://toolkit.data.wa.gov.au/hc/en-gb/articles/115009696428
CLIENT_AUTH_HEADER = "Basic ZGlyZWN0LWRvd25sb2Fk"


def get_slip_token(username, password):
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "password", "username": username, "password": password},
        headers={
            "Authorization": CLIENT_AUTH_HEADER,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=60,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"SLIP login didn't return a token - response was: {resp.text[:300]}")
    return token


def download_snapshot(direct_download_url, username, password):
    token = get_slip_token(username, password)
    r = requests.get(
        direct_download_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
    )
    r.raise_for_status()

    content = r.content

    # SLIP downloads are zip archives, not raw GeoJSON files. Detect this
    # (zip files start with 'PK') rather than assuming based on the URL,
    # in case DMIRS ever serves it differently.
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            json_names = [n for n in z.namelist() if n.lower().endswith((".json", ".geojson"))]
            if not json_names:
                raise RuntimeError(
                    f"Downloaded zip didn't contain a .json/.geojson file. "
                    f"Contents were: {z.namelist()}"
                )
            with z.open(json_names[0]) as f:
                return json.loads(f.read())
    else:
        return json.loads(content)


def load_snapshot_into_db(conn, geojson, snapshot_date):
    cur = conn.cursor()
    features = geojson.get("features", [])
    if not features:
        print("WARNING: no features found - check dasc_download_url in config.json")
        return 0

    sample_props = features[0].get("properties", {})
    if not get_field(sample_props, ID_FIELDS):
        print("\n--- FIELD NAME MISMATCH ---")
        print("Available fields in this dataset:", list(sample_props.keys()))
        print("Update ID_FIELDS/STATUS_FIELDS/HOLDER_FIELDS/TYPE_FIELDS in common.py")
        print("---------------------------\n")

    count = 0
    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry")
        tid = get_field(props, ID_FIELDS)
        status = get_field(props, STATUS_FIELDS)
        holder = get_holder(props)
        ttype = get_field(props, TYPE_FIELDS)
        if not tid or not geom:
            continue
        clat, clon = rough_centroid(geom)
        cur.execute(
            """INSERT OR REPLACE INTO tenement_snapshots
               (snapshot_date, tenement_id, status, holder, tenement_type,
                centroid_lat, centroid_lon, raw_json, geometry_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_date, tid, status, holder, ttype, clat, clon,
             json.dumps(props), json.dumps(geom)),
        )
        count += 1
    conn.commit()
    return count


def get_previous_snapshot_date(conn, exclude_date):
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT snapshot_date FROM tenement_snapshots WHERE snapshot_date != ? ORDER BY snapshot_date DESC LIMIT 1",
        (exclude_date,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def diff_and_log(conn, prev_date, curr_date):
    cur = conn.cursor()

    def fetch(snap_date):
        cur.execute(
            "SELECT tenement_id, status, holder, tenement_type, centroid_lat, centroid_lon FROM tenement_snapshots WHERE snapshot_date = ?",
            (snap_date,),
        )
        return {row[0]: row[1:] for row in cur.fetchall()}

    prev = fetch(prev_date) if prev_date else {}
    curr = fetch(curr_date)

    count = 0
    for tid, (status, holder, ttype, clat, clon) in curr.items():
        old = prev.get(tid)
        change_type, old_status = None, None
        if old is None:
            change_type = "NEW"
        elif old[0] != status:
            change_type = "STATUS_CHANGE"
            old_status = old[0]
        if change_type:
            cur.execute(
                """INSERT INTO changes_log
                   (detected_date, tenement_id, change_type, old_status, new_status,
                    holder, tenement_type, centroid_lat, centroid_lon)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (curr_date, tid, change_type, old_status, status, holder, ttype, clat, clon),
            )
            count += 1

    for tid, (status, holder, ttype, clat, clon) in prev.items():
        if tid not in curr:
            cur.execute(
                """INSERT INTO changes_log
                   (detected_date, tenement_id, change_type, old_status, new_status,
                    holder, tenement_type, centroid_lat, centroid_lon)
                   VALUES (?, ?, 'REMOVED', ?, NULL, ?, ?, ?, ?)""",
                (curr_date, tid, status, holder, ttype, clat, clon),
            )
            count += 1

    conn.commit()
    return count


def prune_old_snapshots(conn, keep_dates):
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in keep_dates)
    cur.execute(
        f"DELETE FROM tenement_snapshots WHERE snapshot_date NOT IN ({placeholders})",
        keep_dates,
    )
    conn.commit()


def main():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    missing = [
        k for k in ("slip_username", "slip_password", "direct_download_url")
        if config.get(k, "").startswith("PASTE_")
    ]
    if missing:
        print(f"ERROR: set {', '.join(missing)} in config.json first (see README.md).")
        sys.exit(1)

    try:
        geojson = download_snapshot(
            config["direct_download_url"], config["slip_username"], config["slip_password"]
        )
    except Exception as e:
        # Silent-ish failure by design - if you're out of signal for weeks,
        # or your SLIP session/credentials need attention, this just
        # quietly does nothing until it works again.
        print(f"Update skipped - couldn't reach/download from SLIP ({e})")
        sys.exit(0)

    conn = sqlite3.connect(DB_PATH)
    n = load_snapshot_into_db(conn, geojson, TODAY)
    prev_date = get_previous_snapshot_date(conn, exclude_date=TODAY)
    changed = diff_and_log(conn, prev_date, TODAY)
    prune_old_snapshots(conn, [d for d in [TODAY, prev_date] if d])
    conn.close()

    print(f"Updated: {n} tenements loaded, {changed} changes logged "
          f"(compared against {prev_date or 'nothing - first run'}).")


if __name__ == "__main__":
    main()
