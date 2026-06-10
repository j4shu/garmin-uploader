"""Upload the latest TrainerDay workout to Garmin Connect as Virtual Cycling.

Finds the most recent .tcx that TrainerDay exported into the Dropbox app folder,
uploads it to Garmin, then sets the activity name (the workout title from the
filename) and type (Virtual Cycling). Name and type can only be set after upload
(Garmin auto-names imports and TCX can't express a virtual sport), so the script
identifies the uploaded activity as the new most-recent activity to edit the
right one.

Usage:
    uv run garmin_upload.py --dry-run  # find + parse only; no login, no upload
    uv run garmin_upload.py            # find, upload + edit

Auth: the first run prompts for your Garmin email/password (+ MFA code); the
token is cached in ~/.garminconnect and reused afterwards. To skip the prompts,
set GARMIN_EMAIL / GARMIN_PASSWORD in the environment or a .env file.

TrainerDay TCX filenames look like "2026-06-09 20-35-37 - 5x3 120%, 2x 102%.tcx":
"<date> <time> - <workout title>"; the title is used as the activity name.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin

# --- Configuration -----------------------------------------------------------
TRAINERDAY_DIR = Path("~/Library/CloudStorage/Dropbox/Apps/TrainerDay").expanduser()
TOKENSTORE = Path("~/.garminconnect").expanduser()

# TrainerDay TCX export: "<date> <time> - <workout title>.tcx", e.g.
# "2026-06-09 20-35-37 - 5x3 120%, 2x 102%.tcx" (optional "Downloaded " prefix).
TRAINERDAY_TCX_FORMAT_REGEX = re.compile(
    r"^(?:Downloaded )?\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2} - (?P<title>.+)$"
)

# Desired activity type
# Note: payload is from `client.get_activity_types()`
ACTIVITY_TYPE_DTO = {
    "typeId": 152,
    "typeKey": "virtual_ride",
    "parentTypeId": 2,
}

# --- Logging ------------------------------------------------------------------
log = logging.getLogger("garmin_upload")


def setup_logging():
    """Timestamped logging that mirrors the prior print/stderr split: progress
    (INFO) goes to stdout, warnings and errors to stderr."""
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(logging.INFO)
    stdout.addFilter(lambda record: record.levelno < logging.WARNING)
    stdout.setFormatter(fmt)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    stderr.setFormatter(fmt)

    log.addHandler(stdout)
    log.addHandler(stderr)


def find_latest_tcx_file(directory: Path) -> Path | None:
    """Most recently modified .tcx under `directory` (recursive), or None."""
    files = [p for p in directory.rglob("*.tcx") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def parse_activity_name(path: Path) -> str:
    """Return the workout title from a TrainerDay TCX filename."""
    m = TRAINERDAY_TCX_FORMAT_REGEX.match(path.stem)
    return m.group("title").strip()


def wait_for_activity_upload(
    client: Garmin,
    current_last_activity: dict | None,
    timeout: int = 90,
    poll_interval: int = 5,
) -> dict:
    """Return the just-uploaded activity, identified as the new most-recent
    activity once Garmin finishes indexing it.

    Detects it by the most-recent activity changing from `current_last_activity`
    (the last activity before the upload). Polls because Garmin indexes the
    upload asynchronously, and raises TimeoutError if nothing new appears.
    """
    current_last_activity_id = current_last_activity.get("activityId")
    waited = 0
    while True:
        new_last_activity = client.get_last_activity()
        new_last_activity_id = new_last_activity.get("activityId")

        if new_last_activity_id is not None:
            # If the last activity changed, that means the upload was processed and is now the new last activity
            if new_last_activity_id != current_last_activity_id:
                log.info(f"Found new uploaded activity: {new_last_activity_id}")
                return new_last_activity

        if waited >= timeout:
            raise TimeoutError(
                f"Waited {waited}s but no new activity appeared after upload; giving up."
            )
        log.info(
            f"Waiting for a new uploaded activity to appear. Waiting {poll_interval}s..."
        )
        time.sleep(poll_interval)
        waited += poll_interval


def garmin_login() -> Garmin:
    """Restore a cached Garmin session, or log in fresh (with MFA) and cache it."""
    if TOKENSTORE.exists():
        try:
            client = Garmin()
            client.login(str(TOKENSTORE))
            return client
        except Exception as exc:
            log.warning(f"cached session unusable ({exc}); logging in fresh")
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA / 2FA code: ").strip(),
    )
    client.login(str(TOKENSTORE))  # caches tokens to TOKENSTORE
    return client


def main():
    setup_logging()
    load_dotenv()
    dry_run = "--dry-run" in sys.argv

    # Find the latest TCX file exported by TrainerDay
    tcx_file = find_latest_tcx_file(TRAINERDAY_DIR)
    if tcx_file is None:
        log.warning(f"No .tcx files found under: {TRAINERDAY_DIR}")
        return 0
    log.info(f"Found latest tcx file: {tcx_file.name}")
    log.info(f"Full path: {tcx_file.resolve()}")

    # Parse the activity name
    activity_name = parse_activity_name(tcx_file)
    log.info(f"Parsed activity name: {activity_name}")

    # Return early if dry run
    if dry_run:
        log.warning("DRY RUN: No upload or edits will be performed.")
        return 0

    # Garmin login
    client = garmin_login()

    # Save the current last activity before upload so we can recognise the newly-created one
    # and never touch a pre-existing activity.
    current_last_activity = client.get_last_activity()

    # Upload the new activity
    result = client.import_activity(str(tcx_file))
    log.info(f"Garmin upload initiated. Result: {result}")

    # Wait for it to appear
    uploaded_activity = wait_for_activity_upload(client, current_last_activity)
    uploaded_activity_id = uploaded_activity.get("activityId")
    # log.info(f"Uploaded activity before edits: {uploaded_activity}")

    # Sleep to let Garmin activity processing to settle before editing
    time.sleep(3)

    # Edit it and finish
    activity_type = ACTIVITY_TYPE_DTO["typeKey"]
    log.info(f"Editing activity name to: {activity_name}")
    client.set_activity_name(uploaded_activity_id, activity_name)
    log.info(f"Editing activity type to: {activity_type}")
    client.set_activity_type(uploaded_activity_id, *ACTIVITY_TYPE_DTO.values())

    # Verify the edits stuck
    log.info("Verifying activity after edits...")
    verify_activity = client.get_activity(uploaded_activity_id)
    # log.info(f"Verifying activity after edits: {verify_activity}")
    assert verify_activity.get("activityName") == activity_name
    assert verify_activity.get("activityTypeDTO").get("typeKey") == activity_type

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
