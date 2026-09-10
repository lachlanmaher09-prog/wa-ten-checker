"""
common.py
Shared helpers. Not run directly.
"""

# Field names confirmed against the real DASC/SLIP Mining Tenements dataset
# (July 2026). Older guesses kept as fallbacks in case the schema changes
# again - if update.py prints a FIELD NAME MISMATCH warning, add the
# correct name here (check the printed list of available fields).
ID_FIELDS = ["fmt_tenid", "tenid", "TENEMENT_ID", "TENEMENT_NO", "TENEMENT", "TENNO", "TENEMENTID", "TITLE_NO"]
STATUS_FIELDS = ["tenstatus", "STATUS", "TENEMENT_STATUS", "TITLE_STATUS", "STAT_DESC"]
HOLDER_FIELDS = ["HOLDER_NAME", "HOLDER", "TITLE_HOLDER", "OWNER", "COMPANY"]
TYPE_FIELDS = ["type", "TENEMENT_TYPE", "TYPE", "TITLE_TYPE", "TENTYPE"]

# The real dataset spreads titleholders across holder1..holder9 (joint
# ventures / multiple titleholders per tenement) rather than one field.
HOLDER_NUMBERED_PREFIX = "holder"
HOLDER_NUMBERED_MAX = 9


def get_field(props, candidates):
    for c in candidates:
        if c in props and props[c] not in (None, ""):
            return str(props[c])
    return None


def get_holder(props):
    """Combine holder1..holder9 into one string for company-name matching
    and display. Falls back to a single-field guess if those aren't
    present (in case the schema differs from what we've confirmed)."""
    names = []
    for i in range(1, HOLDER_NUMBERED_MAX + 1):
        key = f"{HOLDER_NUMBERED_PREFIX}{i}"
        val = props.get(key)
        if val is not None and str(val).strip():
            names.append(str(val).strip())
    if names:
        return "; ".join(names)
    return get_field(props, HOLDER_FIELDS)


def rough_centroid(geometry):
    """Average every coordinate pair in the geometry. Not a true polygon
    centroid, but plenty accurate for a 'is this near this point' check."""
    coords = []

    def walk(node):
        if isinstance(node, (int, float)):
            return
        if len(node) == 2 and all(isinstance(x, (int, float)) for x in node):
            coords.append(node)
        else:
            for child in node:
                walk(child)

    walk(geometry.get("coordinates", []))
    if not coords:
        return None, None
    lon = sum(c[0] for c in coords) / len(coords)
    lat = sum(c[1] for c in coords) / len(coords)
    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))
