"""Mechanical building blocks for the Canvas reconciliation agent.

Everything in here is deliberately dumb: it fetches, converts, writes, and
reports. No function decides what is urgent or what to do next — that
judgment lives in the model (see canvas_agent.py).

Standalone checks (checkpoints 1 and 2):
    ./.venv/bin/python agent_tools.py assignments        # every MIT assignment
    ./.venv/bin/python agent_tools.py files 38520        # file tree for a course
    ./.venv/bin/python agent_tools.py event 485352       # ONE real Calendar.app event
    ./.venv/bin/python agent_tools.py calendar           # read back the Canvas calendar
"""

import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from canvas import credentials, get

SCHOOL = "mit"                       # HBS is a later flip; nothing here is HBS-aware yet.
HERE = Path(__file__).parent

# Where the mirror lives: Nicolas's own "Semester 1" folder, one subfolder per course.
# A course that is not listed here is skipped on purpose (the ISO orientation eCourse).
MIRROR_ROOT = Path.home() / "Desktop" / "Semester 1"
COURSE_FOLDERS = {
    "15.071_FA26": "ai-edge",
    "15.572_FA26": "Analytics Lab",
    "15.THG_FA26": "Thesis",
    "MAS.665": "AI Studio",
}
# Canvas subfolders that Nicolas keeps somewhere else. Key = first path segment after
# "course files/", value = where it goes ("" means the course root).
FOLDER_ALIASES = {
    "15.071_FA26": {"General": ""},
}
LEDGER_FILE = HERE / "ledger.json"   # this agent's own ledger; seen_files.json belongs to sync_files.py
CALENDAR_NAME = "Canvas"             # dedicated calendar so nothing else in Calendar.app is at risk
EASTERN = ZoneInfo("America/New_York")

# The runner flips this on. When True, nothing is downloaded and Calendar.app is never written.
DRY_RUN = False

# Assignment submission states that mean "I don't need an alarm for this".
SUBMITTED_STATES = {"submitted", "graded", "pending_review"}

# Courses whose homework is handed in outside Canvas, so Canvas always says "unsubmitted".
# For these the submission state is ignored: every future due date gets a reminder,
# past due dates get nothing, and nothing is ever reported as late.
EXTERNAL_SUBMISSION_COURSES = {"15.071_FA26"}


# ---------------------------------------------------------------- time helpers

def parse_utc(iso_text):
    """Canvas gives '2026-09-24T03:59:59Z'. Return an aware UTC datetime."""
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00"))


def to_eastern(dt):
    return dt.astimezone(EASTERN)


def eastern_text(dt):
    """Human-readable Eastern time, e.g. 'Wed 2026-09-23 23:59 EDT'."""
    return to_eastern(dt).strftime("%a %Y-%m-%d %H:%M %Z")


def now_utc():
    return datetime.now(timezone.utc)


def compute_alarm(due_utc, now=None):
    """Where the day-of alarm belongs for one due time. Returns (alarm_utc, rule).

    Rules, in order:
      1. 8:00am Eastern on the due date (the Eastern date, not the UTC date).
      2. If that is within 2 hours of the deadline, or after it: 8:00pm Eastern the day before.
      3. If the chosen alarm has already passed: 15 minutes from now, so it actually fires.
    """
    now = now or now_utc()
    due_et = to_eastern(due_utc)
    alarm = due_et.replace(hour=8, minute=0, second=0, microsecond=0)
    rule = "8am due day"
    if alarm >= due_et - timedelta(hours=2):
        alarm = (due_et - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
        rule = "8pm day before (8am was too close to deadline)"
    if alarm <= now:
        alarm = now + timedelta(minutes=15)
        rule = "15 min from now (planned alarm already passed)"
    return alarm.astimezone(timezone.utc), rule


# ---------------------------------------------------------------- Canvas reads

def active_courses():
    return get(SCHOOL, "/courses", enrollment_state="active")


def course_dir(course):
    """The course's folder inside Semester 1, or None if the course is not mirrored."""
    name = COURSE_FOLDERS.get(course["course_code"])
    return MIRROR_ROOT / name if name else None


_LECTURE_ZIP = re.compile(r"^15\.071_Lecture(\d+)_(?:code|demo)\.zip$")


def mirror_relpath(course_code, folder_full_name, file_name):
    """Where one Canvas file belongs, relative to its course folder.

    Canvas paths start with 'course files/'; that prefix is dropped. Then the
    per-course aliases apply, and ai-edge lecture zips go under 'Lecture N/'
    because that is how the folder is already organised.
    """
    rel = folder_full_name
    if rel == "course files":
        rel = ""
    elif rel.startswith("course files/"):
        rel = rel[len("course files/"):]

    head, _, tail = rel.partition("/")
    aliases = FOLDER_ALIASES.get(course_code, {})
    if head in aliases:
        rel = "/".join(p for p in (aliases[head], tail) if p)

    m = _LECTURE_ZIP.match(file_name)
    if course_code == "15.071_FA26" and m and rel in ("Code", "Demo"):
        rel = f"Lecture {m.group(1)}"

    return Path(rel) / _safe_segment(file_name) if rel else Path(_safe_segment(file_name))


def canvas_copy_path(path):
    """Sibling name used when Nicolas already has a different file at the same path."""
    return path.with_name(f"{path.stem} (canvas){path.suffix}")


def is_on_disk(path, size):
    """True if this Canvas file is already present in an acceptable form.

    Acceptable: the exact path with the same size; an unzipped folder in place of a
    .zip; or a '(canvas)' sibling saved earlier because the original path was taken.
    """
    if path.exists() and path.stat().st_size == size:
        return True
    if path.suffix == ".zip" and path.with_suffix("").is_dir():
        return True
    alt = canvas_copy_path(path)
    return alt.exists() and alt.stat().st_size == size


def is_submitted(assignment):
    sub = assignment.get("submission") or {}
    return bool(sub.get("submitted_at")) or sub.get("workflow_state") in SUBMITTED_STATES


def list_assignments():
    """Every active MIT course and every assignment in it, with due date and submission state.

    Courses are listed separately so a course with no assignments (e.g. a thesis
    course that is only files) still gets its files mirrored.
    """
    now = now_utc()
    courses, out = [], []
    for course in active_courses():
        courses.append({"course_id": course["id"], "course_code": course["course_code"], "name": course["name"]})
        items = get(SCHOOL, f"/courses/{course['id']}/assignments",
                    include=["submission"], order_by="due_at")
        tracked = course["course_code"] not in EXTERNAL_SUBMISSION_COURSES
        for a in items:
            row = {
                "assignment_id": a["id"],
                "course_id": course["id"],
                "course_code": course["course_code"],
                "title": a["name"],
                "due_at": a.get("due_at"),          # UTC as Canvas sent it, or None
                "due_eastern": None,
                "submission_tracked": tracked,
                "submitted": is_submitted(a) if tracked else None,
                "submission_state": (a.get("submission") or {}).get("workflow_state") if tracked
                                    else "not tracked in Canvas (handed in elsewhere)",
                "html_url": a.get("html_url"),
                "alarm_plan": None,
            }
            if a.get("due_at"):
                due = parse_utc(a["due_at"])
                row["due_eastern"] = eastern_text(due)
                if tracked or due > now:
                    alarm, rule = compute_alarm(due, now)
                    row["alarm_plan"] = {"alarm_at_utc": alarm.isoformat(), "alarm_eastern": eastern_text(alarm), "rule": rule}
                else:
                    row["alarm_plan"] = {"rule": "past due and submission not tracked in Canvas: no reminder"}
            out.append(row)
    return {"courses": courses, "assignments": out}


def _safe_segment(text):
    """A file/folder name segment must not contain '/'. Nothing else is changed."""
    return text.replace("/", "-").strip() or "untitled"


def _file_record(course, file_json, folder_path, source):
    path = course_dir(course) / mirror_relpath(course["course_code"], folder_path, file_json["display_name"])
    size = file_json.get("size")
    record = {
        "file_id": file_json["id"],
        "name": file_json["display_name"],
        "path": str(path.relative_to(MIRROR_ROOT)),   # what download_file expects back
        "size": size,
        "on_disk": is_on_disk(path, size),
        "source": source,
    }
    if file_json.get("locked_for_user") and not record["on_disk"]:
        # Canvas will not hand out a download URL for this; nothing anyone can do here.
        record["on_disk"] = None
        record["locked"] = "locked on Canvas, cannot be downloaded"
    return record


def file_info(file_id):
    """GET /files/:id — a fresh pre-signed download url plus size and name."""
    url, token = credentials(SCHOOL)
    r = requests.get(f"{url}/api/v1/files/{file_id}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    return r.json()


def list_files(course_id):
    """The full file tree of one course, with the on-disk path each file must live at.

    Folder paths come from /folders (full_name, e.g. 'course files/HW/HW2') and are
    reproduced verbatim under downloads/<course_code>/. Files that are only linked from
    an assignment description (not in the tree) go under _assignments/<assignment name>/.
    """
    course = next(c for c in active_courses() if c["id"] == int(course_id))
    if course_dir(course) is None:
        return {"course_id": course["id"], "course_code": course["course_code"], "files": [], "missing": 0,
                "errors": [], "note": "not mirrored on purpose (no folder in COURSE_FOLDERS)"}
    files, errors = [], []
    tree_ids = set()

    try:
        folders = {f["id"]: f["full_name"] for f in get(SCHOOL, f"/courses/{course_id}/folders")}
        for f in get(SCHOOL, f"/courses/{course_id}/files"):
            tree_ids.add(f["id"])
            files.append(_file_record(course, f, folders.get(f["folder_id"], "course files"), "tree"))
    except requests.HTTPError as e:
        # Instructors can hide the Files tab; description attachments may still be reachable.
        errors.append(f"files tab not accessible (HTTP {e.response.status_code})")

    for a in get(SCHOOL, f"/courses/{course_id}/assignments"):
        ids = sorted({int(i) for i in re.findall(r"/files/(\d+)", a.get("description") or "")})
        for fid in ids:
            if fid in tree_ids:
                continue  # already mirrored in its real folder; don't duplicate it
            try:
                info = file_info(fid)
            except requests.HTTPError as e:
                errors.append(f"assignment '{a['name']}' file {fid}: HTTP {e.response.status_code}")
                continue
            files.append(_file_record(course, info, f"course files/_assignments/{_safe_segment(a['name'])}", "assignment"))

    return {"course_id": course["id"], "course_code": course["course_code"],
            "files": files, "missing": sum(1 for f in files if f["on_disk"] is False),
            "locked": [f["path"] for f in files if f.get("locked")], "errors": errors}


def download_file(file_id, dest_path):
    """Fetch one Canvas file to dest_path (relative to Semester 1).

    Never overwrites: if a different file already sits at that path, the Canvas
    version is saved beside it as '<name> (canvas).<ext>'. Zips are unpacked into a
    folder of the same name and the zip itself removed, matching the existing layout.
    """
    dest = (MIRROR_ROOT / dest_path).resolve()
    if MIRROR_ROOT.resolve() not in dest.parents:
        return {"status": "refused: path is outside the Semester 1 folder", "path": dest_path}
    info = file_info(file_id)
    size = info.get("size")
    if is_on_disk(dest, size):
        return {"status": "already on disk", "path": dest_path, "size": size}
    if info.get("locked_for_user") or not info.get("url"):
        return {"status": "locked on Canvas, cannot be downloaded (do not retry)", "path": dest_path}
    if dest.exists():
        dest = canvas_copy_path(dest)
        dest_path = str(dest.relative_to(MIRROR_ROOT))
    if DRY_RUN:
        return {"status": "DRY-RUN: would download", "path": dest_path, "size": size}

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    # Pre-signed URL: must be fetched WITHOUT the Authorization header.
    with requests.get(info["url"], stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
    got = tmp.stat().st_size
    if size is not None and got != size:
        tmp.unlink()
        return {"status": "size mismatch, discarded", "path": dest_path, "expected": size, "got": got}
    tmp.replace(dest)
    if dest.suffix == ".zip":
        _unzip_in_place(dest)
        return {"status": "downloaded and unzipped", "path": str(dest.with_suffix("").relative_to(MIRROR_ROOT)), "size": got}
    return {"status": "downloaded", "path": dest_path, "size": got}


def _unzip_in_place(zip_path):
    """Unpack <name>.zip into <name>/ next to it, then delete the zip."""
    import zipfile
    target = zip_path.with_suffix("")
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if member.startswith("__MACOSX/") or ".." in Path(member).parts:
                continue  # Finder junk, or a path trying to escape the folder
            z.extract(member, target)
    zip_path.unlink()


# ---------------------------------------------------------------- Calendar.app

def _as_text(text):
    """Quote a Python string for use inside an AppleScript string literal."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _mkdate_call(dt):
    """AppleScript expression building a local-time date from components.

    Component-by-component is locale-proof; `date "..."` parsing is not.
    AppleScript dates are wall-clock in the Mac's zone, so convert first.
    """
    d = dt.astimezone()  # the Mac's local zone (this Mac is set to Eastern)
    return f"my mkdate({d.year}, {d.month}, {d.day}, {d.hour}, {d.minute}, {d.second})"


_APPLESCRIPT_HELPERS = """
on mkdate(y, m, d, h, mi, s)
    set dt to current date
    set day of dt to 1
    set year of dt to y
    set month of dt to m
    set day of dt to d
    set hours of dt to h
    set minutes of dt to mi
    set seconds of dt to s
    return dt
end mkdate
"""


def _ensure_calendar_running():
    """Launch Calendar.app hidden in the background if it is not already running.

    From a terminal, AppleScript launches it on demand. From a launchd job it
    cannot ("Application isn't running", error -600), so we launch it ourselves.
    """
    if subprocess.run(["pgrep", "-x", "Calendar"], capture_output=True).returncode == 0:
        return
    subprocess.run(["open", "-gj", "-a", "Calendar"], check=True, timeout=30)
    for _ in range(20):  # give it up to ~10 s to come up
        time.sleep(0.5)
        if subprocess.run(["pgrep", "-x", "Calendar"], capture_output=True).returncode == 0:
            time.sleep(2)  # and a moment more to finish loading its calendars
            return


def _osascript(body):
    _ensure_calendar_running()
    script = _APPLESCRIPT_HELPERS + "\n" + body
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        err = r.stderr.strip()
        hint = ""
        if "-600" in err or "-1743" in err:
            hint = (" | Calendar.app is not reachable from this process (macOS Automation permission or "
                    "no GUI session). Retrying will not help; report it.")
        raise RuntimeError(f"osascript failed: {err}{hint}")
    return r.stdout.strip()


def event_key(assignment_id):
    """Stable key stored in the event's URL field so re-runs update instead of duplicate."""
    return f"canvas://assignment/{assignment_id}"


def ensure_calendar():
    return _osascript(f'''
tell application "Calendar"
    if not (exists calendar {_as_text(CALENDAR_NAME)}) then
        make new calendar with properties {{name:{_as_text(CALENDAR_NAME)}}}
    end if
    return name of calendar {_as_text(CALENDAR_NAME)}
end tell''')


def _delete_events(assignment_id):
    """AppleScript snippet: remove every event carrying this assignment's key."""
    return f'''
    set evs to (every event of cal whose url is {_as_text(event_key(assignment_id))})
    set n to count of evs
    repeat with old in evs
        delete old
    end repeat'''


def upsert_calendar_event(assignment_id, title, due_at, alarm):
    """Create or replace the event for one assignment. `alarm` True places the day-of alarm.

    Calendar.app refuses to delete alarms over AppleScript, so an "update" is really
    delete-then-recreate under the same stable key (the URL field). The result is
    always exactly one event with exactly zero or one alarm.

    The event starts at the due minute (Eastern) and lasts one minute.
    """
    due = parse_utc(due_at)
    start = due.replace(second=0, microsecond=0)
    end = start + timedelta(minutes=1)
    alarm_line, alarm_info = "", None
    if alarm:
        alarm_at, rule = compute_alarm(due)
        # trigger interval is minutes relative to the start date; negative = before.
        minutes = round((alarm_at - start).total_seconds() / 60)
        alarm_line = f"tell ev to make new display alarm at end of display alarms with properties {{trigger interval:{minutes}}}"
        alarm_info = {"alarm_eastern": eastern_text(alarm_at), "rule": rule}

    summary = {"assignment_id": assignment_id, "title": title, "due_eastern": eastern_text(due), "alarm": alarm_info}
    if DRY_RUN:
        return {"status": "DRY-RUN: would upsert", **summary}

    description = f"Canvas assignment {assignment_id} | due {eastern_text(due)} | managed by canvas_agent.py"
    result = _osascript(f'''
tell application "Calendar"
    if not (exists calendar {_as_text(CALENDAR_NAME)}) then
        make new calendar with properties {{name:{_as_text(CALENDAR_NAME)}}}
    end if
    set cal to calendar {_as_text(CALENDAR_NAME)}
    {_delete_events(assignment_id)}
    set ev to make new event at end of events of cal with properties {{summary:{_as_text(title)}, start date:{_mkdate_call(start)}, end date:{_mkdate_call(end)}, url:{_as_text(event_key(assignment_id))}, description:{_as_text(description)}}}
    {alarm_line}
    if n is 0 then
        return "created " & (uid of ev)
    else
        return "replaced " & (uid of ev)
    end if
end tell''')
    verb, uid = result.split(" ", 1)
    return {"status": verb, "uid": uid, **summary}


def remove_calendar_alarm(assignment_id):
    """Strip the alarm from one assignment's event (it was submitted).

    Same delete-and-recreate trick: the event is rebuilt from its own stored
    title and dates, minus the alarm.
    """
    if DRY_RUN:
        return {"status": "DRY-RUN: would remove alarm", "assignment_id": assignment_id}
    result = _osascript(f'''
tell application "Calendar"
    set cal to calendar {_as_text(CALENDAR_NAME)}
    set evs to (every event of cal whose url is {_as_text(event_key(assignment_id))})
    if (count of evs) is 0 then return "no event"
    set old to item 1 of evs
    if (count of display alarms of old) is 0 then return "no alarm to remove"
    set t to summary of old
    set s to start date of old
    set e to end date of old
    set d to description of old
    delete old
    make new event at end of events of cal with properties {{summary:t, start date:s, end date:e, url:{_as_text(event_key(assignment_id))}, description:d}}
    return "alarm removed"
end tell''')
    return {"status": result, "assignment_id": assignment_id}


def list_calendar_events():
    """Read back every event in the Canvas calendar: key, title, start (local), alarm offsets."""
    raw = _osascript(f'''
tell application "Calendar"
    if not (exists calendar {_as_text(CALENDAR_NAME)}) then return ""
    set out to ""
    repeat with ev in (every event of calendar {_as_text(CALENDAR_NAME)})
        set d to start date of ev
        set alarms to ""
        repeat with al in (display alarms of ev)
            set alarms to alarms & (trigger interval of al) & ","
        end repeat
        set out to out & (url of ev) & tab & (summary of ev) & tab & (year of d) & tab & ((month of d) as integer) & tab & (day of d) & tab & (time of d) & tab & alarms & linefeed
    end repeat
    return out
end tell''')
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        key, title, y, m, d, secs, alarms = line.split("\t")
        start = datetime(int(y), int(m), int(d)).astimezone() + timedelta(seconds=int(secs))
        offsets = [int(x) for x in alarms.split(",") if x]
        events.append({
            "key": key,
            "assignment_id": int(key.rsplit("/", 1)[1]) if key.startswith("canvas://assignment/") else None,
            "title": title,
            "start_eastern": eastern_text(start),
            "start_utc": start.astimezone(timezone.utc).isoformat(),
            "alarm_offsets_min": offsets,
            "alarms_eastern": [eastern_text(start + timedelta(minutes=o)) for o in offsets],
        })
    return events


# ---------------------------------------------------------------- ledger

def read_ledger():
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text())
    return {"last_run": None, "assignments": {}}


def _write_ledger(ledger):
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2, sort_keys=True))


def mark_handled(assignment_id, action, due_at=None, submitted=None, title=None):
    """Record that one assignment's calendar state is correct as of now."""
    ledger = read_ledger()
    entry = ledger["assignments"].get(str(assignment_id), {})
    entry.update({"action": action, "at": now_utc().isoformat(),
                  "due_at": due_at, "submitted": submitted, "title": title})
    ledger["assignments"][str(assignment_id)] = entry
    if not DRY_RUN:
        _write_ledger(ledger)
    return {"status": "DRY-RUN: not written" if DRY_RUN else "written", "assignment_id": assignment_id, "entry": entry}


def record_run(when=None):
    """Stamp the end of a run so the next run can measure the real gap."""
    ledger = read_ledger()
    ledger["last_run"] = (when or now_utc()).isoformat()
    if not DRY_RUN:
        _write_ledger(ledger)
    return ledger["last_run"]


# ---------------------------------------------------------------- verify

def verify():
    """Re-read Canvas, disk, and Calendar.app and check the three acceptance criteria.

    Returns every failing item. Reads only; never fixes anything.
    """
    assignments = list_assignments()["assignments"]
    events = {e["assignment_id"]: e for e in list_calendar_events() if e["assignment_id"]}
    now = now_utc()
    calendar_failures, alarm_failures, file_failures, notes = [], [], [], []

    for a in assignments:
        if not a["due_at"]:
            continue
        ev = events.get(a["assignment_id"])
        label = f"{a['course_code']} / {a['title']} ({a['assignment_id']})"
        due = parse_utc(a["due_at"])
        if ev is None:
            calendar_failures.append(f"{label}: no event in '{CALENDAR_NAME}' calendar")
            wants_alarm = not a["submitted"] and (a["submission_tracked"] or due > now)
            if wants_alarm:
                alarm_failures.append(f"{label}: no event, so no alarm to check")
            continue
        start = datetime.fromisoformat(ev["start_utc"])
        if to_eastern(start).date() != to_eastern(due).date():
            calendar_failures.append(f"{label}: event is on {ev['start_eastern']} but due {eastern_text(due)}")
        if ev["title"] != a["title"]:
            calendar_failures.append(f"{label}: event title is '{ev['title']}'")

        alarm_times = [start + timedelta(minutes=o) for o in ev["alarm_offsets_min"]]
        if not a["submission_tracked"] and due <= now:
            # Handed in elsewhere and already past: nothing to remind, nothing to check.
            continue
        if a["submitted"]:
            if alarm_times:
                alarm_failures.append(f"{label}: submitted but still has alarm(s) at {ev['alarms_eastern']}")
        else:
            expected, rule = compute_alarm(due, now)
            if not alarm_times:
                alarm_failures.append(f"{label}: unsubmitted but no alarm (expected {eastern_text(expected)}, {rule})")
            elif not rule.startswith("15 min") and not any(abs((t - expected).total_seconds()) < 60 for t in alarm_times):
                alarm_failures.append(f"{label}: alarm at {ev['alarms_eastern']} but expected {eastern_text(expected)} ({rule})")
            # For the "already passed" rule any alarm counts; it was set at run time and has since drifted.

    for course in active_courses():
        listing = list_files(course["id"])
        for f in listing["files"]:
            if f["on_disk"] is False:
                file_failures.append(f"{course['course_code']}: missing {f['path']} ({f['size']} bytes)")
        # Conditions nobody can fix from here are notes, not failures: a hidden Files
        # tab, a locked file, a course that is skipped on purpose.
        for err in listing["errors"]:
            notes.append(f"{course['course_code']}: {err}")
        for path in listing.get("locked", []):
            notes.append(f"{course['course_code']}: {path} is locked on Canvas")
        if listing.get("note"):
            notes.append(f"{course['course_code']}: {listing['note']}")

    return {
        "ok": not (calendar_failures or alarm_failures or file_failures),
        "checked": {"assignments_with_due_date": sum(1 for a in assignments if a["due_at"]),
                    "events_in_calendar": len(events)},
        "calendar_failures": calendar_failures,
        "alarm_failures": alarm_failures,
        "file_failures": file_failures,
        "notes": notes,
    }


# ---------------------------------------------------------------- standalone checks

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "assignments"
    if cmd == "assignments":
        for a in list_assignments()["assignments"]:
            plan = a["alarm_plan"]
            state = "untracked" if a["submitted"] is None else ("SUBMITTED" if a["submitted"] else "unsubmitted")
            alarm = "-" if not plan else (plan["alarm_eastern"] + " " if "alarm_eastern" in plan else "") + "[" + plan["rule"] + "]"
            print(f"{a['assignment_id']:7} {a['course_code'][:12]:12} due={a['due_eastern'] or '-':26} {state:11} alarm={alarm}  {a['title'][:40]}")
    elif cmd == "files":
        listing = list_files(int(sys.argv[2]))
        print(f"{listing['course_code']} -> {MIRROR_ROOT / COURSE_FOLDERS.get(listing['course_code'], '(skipped)')}")
        print(f"  {len(listing['files'])} files, {listing['missing']} new, errors={listing['errors']} {listing.get('note', '')}")
        for f in listing["files"]:
            tag = "ok " if f["on_disk"] else ("LCK" if f["on_disk"] is None else "NEW")
            print(f"  {tag} {f['size']:>9}  {f['path']}")
    elif cmd == "event":
        target = int(sys.argv[2])
        a = next(x for x in list_assignments()["assignments"] if x["assignment_id"] == target)
        print(json.dumps(upsert_calendar_event(a["assignment_id"], a["title"], a["due_at"], alarm=not a["submitted"]), indent=2))
    elif cmd == "calendar":
        print(json.dumps(list_calendar_events(), indent=2))
    elif cmd == "verify":
        print(json.dumps(verify(), indent=2))
