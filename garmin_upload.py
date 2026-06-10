"""Upload the latest TrainerDay workout to Garmin Connect as Virtual Cycling.

Finds the most recent .tcx that TrainerDay exported into the Dropbox app folder,
uploads it to Garmin, then sets the activity name (the workout title from the
filename) and type (Virtual Cycling). Name and type can only be set after upload
(Garmin auto-names imports and TCX can't express a virtual sport), so the script
identifies the uploaded activity by its exact start time to edit the right one.

Usage:
    uv run garmin_upload.py            # find, upload + edit
    uv run garmin_upload.py --dry-run  # find + parse only; no login, no upload

Auth: the first run prompts for your Garmin email/password (+ MFA code); the
token is cached in ~/.garminconnect and reused afterwards. To skip the prompts,
set GARMIN_EMAIL / GARMIN_PASSWORD in the environment or a .env file.

TrainerDay TCX filenames look like "2026-06-09 20-35-37 - 5x3 120%, 2x 102%.tcx":
"<date> <time> - <workout title>"; the title is used as the activity name.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

# --- Configuration -----------------------------------------------------------
TRAINERDAY_DIR = Path("~/Library/CloudStorage/Dropbox/Apps/TrainerDay").expanduser()
TOKENSTORE = Path("~/.garminconnect").expanduser()

# Used as the activity name only if the title can't be parsed from the filename.
DEFAULT_NAME = "Virtual Cycling"

# Garmin ingests an upload asynchronously and derives the sport from the file
# plus an auto-name. If we edit too early those edits get overwritten, so wait
# this long before editing, then verify against Garmin and retry.
SETTLE_SECONDS = 8

# Garmin type key(s) for "Virtual Cycling". The whole point is to convert the
# ride to virtual, so only *virtual* keys belong here — never fall back to a
# non-virtual type. Garmin labels "virtual_ride" as "Virtual Cycling" (the key
# Zwift indoor rides sync as); "virtual_cycling" is accepted as a synonym in
# case your catalog uses that spelling. First one present in your catalog wins.
TYPE_CANDIDATES = ("virtual_ride", "virtual_cycling")

# TrainerDay TCX export: "<date> <time> - <workout title>.tcx", e.g.
# "2026-06-09 20-35-37 - 5x3 120%, 2x 102%.tcx" (optional "Downloaded " prefix).
TRAINERDAY_TCX = re.compile(
    r"^(?:Downloaded )?\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2} - (?P<title>.+)$"
)


def find_latest_tcx(directory: Path) -> Path | None:
    """Most recently modified .tcx under `directory` (recursive), or None."""
    if not directory.exists():
        return None
    files = [p for p in directory.rglob("*.tcx") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def parse_trainerday_tcx(path: Path) -> str | None:
    """Return the workout title from a TrainerDay TCX filename, or None if the
    name doesn't match the TrainerDay pattern."""
    if path.suffix.lower() != ".tcx":
        return None
    m = TRAINERDAY_TCX.match(path.stem)
    if not m:
        return None
    return m.group("title").strip() or DEFAULT_NAME


def extract_activity_id(import_result) -> str | None:
    """Pull the new activity id out of an import_activity() result."""
    if not isinstance(import_result, dict):
        return None
    detail = import_result.get("detailedImportResult", import_result)
    if not isinstance(detail, dict):
        return None
    for entry in detail.get("successes") or []:
        if entry.get("internalId") is not None:
            return str(entry["internalId"])
    return None


def read_tcx_start_time(path: Path) -> datetime | None:
    """Read the activity start time (naive UTC) from the TCX, or None.

    The TCX <Id> element is the activity start in ISO-8601 UTC. This is the
    precise key we match Garmin activities against, so we never edit the wrong
    activity.
    """
    try:
        text = path.read_text(errors="ignore")
    except OSError as exc:
        print(f"  ! could not read TCX ({exc})")
        return None
    m = re.search(r"<Id>([^<]+)</Id>", text)
    if not m:
        return None
    stamp = m.group(1).strip().replace("Z", "")  # trailing Z just marks UTC
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(stamp, fmt)
        except ValueError:
            continue
    print(f"  ! could not parse TCX start time {stamp!r}")
    return None


def recent_activity_ids(client) -> set[str]:
    """IDs of recent activities currently on Garmin (for new-activity detection)."""
    try:
        acts = client.get_activities(0, 20)
    except Exception as exc:  # noqa: BLE001
        print(f"    (could not list recent activities: {exc})")
        return set()
    items = acts.get("activityList") if isinstance(acts, dict) else acts
    return {
        str(a["activityId"]) for a in (items or []) if a.get("activityId") is not None
    }


def _matches_start(
    activity: dict, start: datetime | None, tolerance_s: int = 90
) -> bool:
    if start is None:
        return False
    stamp = activity.get("startTimeGMT")
    if not stamp:
        return False
    try:
        adt = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return abs((adt - start).total_seconds()) <= tolerance_s


def wait_for_activity(
    client,
    start: datetime | None,
    before_ids: set[str],
    allow_new: bool,
    timeout_s: int = 90,
    interval_s: int = 5,
) -> str | None:
    """Return the id of the just-uploaded activity, or None if it can't be
    confidently identified (in which case the caller must NOT edit anything).

    Identifies it by exact start-time match (works for fresh and duplicate) or,
    for a fresh upload, by an id that wasn't present before (`allow_new`).
    Polls because Garmin indexes the upload asynchronously.
    """
    waited = 0
    while True:
        try:
            acts = client.get_activities(0, 20)
        except Exception as exc:  # noqa: BLE001
            print(f"    (could not list recent activities: {exc})")
            acts = None
        items = (acts.get("activityList") if isinstance(acts, dict) else acts) or []

        for a in items:  # precise: exact start-time match
            if _matches_start(a, start) and a.get("activityId") is not None:
                return str(a["activityId"])
        if allow_new:  # fresh upload: an id that wasn't there before
            for a in items:
                aid = a.get("activityId")
                if aid is not None and str(aid) not in before_ids:
                    return str(aid)

        if waited >= timeout_s:
            return None
        print(f"    waiting for the uploaded activity to appear... ({waited}s)")
        time.sleep(interval_s)
        waited += interval_s


def resolve_type(client) -> tuple[int, str, int] | None:
    """Resolve a virtual-cycling (type_id, type_key, parent_type_id) from the
    live catalog, or None if none of the candidates exist."""
    try:
        catalog = client.get_activity_types()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not fetch activity types ({exc}); leaving type unchanged")
        return None
    index = {t["typeKey"]: t for t in (catalog or []) if t.get("typeKey")}
    for key in TYPE_CANDIDATES:
        t = index.get(key)
        if t:
            return int(t["typeId"]), key, int(t["parentTypeId"])
    return None


def _print_relevant_types(client) -> None:
    """Diagnostic: list the cycling/virtual type keys actually in the catalog."""
    try:
        catalog = client.get_activity_types()
    except Exception:  # noqa: BLE001
        return
    keys = sorted(t.get("typeKey", "") for t in (catalog or []))
    relevant = [
        k for k in keys if any(w in k for w in ("cycl", "ride", "virtual", "bik"))
    ]
    print(
        f"    cycling-related type keys in your catalog: {', '.join(relevant) or '(none)'}"
    )


def get_activity_summary(client, activity_id) -> tuple[str | None, str | None]:
    """Read back (activityName, activityType typeKey) currently on Garmin."""
    try:
        act = client.get_activity(activity_id)
    except Exception as exc:  # noqa: BLE001
        print(f"    (could not read activity {activity_id} back: {exc})")
        return None, None
    if not isinstance(act, dict):
        return None, None
    type_key = (act.get("activityTypeDTO") or {}).get("typeKey")
    return act.get("activityName"), type_key


def apply_edits(client, activity_id, name: str, settle: bool = True) -> bool:
    """Set name + type, then verify they actually stuck, retrying a few times.

    Garmin can overwrite edits made before it finishes processing the upload, so
    we wait first, then apply, then read the activity back and re-apply until it
    matches (or we give up loudly). `settle=False` skips the initial wait when
    the activity is already processed (e.g. fixing an existing one).
    """
    resolved = resolve_type(client)
    if resolved is None:
        print(
            "  ! no Virtual Cycling type in your catalog (tried "
            f"{', '.join(TYPE_CANDIDATES)}); will set the name only.",
            file=sys.stderr,
        )
        _print_relevant_types(client)
    target_type = resolved[1] if resolved else None

    if settle:
        print(f"  waiting {SETTLE_SECONDS}s for Garmin to finish processing...")
        time.sleep(SETTLE_SECONDS)

    for attempt in range(1, 4):
        client.set_activity_name(activity_id, name)
        if resolved:
            client.set_activity_type(activity_id, *resolved)

        time.sleep(3)  # let the edit register
        cur_name, cur_type = get_activity_summary(client, activity_id)
        print(
            f"  attempt {attempt}: Garmin now has name={cur_name!r}, type={cur_type!r}"
        )

        name_ok = cur_name == name
        type_ok = target_type is None or cur_type == target_type
        if name_ok and type_ok:
            print("  verified on Garmin.")
            return True
        time.sleep(4)  # wait for any late processing, then re-apply

    print("  ! edits did not stick after retries.", file=sys.stderr)
    return False


def login() -> Garmin:
    """Restore a cached Garmin session, or log in fresh (with MFA) and cache it."""
    if TOKENSTORE.exists():
        try:
            client = Garmin()
            client.login(str(TOKENSTORE))
            return client
        except Exception as exc:  # noqa: BLE001
            print(f"  cached session unusable ({exc}); logging in fresh")
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA / 2FA code: ").strip(),
    )
    client.login(str(TOKENSTORE))  # caches tokens to TOKENSTORE
    return client


def main() -> int:
    load_dotenv()
    dry_run = "--dry-run" in sys.argv

    latest = find_latest_tcx(TRAINERDAY_DIR)
    if latest is None:
        print(f"No .tcx files found under {TRAINERDAY_DIR}")
        return 0
    print(f"Latest file: {latest.name}")

    name = parse_trainerday_tcx(latest)
    if name is None:
        print("  not a TrainerDay activity (filename pattern didn't match); skipping.")
        return 0
    start = read_tcx_start_time(latest)
    print(f"  parsed -> name={name!r}, start(UTC)={start}, type=virtual cycling")

    if dry_run:
        print("  (dry run: not logging in or uploading)")
        return 0

    try:
        client = login()
    except GarminConnectTooManyRequestsError:
        print("Garmin rate-limited the login; wait a few minutes.", file=sys.stderr)
        return 2
    except GarminConnectAuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2

    # Snapshot existing activities so we can recognise the newly-created one and
    # never touch a pre-existing activity.
    before_ids = recent_activity_ids(client)

    duplicate = False
    id_from_result = None
    try:
        result = client.import_activity(str(latest))
        detail = (
            result.get("detailedImportResult", {}) if isinstance(result, dict) else {}
        )
        print(
            f"  upload result: successes={detail.get('successes')} failures={detail.get('failures')}"
        )
        id_from_result = extract_activity_id(result)  # precise id when present
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "duplicate" in msg or "already exist" in msg:
            print("  already on Garmin (duplicate) -- locating it by start time...")
            duplicate = True
        else:
            print(f"Upload failed: {exc}", file=sys.stderr)
            return 1

    activity_id = id_from_result
    if activity_id is None:
        if start is None and duplicate:
            # No id, can't match by time, nothing new to detect: don't guess.
            print(
                "Cannot identify the existing activity (no TCX start time); "
                "refusing to edit to avoid touching the wrong one.",
                file=sys.stderr,
            )
            return 1
        activity_id = wait_for_activity(
            client, start, before_ids, allow_new=not duplicate
        )

    if activity_id is None:
        print(
            "Could not confidently identify the uploaded activity; edited nothing. "
            "Check Garmin Connect and re-run.",
            file=sys.stderr,
        )
        return 1
    print(f"  editing activity {activity_id}")

    # Only settle when Garmin handed us an id synchronously (the activity may
    # still be processing). If we found it by polling or it's a duplicate, it's
    # already indexed, so skip the wait — the verify-and-retry loop covers edge
    # cases either way.
    ok = apply_edits(client, activity_id, name, settle=id_from_result is not None)
    print("Done." if ok else "Finished with problems (see above).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
