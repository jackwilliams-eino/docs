#!/usr/bin/env python3
"""
generate_release_notes.py

Manually generate a customer-facing release note from merged GitHub PRs.

What it does, in order:
  1. Fetches PRs merged in a date window from one or more repos (GitHub Search API).
  2. Drops obvious branch-move PRs (Dev -> release, main -> dev, etc.).
  3. Sends the remaining PR titles + descriptions to Claude with the drafting prompt.
  4. Wraps Claude's output in a Mintlify <Update> block and writes it to a .mdx file.

It does NOT publish. You review the output, paste it into changelog.mdx in the docs
repo, open a PR, and let the normal review/merge flow publish it via Mintlify.

Setup (once):
    pip install anthropic requests
    export GITHUB_TOKEN=...        # a token with read access to the repos
    export ANTHROPIC_API_KEY=...   # from console.anthropic.com

Usage:
    python generate_release_notes.py                                  # auto-detect latest release window
    python generate_release_notes.py --since 2026-06-17 --until 2026-06-22
    python generate_release_notes.py --since 2026-06-17

Leaving --since blank auto-detects the window from the two most recent
"Dev -> release" promotion PRs in the release-signal repo.
"""

import argparse
import datetime as dt
import os
import re
import sys

import requests
from anthropic import Anthropic

# --- Defaults -------------------------------------------------------------

# The repos that actually ship user-facing product. Confirm this list with David/JT:
# frontend + backend for sure; add eino-app (walk-test app) if its features count.
DEFAULT_REPOS = [
    "eino-ai/private-network-planning-frontend",
    "eino-ai/pnp_platform",
]

DEFAULT_MODEL = "claude-sonnet-4-6"  # good balance for drafting; Opus is overkill here
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "release-notes-agent-prompt.md")

# Auto-detect uses these: a "Dev -> release" PR is one whose base branch is the release
# branch. We read the release cadence from the frontend repo. Confirm both with JT if
# detection ever returns nothing.
RELEASE_SIGNAL_REPO = "eino-ai/private-network-planning-frontend"
RELEASE_BRANCH = "release"

# Titles that are branch moves, not features. Dropped before anything reaches the model.
BRANCH_MOVE_RE = re.compile(
    r"^\s*(dev|main|master|release|staging)\s*(->|→|to)\s*(dev|main|master|release|staging)\s*$",
    re.IGNORECASE,
)

GITHUB_API = "https://api.github.com/search/issues"


# --- GitHub ---------------------------------------------------------------

def fetch_merged_prs(repo, since, until, token):
    """Return merged PRs in [since, until] for one repo, as dicts with number/title/body/url."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    query = f"repo:{repo} is:pr is:merged merged:{since}..{until}"
    prs, page = [], 1
    while True:
        resp = requests.get(
            GITHUB_API,
            headers=headers,
            params={"q": query, "per_page": 100, "page": page},
            timeout=30,
        )
        if resp.status_code != 200:
            sys.exit(f"GitHub API error for {repo}: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        for item in data.get("items", []):
            prs.append(
                {
                    "repo": repo,
                    "number": item["number"],
                    "title": item["title"] or "",
                    "body": (item.get("body") or "").strip(),
                    "url": item["html_url"],
                }
            )
        # Stop once we've collected everything the search reported.
        if len(prs) >= data.get("total_count", 0) or not data.get("items"):
            break
        page += 1
    return prs


def is_feature_candidate(pr):
    """Drop branch-move PRs in code; the prompt handles the subtler test/chore filtering."""
    return not BRANCH_MOVE_RE.match(pr["title"])


def detect_release_window(token):
    """Find the date window of the latest release from the two most recent promotion PRs.

    A "Dev -> release" PR is one merged into the release branch. We take the most recent
    one as the end of the window and the one before it as the start, so the window covers
    everything merged since the previous release. Returns (since, until) as YYYY-MM-DD.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{RELEASE_SIGNAL_REPO}/pulls"
    resp = requests.get(
        url,
        headers=headers,
        params={"state": "closed", "base": RELEASE_BRANCH, "per_page": 50,
                "sort": "updated", "direction": "desc"},
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"GitHub API error detecting release window: {resp.status_code} {resp.text[:300]}")

    # Keep only merged PRs, newest merge first.
    merged = [pr for pr in resp.json() if pr.get("merged_at")]
    merged.sort(key=lambda pr: pr["merged_at"], reverse=True)

    if len(merged) < 2:
        sys.exit(
            f"Could not auto-detect a release window (found {len(merged)} promotion PRs "
            f"into '{RELEASE_BRANCH}' on {RELEASE_SIGNAL_REPO}). "
            "Pass --since and --until explicitly, or check the release branch name."
        )

    current, previous = merged[0], merged[1]
    until = current["merged_at"][:10]  # date of the latest release
    prev_date = dt.date.fromisoformat(previous["merged_at"][:10])
    since = (prev_date + dt.timedelta(days=1)).isoformat()  # day after the previous release
    return since, until


# --- Drafting -------------------------------------------------------------

def build_pr_digest(prs):
    """Format the PR list as the model's input. Titles + trimmed descriptions."""
    lines = []
    for pr in prs:
        lines.append(f"- (#{pr['number']}, {pr['repo'].split('/')[-1]}) {pr['title']}")
        if pr["body"]:
            body = pr["body"][:1500]  # keep prompt size sane
            indented = "\n".join("    " + ln for ln in body.splitlines())
            lines.append(indented)
    return "\n".join(lines)


def draft_release_note(prompt_text, pr_digest, model):
    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        system=prompt_text,
        messages=[{"role": "user", "content": f"Merged PRs in this release:\n\n{pr_digest}"}],
    )
    return "".join(block.text for block in msg.content if block.type == "text").strip()


def insert_into_changelog(changelog_path, entry):
    """Insert the entry into changelog.mdx, newest first.

    Places the new <Update> block above the most recent existing one (so it lands below
    the frontmatter and comments but above older entries). If there are no entries yet,
    appends it after the existing header content.
    """
    with open(changelog_path, encoding="utf-8") as f:
        content = f.read()
    marker = content.find("<Update label=")
    if marker == -1:
        new_content = content.rstrip() + "\n\n" + entry.strip() + "\n"
    else:
        new_content = content[:marker] + entry.strip() + "\n\n" + content[marker:]
    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(new_content)


# --- Main -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a release note from merged GitHub PRs.")
    parser.add_argument("--since", help="Include PRs merged on/after this date (YYYY-MM-DD). If omitted, auto-detects from the latest release.")
    parser.add_argument("--until", help="Include PRs merged on/before this date (YYYY-MM-DD). Defaults to the detected release date, or today.")
    parser.add_argument("--repos", help="Comma-separated owner/repo list (default: frontend + pnp_platform).")
    parser.add_argument("--prompt", default=PROMPT_PATH, help="Path to the drafting prompt file.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model to use.")
    parser.add_argument("--out", default="./output", help="Directory to write the .mdx file into.")
    parser.add_argument("--label", help="Date label for the <Update> block (default: --until value).")
    parser.add_argument("--changelog", help="If set, insert the entry directly into this changelog.mdx file (newest first).")
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        sys.exit("Set GITHUB_TOKEN (a token with read access to the repos).")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY (from console.anthropic.com).")

    repos = [r.strip() for r in args.repos.split(",")] if args.repos else DEFAULT_REPOS
    with open(args.prompt, encoding="utf-8") as f:
        prompt_text = f.read()

    # Resolve the date window.
    if args.since:
        since = args.since
        until = args.until or dt.date.today().isoformat()
    else:
        # No --since: auto-detect from the two most recent release-promotion PRs.
        since, until = detect_release_window(github_token)
        if args.until:  # let an explicit --until override the detected end date
            until = args.until
        print(f"  auto-detected window: {since} to {until}", file=sys.stderr)

    # 1. Fetch
    all_prs = []
    for repo in repos:
        found = fetch_merged_prs(repo, since, until, github_token)
        print(f"  {repo}: {len(found)} merged PRs in window", file=sys.stderr)
        all_prs.extend(found)

    # 2. Filter branch moves
    features = [pr for pr in all_prs if is_feature_candidate(pr)]
    print(f"  {len(features)} candidate PRs after dropping branch moves", file=sys.stderr)
    if not features:
        sys.exit("No candidate PRs found in that window. Widen --since or check the repos.")

    # 3. Draft
    digest = build_pr_digest(features)
    if len(digest.strip()) < 20:
    sys.exit("PR digest is too short to be meaningful — check that the repos and date window are correct.")
    body = draft_release_note(prompt_text, digest, args.model)

    # 4. Wrap in a Mintlify <Update> block (script owns the date, model owns the content)
    label = args.label or until
    entry = f'<Update label="{label}">\n\n{body}\n\n</Update>\n'

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, f"release-note-{label}.mdx")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(entry)

    print(f"\nWrote {out_path}\n", file=sys.stderr)
    print(entry)

    # Optionally insert straight into the changelog (used by the GitHub Actions workflow).
    if args.changelog:
        insert_into_changelog(args.changelog, entry)
        print(f"Inserted entry into {args.changelog}", file=sys.stderr)


if __name__ == "__main__":
    main()
