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
token is cached in ~/.garminconnect and reused afterwards.

TrainerDay TCX filenames look like "<date> <time> - <workout title>". The workout
title is used as the activity name.
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

TRAINERDAY_DIR = Path("~/Library/CloudStorage/Dropbox/Apps/TrainerDay").expanduser()
TOKENSTORE = Path("~/.garminconnect").expanduser()

# TrainerDay TCX file format: "<date> <time> - <workout title>.tcx", e.g.
# 2026-06-09 20-35-37 - 5x3 120%, 2x 102%.tcx
TRAINERDAY_TCX_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2} - (?P<title>.+)$"
)

# Desired activity type. Payload is from `client.get_activity_types()`
ACTIVITY_TYPE_DTO = {
    "typeId": 152,
    "typeKey": "virtual_ride",
    "parentTypeId": 2,
}
log = logging.getLogger("garmin_upload")


def setup_logging():
    """Timestamped logging to stderr at INFO level."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def find_latest_tcx_file(directory: Path) -> Path:
    files = [p for p in directory.glob("*.tcx") if p.is_file()]
    if not files:
        raise FileNotFoundError(f"No .tcx files found in: {directory}")
    return max(files, key=lambda p: p.stat().st_mtime)


def wait_for_activity_upload(
    client: Garmin,
    last_activity: dict,
    timeout: int = 30,
    poll_interval: int = 5,
) -> dict:
    """Return the just-uploaded activity, identified as the new most-recent
    activity once Garmin finishes indexing it.
    """
    last_activity_id = last_activity.get("activityId")
    deadline = time.monotonic() + timeout
    while True:
        new_activity = client.get_last_activity()
        new_activity_id = new_activity.get("activityId")

        # If the last activity changed, that means the upload was processed and it's now the new last activity
        if new_activity_id is not None and new_activity_id != last_activity_id:
            log.info(f"Found new uploaded activity: {new_activity_id}")
            return new_activity

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Waited {timeout}s but no new activity appeared after upload; giving up."
            )
        log.info(f"No new activity yet; polling again in {poll_interval}s...")
        time.sleep(poll_interval)


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
    log.info(f"Found latest tcx file: {tcx_file.name}")
    log.info(f"Full path: {tcx_file.resolve()}")

    # Parse the activity name
    activity_name = TRAINERDAY_TCX_REGEX.match(tcx_file.stem).group("title").strip()
    log.info(f"Parsed activity name: {activity_name}")

    # Return early if dry run
    if dry_run:
        log.warning("DRY RUN: No upload or edits will be performed.")
        return 0

    # Garmin login
    client = garmin_login()

    # Save the current last activity before upload so we can recognise the newly-created one
    # and never touch a pre-existing activity.
    last_activity = client.get_last_activity()

    # Upload the new activity
    result = client.import_activity(str(tcx_file))
    log.info(f"Garmin upload initiated. Result: {result}")

    # Wait for it to appear
    new_activity = wait_for_activity_upload(client, last_activity)
    new_activity_id = new_activity.get("activityId")
    # log.info(f"Uploaded activity before edits: {new_activity}")

    # Sleep to let Garmin activity processing to settle before editing
    time.sleep(3)

    # Edit it and finish
    activity_type = ACTIVITY_TYPE_DTO["typeKey"]
    log.info(f"Editing activity name to: {activity_name}")
    client.set_activity_name(new_activity_id, activity_name)
    log.info(f"Editing activity type to: {activity_type}")
    client.set_activity_type(new_activity_id, *ACTIVITY_TYPE_DTO.values())

    # Verify the edits stuck
    log.info("Verifying activity after edits...")
    verify_activity = client.get_activity(new_activity_id)
    # log.info(f"Verifying activity after edits: {verify_activity}")
    assert verify_activity.get("activityName") == activity_name
    assert verify_activity.get("activityTypeDTO").get("typeKey") == activity_type

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
