"""Submit the current public HEAD of this repo to the Blast Radius track.

Drives the real Brave window started with tools/brave_debug.sh (remote debugging
on port 9222, profile ~/.buildathon-brave). Not Chrome for Testing.

Usage:
  python3 tools/submit_brave.py            # submit HEAD
  python3 tools/submit_brave.py <sha>      # submit that commit (must be on GitHub)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = 9222
TRACK = "https://www.joinbuilders.org/buildathon/tracks/blast-radius"
REPO = "https://github.com/viktor-lorentz/blast-radius"
ROOT = Path(__file__).resolve().parents[1]


def head_sha() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return (ROOT / ".git" / "HEAD").read_text().strip() if False else _git_head()


def _git_head() -> str:
    import subprocess

    return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()


def main() -> None:
    sha = head_sha()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3) as r:
            ver = json.load(r)
    except Exception as e:
        sys.exit(
            "Brave debug port is not up. Start it with tools/brave_debug.sh "
            f"and sign in once in that window.\n{e}"
        )
    print("browser", ver.get("Browser"), flush=True)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TRACK, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        Path("/tmp/blast-submit-page.txt").write_text(body)
        if page.url.rstrip("/").endswith("/sign-in") or "The email you registered with" in body:
            sys.exit("Not signed in. Sign in inside the Brave window, then run this again.")
        if "left this hour" in body and "0 of 1 left" in body:
            sys.exit("Hourly slot is already used. Not submitting.")
        repo_box = page.locator("input[name='repo']")
        commit_box = page.locator("input[name='commit']")
        if repo_box.count() != 1 or commit_box.count() != 1:
            sys.exit(f"Could not find the submit form. Page saved to /tmp/blast-submit-page.txt url={page.url}")
        repo_box.fill(REPO)
        commit_box.fill(sha)
        # The hand-in control is a <button> with no type attribute (it defaults to submit).
        # "Pick as our final entry" is a different button, outside this form.
        button = page.locator("form button", has_text="Submit")
        if button.count() != 1:
            sys.exit(f"Expected one hand-in button, found {button.count()}")
        button.click()
        page.wait_for_timeout(4000)
        after = page.inner_text("body")
        Path("/tmp/blast-submit-page.txt").write_text(after)
        print("submitted", sha, flush=True)
        # Surface a receipt if the page shows one.
        for line in after.splitlines():
            if "BB-" in line or "queued" in line.lower() or "submission" in line.lower():
                print(line.strip())
        browser.close()


if __name__ == "__main__":
    main()
