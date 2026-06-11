# garmin-uploader

Automatically uploads the latest [TrainerDay](https://trainerday.com/) indoor
cycling workout to [Garmin Connect](https://connect.garmin.com/).

## Motivation

TrainerDay can automatically export a `.tcx` file of your indoor cycling workout
to your Dropbox after you finish it.

However, the file doesn't get automatically uploaded to Garmin Connect, so you
have to do it yourself - manually. Not only that, when you upload the file,
Garmin defaults the activity type to "Cycling" and names it "Cycling", so you
then have to manually edit the activity type to "Virtual Cycling" and name it
something meaningful. The activity type is specifically important for me to
distinguish between indoor vs outdoor rides when viewing activities/totals on
Intervals.icu.

## How It Works

This script simply automates the manual process above. When you run it, it finds
the most recent `.tcx` file in your TrainerDay Dropbox folder, parses the
workout title from the filename, uploads it to Garmin Connect, and then edits
the activity to set the correct name and type.

Editing activity fields can only happen after the initial upload. The script
handles this by snapshotting your most recent activity before uploading and
using that to detect when the new activity appears after upload. A pre-existing
activity is never touched.

The script reads `.tcx` files from `TRAINERDAY_DIR` by default:

```
~/Library/CloudStorage/Dropbox/Apps/TrainerDay
```

TrainerDay names its files `<date> <time> - <workout title>.tcx`, for example:

```
2026-06-09 20-35-37 - 5x3 120%, 2x 102%.tcx
```

After the script finishes, `5x3 120%, 2x 102%` becomes the activity name.

On the very first run, you're prompted to log in to Garmin. The credentials are
cached at `TOKENSTORE` (defaults to `~/.garminconnect`) for future runs.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A Garmin Connect account
- TrainerDay configured to export `.tcx` files to Dropbox

## Setup

Install dependencies into a virtual environment:

```
uv sync
```

## Usage

Dry run: Log in to Garmin, then find the latest `.tcx` only (no upload or
edits).

```
uv run garmin_upload.py --dry-run
```

Same as above, but continue to perform the upload and edits:

```
uv run garmin_upload.py
```
