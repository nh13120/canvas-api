"""Canvas API client for multiple schools.

Run:  python3 canvas.py
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Add a school by adding one line here and two lines in .env.
SCHOOLS = {
    "mit": ("MIT_CANVAS_URL", "MIT_CANVAS_TOKEN"),
    "hbs": ("HBS_CANVAS_URL", "HBS_CANVAS_TOKEN"),
}


def credentials(school):
    """Look up one school's URL and token, or explain what's missing."""
    url_key, token_key = SCHOOLS[school]
    url, token = os.getenv(url_key), os.getenv(token_key)
    if not url or not token or token.startswith("PASTE"):
        raise RuntimeError(f"Set {url_key} and {token_key} in .env")
    return url, token


def get(school, path, **params):
    """GET one Canvas endpoint, following pagination to the last page.

    Canvas returns 10 items by default and does NOT tell you more exist —
    it puts the next page in the Link header. This follows it so you always
    get the full list.
    """
    url, token = credentials(school)
    headers = {"Authorization": f"Bearer {token}"}
    next_url = f"{url}/api/v1{path}"
    params = {"per_page": 100, **params}
    results = []

    while next_url:
        r = requests.get(next_url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        results.extend(r.json())
        # After the first request the Link URL already carries the params.
        params = None
        next_url = r.links.get("next", {}).get("url")

    return results


def whoami(school):
    url, token = credentials(school)
    r = requests.get(
        f"{url}/api/v1/users/self",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    for school in SCHOOLS:
        try:
            me = whoami(school)
            courses = get(school, "/courses", enrollment_state="active")
        except RuntimeError as e:
            print(f"[{school}] skipped — {e}\n")
            continue
        except requests.HTTPError as e:
            print(f"[{school}] FAILED — {e.response.status_code} {e.response.text[:80]}\n")
            continue

        print(f"[{school}] {me.get('name')} ({me.get('id')}) — {len(courses)} courses")
        for c in courses:
            print(f"  {c.get('id')}  {c.get('name')}")
        print()
