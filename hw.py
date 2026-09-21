"""List assignments for one course. Default: AI Studio (MAS.665).

Run:  python3 hw.py            # MAS.665
      python3 hw.py "AI Edge"  # any course-name fragment
"""

import sys
from datetime import datetime, timezone

from canvas import get

MATCH = (sys.argv[1] if len(sys.argv) > 1 else "MAS.665").lower()

courses = get("mit", "/courses", enrollment_state="active")
hits = [c for c in courses
        if MATCH in (c.get("name") or "").lower()
        or MATCH in (c.get("course_code") or "").lower()]

if not hits:
    print("No match. Active MIT courses:")
    for c in courses:
        print(" ", c.get("id"), "|", c.get("course_code"), "|", c.get("name"))
    sys.exit()

now = datetime.now(timezone.utc)

for c in hits:
    print(f"\n=== {c.get('name')} (id {c.get('id')}) ===")
    # include=submission adds my own submission state to each assignment.
    items = get("mit", f"/courses/{c['id']}/assignments",
                include=["submission"], order_by="due_at")
    for a in items:
        due = a.get("due_at")
        state = (a.get("submission") or {}).get("workflow_state", "?")
        when = datetime.fromisoformat(due.replace("Z", "+00:00")) if due else None
        if when is None:
            flag = "no-due"
        elif when < now:
            flag = "past"
        else:
            flag = "DUE"
        print(f"  {flag:7} {due or '-':26} [{state:11}] {a.get('name')}")
