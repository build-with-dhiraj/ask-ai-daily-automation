"""
poster_publisher.py: publish daily poster PNGs to the gh-pages branch.

Part of C1.2c (poster-format redesign). See scripts/README_poster_publisher.md
for the operational contract.

Public surface:
    publish_poster(png_bytes, surface, date_str, *, short_sha=None) -> str
    _verify_url_reachable(url, timeout=120) -> bool

Behavior:
    - Writes PNG into a local checkout of the gh-pages branch under
      posters/{surface}/{date_str}[-{short_sha}].png
    - Commits the file with a deterministic message
    - DOES NOT push by default. Push only when POSTER_AUTO_PUSH=1 in env.
    - Returns the eventual public URL (regardless of whether we pushed).

Design notes:
    - We use a separate working-tree checkout under .gh-pages-worktree/
      via `git worktree add` so the caller's working tree is never disturbed.
    - The worktree is reused across calls if present; we `git fetch` + reset
      to origin/gh-pages on every publish to avoid drift.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKTREE_DIR = REPO_ROOT / ".gh-pages-worktree"
GH_PAGES_BRANCH = "gh-pages"
PUBLIC_URL_BASE = (
    "https://build-with-dhiraj.github.io/ask-ai-daily-automation/posters"
)

Surface = Literal["scoreboard", "digest"]


class PosterPublishError(RuntimeError):
    """Raised when the publish flow cannot complete."""


class PosterPublishUnreachableError(RuntimeError):
    """Raised when a freshly-published gh-pages URL did not become reachable
    within the verification window. Distinct from PosterPublishError so the
    caller can degrade with a more specific cause string (`publish_unreachable`)
    and so the verify step does not get swallowed by a publish-failure handler.
    """


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a git/shell command, return stdout. Raise on non-zero."""
    log.debug("$ %s  (cwd=%s)", " ".join(cmd), cwd)
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise PosterPublishError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout


def _ensure_worktree() -> Path:
    """Ensure a gh-pages worktree exists and points at the latest gh-pages tip.

    Returns the worktree path.
    """
    # Make sure the local branch ref exists (created either locally or by fetch).
    try:
        _run(["git", "fetch", "origin", GH_PAGES_BRANCH], cwd=REPO_ROOT)
    except PosterPublishError as e:
        # Remote may not have gh-pages yet (first push). That's OK; local branch
        # must still exist for the worktree to be created. We don't fail here,
        # but emit a clear hint so first-time operators know what to do.
        log.warning(
            "fetch gh-pages skipped: %s. "
            "First run? Bootstrap origin/gh-pages once with: "
            "`git push -u origin gh-pages` before triggering this workflow.",
            e,
        )

    if WORKTREE_DIR.exists():
        # Reset worktree to local gh-pages tip (or remote if available)
        try:
            _run(["git", "fetch", "origin", GH_PAGES_BRANCH], cwd=WORKTREE_DIR)
            _run(
                ["git", "reset", "--hard", f"origin/{GH_PAGES_BRANCH}"],
                cwd=WORKTREE_DIR,
            )
        except PosterPublishError:
            # No remote yet; reset to local gh-pages
            _run(
                ["git", "reset", "--hard", GH_PAGES_BRANCH], cwd=WORKTREE_DIR
            )
        return WORKTREE_DIR

    # Create new worktree on gh-pages branch
    _run(
        ["git", "worktree", "add", str(WORKTREE_DIR), GH_PAGES_BRANCH],
        cwd=REPO_ROOT,
    )
    return WORKTREE_DIR


def _build_filename(date_str: str, short_sha: Optional[str]) -> str:
    if short_sha:
        return f"{date_str}-{short_sha}.png"
    return f"{date_str}.png"


def publish_poster(
    png_bytes: bytes,
    surface: Surface,
    date_str: str,
    *,
    short_sha: Optional[str] = None,
) -> str:
    """Publish a poster PNG to gh-pages and return its eventual public URL.

    Args:
        png_bytes: raw PNG bytes
        surface: "scoreboard" or "digest"
        date_str: "YYYY-MM-DD"
        short_sha: optional 7-char git sha to disambiguate dogfooding runs
                   (workflow_dispatch). When set, filename becomes
                   {date_str}-{short_sha}.png.

    Returns:
        Public URL the file will be served at (after GitHub Pages publishes).

    Side effects:
        - Creates/updates .gh-pages-worktree/
        - Commits to local gh-pages branch
        - Pushes to origin/gh-pages ONLY if POSTER_AUTO_PUSH=1
    """
    if surface not in ("scoreboard", "digest"):
        raise ValueError(f"invalid surface: {surface!r}")
    if not png_bytes:
        raise ValueError("png_bytes is empty")
    if len(date_str) != 10 or date_str[4] != "-" or date_str[7] != "-":
        raise ValueError(f"date_str must be YYYY-MM-DD, got: {date_str!r}")

    worktree = _ensure_worktree()
    filename = _build_filename(date_str, short_sha)
    rel_path = Path("posters") / surface / filename
    abs_path = worktree / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(png_bytes)

    _run(["git", "add", str(rel_path)], cwd=worktree)
    # Allow re-publish (idempotent): only commit if something changed.
    status = _run(["git", "status", "--porcelain"], cwd=worktree).strip()
    if status:
        commit_msg = f"poster: {surface} {filename}"
        _run(
            ["git", "commit", "-m", commit_msg, "--", str(rel_path)],
            cwd=worktree,
        )
    else:
        log.info("no change to commit for %s (idempotent re-publish)", rel_path)

    public_url = f"{PUBLIC_URL_BASE}/{surface}/{filename}"

    if os.environ.get("POSTER_AUTO_PUSH") == "1":
        log.info("POSTER_AUTO_PUSH=1 → pushing to origin/%s", GH_PAGES_BRANCH)
        _run(
            ["git", "push", "origin", f"HEAD:{GH_PAGES_BRANCH}"], cwd=worktree
        )
    else:
        log.info(
            "POSTER_AUTO_PUSH not set; committed locally only. "
            "Run: git -C %s push origin HEAD:%s",
            worktree,
            GH_PAGES_BRANCH,
        )

    return public_url


def _verify_url_reachable(url: str, timeout: int = 120) -> bool:
    """Poll a URL until it returns HTTP 200 or timeout elapses.

    Note on default timeout: GitHub Pages takes 30s to 2 min to serve a freshly
    pushed file on the first publish (subsequent updates settle in ~30s). The
    default of 120s covers the first-publish case; callers that know the file
    is already cached can pass a shorter timeout.

    GitHub Pages takes ~30 to 60s to publish a freshly-pushed file. Callers
    should treat False as a soft failure (the push succeeded; serving lags).

    Args:
        url: public URL to probe
        timeout: total seconds to wait (default 30)

    Returns:
        True if the URL returned 200 within timeout, else False.
    """
    deadline = time.monotonic() + timeout
    delay = 2.0
    last_err: Optional[str] = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
                last_err = f"HTTP {resp.status}"
        except HTTPError as e:
            last_err = f"HTTPError {e.code}"
        except URLError as e:
            last_err = f"URLError {e.reason}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(delay)
        delay = min(delay * 1.5, 10.0)
    log.warning("URL not reachable within %ds: %s (last: %s)", timeout, url, last_err)
    return False


def cleanup_worktree() -> None:
    """Remove the gh-pages worktree. Useful for tests."""
    if not WORKTREE_DIR.exists():
        return
    try:
        _run(
            ["git", "worktree", "remove", "--force", str(WORKTREE_DIR)],
            cwd=REPO_ROOT,
        )
    except PosterPublishError:
        shutil.rmtree(WORKTREE_DIR, ignore_errors=True)
