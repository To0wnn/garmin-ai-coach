#!/usr/bin/env python3
"""Stage 3 verification script — runs coach.py's real InfluxDB-backed build_metrics()
and build_metrics_sqlite.py's parallel SQLite-backed version, diffs the two dicts, and
reports mismatches. Run manually (not part of the app/cron/dashboard), once per day
during the parity-verification window per the migration plan. Deliberately verbose
output — this is a one-off diagnostic tool, not a monitored service."""

import json

import coach
import build_metrics_sqlite

# Fields expected to differ by design — additive Stage-6-recommended improvements
# already present in the SQLite path, not a parity bug. Anything else diverging is
# a real mismatch to investigate.
KNOWN_ADDITIVE_ONLY_IN_SQLITE: set[str] = set()  # none yet — those land in Stage 6


def _flatten(d, prefix=""):
    """Flattens a nested dict into {"a.b.c": value} for a readable diff."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def compare():
    influx_metrics = coach.build_metrics()
    sqlite_metrics = build_metrics_sqlite.build_metrics()

    flat_influx = _flatten(influx_metrics)
    flat_sqlite = _flatten(sqlite_metrics)

    all_keys = sorted(set(flat_influx) | set(flat_sqlite))
    mismatches = []
    matches = 0

    for key in all_keys:
        iv = flat_influx.get(key, "<missing>")
        sv = flat_sqlite.get(key, "<missing>")
        if key.startswith("today.activities_already_done_today") or key.startswith("recent_activities_14d"):
            continue  # list-shaped, compared separately below (order/count can differ trivially)
        if key.endswith("age_minutes"):
            continue  # inherently time-of-comparison-dependent, not a real mismatch signal
        if isinstance(iv, float) and isinstance(sv, float):
            if abs(iv - sv) > 0.15:
                mismatches.append((key, iv, sv))
            else:
                matches += 1
        elif iv != sv:
            mismatches.append((key, iv, sv))
        else:
            matches += 1

    activities_influx_count = len(influx_metrics.get("today", {}).get("activities_already_done_today", []))
    activities_sqlite_count = len(sqlite_metrics.get("today", {}).get("activities_already_done_today", []))
    recent_influx_count = len(influx_metrics.get("recent_activities_14d", []))
    recent_sqlite_count = len(sqlite_metrics.get("recent_activities_14d", []))

    print(f"=== Parity check: {coach.NOW_LOCAL.date().isoformat()} ===")
    print(f"Matching fields: {matches}/{len(all_keys) - len([k for k in all_keys if 'activities' in k or 'age_minutes' in k])}")
    print(f"activities_already_done_today count: influx={activities_influx_count} sqlite={activities_sqlite_count}")
    print(f"recent_activities_14d count: influx={recent_influx_count} sqlite={recent_sqlite_count}")
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH(ES):")
        for key, iv, sv in mismatches:
            print(f"  {key}: influx={iv!r}  sqlite={sv!r}")
    else:
        print("\nNo field-level mismatches.")
    return len(mismatches) == 0


if __name__ == "__main__":
    ok = compare()
    raise SystemExit(0 if ok else 1)
