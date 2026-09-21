# Canvas reconciliation agent

Keeps a local mirror of MIT coursework in agreement with Canvas while Canvas changes underneath it.
Twice a day an agent works out what drifted since the last run and brings three things back into line:

1. **Calendar** — every assignment with a due date is an event in macOS Calendar.app, on the right Eastern day.
2. **Files** — every course file is on disk in the existing course folders, never re-downloaded, never overwritten.
3. **Alerts** — every unsubmitted assignment has an 8:00 am Eastern alarm on its due date; submitted ones have none.

The model runs the loop. The tools are mechanical (fetch, convert, write, verify) and make no decisions.

## How it works

- `canvas.py` — small Canvas API client (pagination, credentials from `.env`).
- `agent_tools.py` — the plain functions: list assignments and files, download, upsert Calendar events
  via AppleScript, read/write `ledger.json`, and `verify()` which re-reads Canvas, disk and Calendar.app.
- `canvas_agent.py` — wraps those functions as `@tool`s on an in-process MCP server (Claude Agent SDK),
  pins the model, restricts the agent to those tools only, prints a one-line trace per call,
  and saves the model's final report to `briefs/YYYY-MM-DD.md`.
- `ledger.json` — per assignment, the due date and submission state last reconciled, plus the time of the
  last run. The disk itself is the ledger for files (path + size).
- `launchd/` — LaunchAgent plists for the 7:00 and 18:00 daily runs.

Alarm placement is computed from each deadline: 8:00 am Eastern on the due date; 8:00 pm the evening before
if that is within two hours of the deadline; 15 minutes from now if it has already passed. Canvas reports UTC,
so `2026-09-24T03:59:59Z` is Wednesday 11:59 pm Eastern and its alarm lands on Wednesday morning.

## Setup

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in the Canvas token and the Anthropic key
```

Edit `COURSE_FOLDERS` and `MIRROR_ROOT` in `agent_tools.py` to point at your own course folders.
Courses graded outside Canvas go in `EXTERNAL_SUBMISSION_COURSES` so they only get reminders.

## Run

```
./.venv/bin/python canvas_agent.py                # dry run: no downloads, no calendar writes
./.venv/bin/python canvas_agent.py --live         # real run
./.venv/bin/python canvas_agent.py --seed-stale --live --quiet   # corrupt the ledger, watch it get repaired
```

Standalone checks without the model: `agent_tools.py assignments | files <course_id> | calendar | verify`.

To schedule, edit the paths in `launchd/*.plist`, copy them to `~/Library/LaunchAgents/`, and
`launchctl bootstrap gui/$(id -u) <plist>`. Logs go to `~/Library/Logs/canvas-agent/`; a LaunchAgent
cannot write under `~/Desktop`.

`sync_files.py` and `hw.py` are the earlier standalone scripts this was built on.
