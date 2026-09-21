"""Download new Canvas files across all schools.

Keeps a record of what it has already fetched, so each run only pulls
files that appeared since last time. Safe to run repeatedly.

Run:  ./.venv/bin/python sync_files.py
"""

import json
import re
from pathlib import Path

import requests

from canvas import SCHOOLS, get, credentials

HERE = Path(__file__).parent
DOWNLOADS = HERE / "downloads"
STATE_FILE = HERE / "seen_files.json"


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def safe_name(text, limit=60):
    """Turn a course name into something usable as a folder name."""
    cleaned = re.sub(r"[^\w\s.-]", "", text).strip()
    return re.sub(r"\s+", " ", cleaned)[:limit] or "untitled"


def sync_course(school, course, seen):
    """Download any files in one course we haven't already got."""
    try:
        files = get(school, f"/courses/{course['id']}/files")
    except requests.HTTPError as e:
        if e.response.status_code in (401, 403, 404):
            # Instructors can disable the files tab; not an error worth stopping for.
            return 0
        raise

    folder = DOWNLOADS / school / safe_name(course.get("name", "untitled"))
    new_count = 0

    for f in files:
        key = f"{school}:{f['id']}"
        if key in seen:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        # This url is pre-signed and expires within minutes — fetch it now.
        data = requests.get(f["url"], timeout=60).content
        (folder / safe_name(f["display_name"], 120)).write_bytes(data)
        print(f"  + {f['display_name']} ({len(data) // 1024} KB)")
        seen.add(key)
        new_count += 1

    return new_count


def main():
    seen = load_seen()
    first_run = not seen
    total = 0

    for school in SCHOOLS:
        try:
            credentials(school)
            courses = get(school, "/courses", enrollment_state="active")
        except RuntimeError:
            continue
        except requests.HTTPError as e:
            print(f"[{school}] auth failed: {e.response.status_code}")
            continue

        print(f"[{school}] checking {len(courses)} courses")
        for course in courses:
            total += sync_course(school, course, seen)

    save_seen(seen)

    if first_run:
        print(f"\nFirst run: downloaded {total} files (baseline established).")
    else:
        print(f"\n{total} new file(s).")


if __name__ == "__main__":
    main()
