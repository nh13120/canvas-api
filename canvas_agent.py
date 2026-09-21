"""Canvas reconciliation agent: keeps the local mirror of MIT coursework in agreement with Canvas.

The model runs the loop. This file only (1) exposes the mechanical tools from
agent_tools.py to the model, (2) writes the goal prompt, (3) prints a one-line
trace per tool call, and (4) saves the model's final report as the day's brief.

Run:  ./.venv/bin/python canvas_agent.py               # dry run (default while building)
      ./.venv/bin/python canvas_agent.py --live        # real downloads and calendar writes
      ./.venv/bin/python canvas_agent.py --seed-stale  # corrupt ledger.json first, to demo drift repair
"""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import agent_tools as t
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

HERE = Path(__file__).parent
BRIEFS = HERE / "briefs"
load_dotenv(HERE / ".env")

MODEL = "claude-sonnet-5"   # Sonnet tier, pinned on purpose (see platform models overview)
MAX_TURNS = 200             # first live run needs ~120 downloads; later runs need a handful


# ---------------------------------------------------------------- tools
# Each tool is a thin wrapper: call the plain function, hand back JSON text.
# The SDK requires async handlers; nothing here actually awaits anything.

def _reply(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}]}


def _error(exc):
    return {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "is_error": True}


@tool("list_assignments",
      "Every active MIT course (with course_id) and every assignment in it: due date (UTC and "
      "Eastern), submission state, and where the day-of alarm belongs if one is needed. No side effects.", {})
async def list_assignments(args):
    try:
        return _reply(t.list_assignments())
    except Exception as e:
        return _error(e)


@tool("list_files",
      "Full file tree of one course with the on-disk path each file must live at and whether "
      "it is already there (path + size match). No side effects.", {"course_id": int})
async def list_files(args):
    try:
        return _reply(t.list_files(args["course_id"]))
    except Exception as e:
        return _error(e)


@tool("download_file",
      "Download one Canvas file to dest_path (use the path from list_files). Skips files already "
      "on disk with the same size.", {"file_id": int, "dest_path": str})
async def download_file(args):
    try:
        return _reply(t.download_file(args["file_id"], args["dest_path"]))
    except Exception as e:
        return _error(e)


@tool("upsert_calendar_event",
      "Create or replace the Calendar.app event for one assignment (keyed by assignment_id). "
      "alarm=true places the day-of alarm computed from the due time; alarm=false leaves none.",
      {"assignment_id": int, "title": str, "due_at": str, "alarm": bool})
async def upsert_calendar_event(args):
    try:
        return _reply(t.upsert_calendar_event(args["assignment_id"], args["title"], args["due_at"], args["alarm"]))
    except Exception as e:
        return _error(e)


@tool("remove_calendar_alarm",
      "Strip the alarm from one assignment's existing event, keeping the event.", {"assignment_id": int})
async def remove_calendar_alarm(args):
    try:
        return _reply(t.remove_calendar_alarm(args["assignment_id"]))
    except Exception as e:
        return _error(e)


@tool("read_ledger",
      "ledger.json: when the last run finished and, per assignment, the due date and submission "
      "state that were last reconciled. Files are not tracked here; the disk is their ledger.", {})
async def read_ledger(args):
    return _reply(t.read_ledger())


@tool("mark_handled",
      "Record in ledger.json that one assignment's calendar state is now correct, with the due "
      "date and submission state it was reconciled against.",
      {"type": "object",
       "properties": {"assignment_id": {"type": "integer"}, "action": {"type": "string"},
                      "due_at": {"type": "string"}, "title": {"type": "string"},
                      "submitted": {"type": ["boolean", "null"],
                                    "description": "null for courses whose submission is not tracked in Canvas"}},
       "required": ["assignment_id", "action", "due_at", "submitted", "title"]})
async def mark_handled(args):
    try:
        return _reply(t.mark_handled(args["assignment_id"], args["action"], args["due_at"], args["submitted"], args["title"]))
    except Exception as e:
        return _error(e)


@tool("verify",
      "Re-read Canvas, the disk, and Calendar.app and check all three acceptance criteria. "
      "Returns every failing item. Read-only.", {})
async def verify(args):
    try:
        return _reply(t.verify())
    except Exception as e:
        return _error(e)


TOOLS = [list_assignments, list_files, download_file, upsert_calendar_event,
         remove_calendar_alarm, read_ledger, mark_handled, verify]
SERVER = create_sdk_mcp_server(name="canvas", version="1.0.0", tools=TOOLS)
ALLOWED = [f"mcp__canvas__{fn.name}" for fn in TOOLS]


# ---------------------------------------------------------------- prompts

SYSTEM_PROMPT = """You are a reconciliation agent for one student's MIT Canvas coursework.
You keep three things in agreement with Canvas: Calendar.app events, alarms on those events,
and a local file mirror. The tools are mechanical; every decision is yours.

Acceptance criteria you are responsible for:
1. CALENDAR - every assignment with a due date has an event on the right Eastern day with the assignment's title.
2. FILES - every course file is on disk at exactly the path list_files gives (never re-download what is there).
   The mirror is the student's own "Semester 1" folder; list_files already knows each course's subfolder and
   layout, and download_file never overwrites an existing file. A course list_files reports as "not mirrored
   on purpose" needs no file work at all.
3. ALERTS - every unsubmitted assignment has its day-of alarm; every submitted one has none.

How to use the ledger: ledger.json records, per assignment, the due date and submission state that were
last reconciled. If an assignment's live due_at and submitted values equal the ledger entry, its calendar
state is already correct - skip it. Anything new, changed, or missing from the ledger needs
upsert_calendar_event (alarm = not submitted) followed by mark_handled. A submission flip to "submitted"
only needs the alarm gone; either remove_calendar_alarm or a fresh upsert with alarm=false is fine.
Assignments with no due date get no event and no ledger entry; only assignments with due dates belong in the ledger.

Some courses hand homework in outside Canvas, so Canvas cannot know whether it was submitted.
list_assignments marks those submission_tracked=false with submitted=null. For them: a future due date gets its
event with alarm=true (a reminder), a past due date gets its event with alarm=false, and you never describe
them as unsubmitted, late, or needing attention. Pass submitted=null to mark_handled for them.

Files: call list_files for every course that list_assignments returned (including courses with no
assignments) and download_file for every entry with on_disk=false. Issue many
independent tool calls in one turn when you can. A course whose Files tab is inaccessible is not your
fault; report it. A file list_files marks as locked (on_disk=null) cannot be downloaded by anyone: mention
it once in the brief and never call download_file for it.

All times the tools show you are already converted to Eastern. Do not reason about UTC yourself.

If a tool fails twice with the same error, the environment is broken in a way you cannot fix: stop calling
it, do not try workarounds, and put the error verbatim in the brief under "Needs attention".

Finish with verify(). If it reports failures you can fix, fix them once and verify again. Its "notes" are
conditions nobody can fix (hidden Files tab, locked file, course skipped on purpose): mention them, do not
re-verify because of them. Then write the
brief as your final message, in Markdown, with these sections:
# Canvas brief - <date>
## Since last run  (the gap, and what drifted)
## Due soon        (unsubmitted work in due-date order, with due time and alarm time)
## Changes made    (calendar, alarms, files - or "none, everything was already correct")
## Verification    (each criterion: PASS or the failing items)
## Needs attention (anything you could not fix, or anything you judge the student should know)
Be concrete and short. Never invent an item that a tool did not return."""


def build_goal(today, ledger, dry_run):
    last = ledger.get("last_run")
    if last:
        gap = datetime.now().astimezone() - datetime.fromisoformat(last)
        since = f"The last recorded run finished {t.eastern_text(datetime.fromisoformat(last))}, {gap.days} days and {gap.seconds // 3600} hours ago."
    else:
        since = "There is no recorded previous run; treat everything as new."
    mode = ("DRY RUN: tools will report what they WOULD do without downloading or writing to Calendar.app "
            "or the ledger. Still call them so the trace shows the full plan, and say in the brief that it was a dry run."
            if dry_run else "This is a live run.")
    return (f"It's {today}. {since} {mode}\n\n"
            "Reconcile my MIT coursework. Every assignment should have a correct calendar event; every "
            "unsubmitted one should have a day-of alarm and every submitted one should not; every course "
            "file should be on disk in its Canvas folder structure. Fix whatever drifted since the last run. "
            "Skip what the ledger says is already correct. Then verify all three and report.")


# ---------------------------------------------------------------- demo helper

def seed_stale():
    """Write a wrong due date and a wrong submission state into ledger.json.

    Picks the first two assignments with due dates, so the next run has visible drift to repair.
    """
    live = [a for a in t.list_assignments()["assignments"] if a["due_at"] and a["submission_tracked"]]
    ledger = t.read_ledger()
    wrong_due, wrong_sub = live[0], live[1]
    ledger["assignments"][str(wrong_due["assignment_id"])] = {
        "title": wrong_due["title"], "due_at": "2020-01-01T00:00:00Z",
        "submitted": wrong_due["submitted"], "action": "seeded-stale-due-date", "at": t.now_utc().isoformat()}
    ledger["assignments"][str(wrong_sub["assignment_id"])] = {
        "title": wrong_sub["title"], "due_at": wrong_sub["due_at"],
        "submitted": not wrong_sub["submitted"], "action": "seeded-stale-submission", "at": t.now_utc().isoformat()}
    t.LEDGER_FILE.write_text(json.dumps(ledger, indent=2, sort_keys=True))
    print(f"seeded stale ledger: wrong due date on {wrong_due['title']!r}, "
          f"flipped submission on {wrong_sub['title']!r}")


# ---------------------------------------------------------------- run

def trace_call(block):
    """One line per tool call: the tool name and its arguments, trimmed."""
    name = block.name.replace("mcp__canvas__", "")
    args = ", ".join(f"{k}={str(v)[:60]!r}" if isinstance(v, str) else f"{k}={v}" for k, v in block.input.items())
    print(f"→ {name}({args})")


def trace_result(block):
    """One line per tool result: the first meaningful line of what came back."""
    text = block.content if isinstance(block.content, str) else " ".join(
        c.get("text", "") for c in (block.content or []) if isinstance(c, dict))
    first = " ".join(text.split())[:110]
    print(f"   {'✗' if block.is_error else '✓'} {first}")


async def run(dry_run, max_turns, quiet=False):
    t.DRY_RUN = dry_run
    today = datetime.now(t.EASTERN).strftime("%A %Y-%m-%d")
    goal = build_goal(today, t.read_ledger(), dry_run)
    print(f"[{'DRY RUN' if dry_run else 'LIVE'}] model={MODEL} max_turns={max_turns}\n{goal}\n")

    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"canvas": SERVER},
        tools=[],                   # no Bash/Read/Write: the model gets only the tools above
        allowed_tools=ALLOWED,
        max_turns=max_turns,
        cwd=str(HERE),
        env={k: os.environ[k] for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL") if k in os.environ},
    )

    final_text, result = "", None
    async for message in query(prompt=goal, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    trace_call(block)
        elif isinstance(message, UserMessage) and isinstance(message.content, list) and not quiet:
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    trace_result(block)
        elif isinstance(message, ResultMessage):
            result = message
            final_text = message.result or ""

    if result is None:
        raise SystemExit("no result message from the SDK")
    print(f"\n--- {result.subtype}: {result.num_turns} turns, ${result.total_cost_usd or 0:.4f}, {result.duration_ms / 1000:.0f}s ---\n")

    # One file per day; the 7am and 6pm runs both append to it.
    BRIEFS.mkdir(exist_ok=True)
    brief_path = BRIEFS / f"{datetime.now(t.EASTERN):%Y-%m-%d}.md"
    stamp = f"_Run at {datetime.now(t.EASTERN):%H:%M %Z}{' (dry run: nothing downloaded or written)' if dry_run else ''}_"
    existing = brief_path.read_text() if brief_path.exists() else ""
    brief_path.write_text(existing + ("\n\n---\n\n" if existing else "") + stamp + "\n\n" + final_text + "\n")
    if quiet:
        # Only the verdict on screen; the whole brief is in the file.
        start = final_text.find("## Verification")
        end = final_text.find("## Needs attention")
        print(final_text[start:end].strip() if start >= 0 else final_text)
    else:
        print(final_text)
    print(f"\nbrief written to {brief_path.relative_to(HERE)}")

    if not dry_run and not result.is_error:
        t.record_run()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="really download and write to Calendar.app")
    parser.add_argument("--dry-run", action="store_true", help="(default) report what would happen, change nothing")
    parser.add_argument("--seed-stale", action="store_true", help="corrupt ledger.json first to demo drift repair")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    parser.add_argument("--quiet", action="store_true", help="trace tool calls only; print just the verification verdict")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set (put it in .env)")
    if args.seed_stale:
        seed_stale()
    asyncio.run(run(dry_run=not args.live, max_turns=args.max_turns, quiet=args.quiet))


if __name__ == "__main__":
    main()
