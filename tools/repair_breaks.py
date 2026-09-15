#!/usr/bin/env python3
"""Clear line breaks that were only ever tile crossings.

    python tools/repair_breaks.py --dry-run
    python tools/repair_breaks.py

The recorder used to treat any change of map ID as a teleport. In the
overworld the map ID changes every time you cross a 256 m tile boundary, which
is walking, not warping -- so the line was cut at every seam. At a
quarter-second poll those holes are a couple of metres wide and invisible; in
a route imported from a tool that sampled every five seconds they are 50 m
wide and obvious.

The recorder no longer does this. Databases written before that keep the stale
flags, and this clears them. It only touches a break where both sides sit on
the world plane, in the same area, within 200 m of each other -- a distance no
teleport covers and no sampling gap invents. Nothing is deleted: the samples
are untouched and only the break flag changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.store import Store, kept_a_copy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 200 m: above Torrent's sprint over any plausible sampling interval, and far
# below the distance a grace warp covers.
NEAR_M2 = 200.0 * 200.0

FIND = """
SELECT a.id, a.session_id,
       sqrt((a.wx - b.wx) * (a.wx - b.wx) + (a.wz - b.wz) * (a.wz - b.wz)) d
FROM samples a JOIN samples b ON b.id = a.id - 1
WHERE a.break_before = 1   -- only "the map changed"; 2 is speed, 3 a load screen
  AND a.wx IS NOT NULL AND b.wx IS NOT NULL
  AND a.session_id = b.session_id
  AND a.map_id / 16777216 = b.map_id / 16777216
  AND ((a.wx - b.wx) * (a.wx - b.wx) + (a.wz - b.wz) * (a.wz - b.wz)) < ?
ORDER BY a.session_id, a.id
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "routes.db"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    a = ap.parse_args()

    store = Store(a.db)
    rows = store.db.execute(FIND, (NEAR_M2,)).fetchall()
    if not rows:
        print("no stale breaks: every line break in this database is a real one.")
        store.close()
        return 0

    by_session: dict[int, list[float]] = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r["d"])

    print(f"{len(rows)} break(s) that only mark a tile crossing:\n")
    print("  session   breaks   widest hole")
    for sid, ds in sorted(by_session.items()):
        print(f"  {sid:7d} {len(ds):8d} {max(ds):9.1f} m")

    if a.dry_run:
        print("\ndry run: nothing written.")
        store.close()
        return 0

    kept_a_copy(store, "breaks")
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        store.db.execute(
            f"UPDATE samples SET break_before = 0 WHERE id IN ({marks})", chunk
        )
    store.commit()
    print(f"\ncleared {len(ids)} break(s). The line joins up across those seams "
          f"now; reload the viewer to see it.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
