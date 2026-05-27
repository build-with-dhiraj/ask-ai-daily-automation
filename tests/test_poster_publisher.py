"""Smoke test for scripts.poster_publisher.

Confirms publish_poster() writes a file into the local gh-pages worktree and
commits it, without pushing to origin (POSTER_AUTO_PUSH unset).

Test fixture: a minimal 1x1 transparent PNG (no real renderer needed).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.poster_publisher import (  # noqa: E402
    WORKTREE_DIR,
    cleanup_worktree,
    publish_poster,
)

# 1x1 transparent PNG, valid bytes
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


def _has_gh_pages_branch() -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "gh-pages"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


@pytest.fixture(autouse=True)
def _ensure_no_auto_push(monkeypatch):
    # Defensive: never let this test push
    monkeypatch.delenv("POSTER_AUTO_PUSH", raising=False)
    yield


@pytest.fixture
def cleanup():
    # Snapshot the gh-pages tip BEFORE the test so we can roll local back to
    # it afterwards. Without this, commits accumulate on the local gh-pages
    # branch across tests and a later test that asserts on HEAD's log sees
    # a previous test's commit on top.
    prev = subprocess.run(
        ["git", "rev-parse", "gh-pages"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    prev_sha = prev.stdout.strip() if prev.returncode == 0 else None
    yield
    cleanup_worktree()
    if prev_sha:
        subprocess.run(
            ["git", "update-ref", "refs/heads/gh-pages", prev_sha],
            cwd=REPO_ROOT, check=False,
        )


@pytest.mark.skipif(
    not _has_gh_pages_branch(),
    reason="gh-pages branch must exist locally for this smoke test",
)
def test_publish_poster_writes_file_and_commits_locally(cleanup):
    date_str = "2026-05-27"
    surface = "scoreboard"
    url = publish_poster(TINY_PNG, surface, date_str)

    # Expected URL shape
    assert url == (
        "https://build-with-dhiraj.github.io/ask-ai-daily-automation/"
        f"posters/{surface}/{date_str}.png"
    )

    # File exists in worktree at expected path
    expected = WORKTREE_DIR / "posters" / surface / f"{date_str}.png"
    assert expected.exists(), f"poster not written at {expected}"
    assert expected.read_bytes() == TINY_PNG

    # Worktree HEAD should have a commit referencing the file
    log = subprocess.run(
        ["git", "log", "-1", "--name-only", "--pretty=format:%s"],
        cwd=WORKTREE_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"posters/{surface}/{date_str}.png" in log
    assert f"poster: {surface} {date_str}.png" in log

    # No push happened; worktree HEAD should differ from origin if origin exists;
    # but more important: the env var was unset, so we just assert the log line
    # was written. (Active push would have required network.)


@pytest.mark.skipif(
    not _has_gh_pages_branch(),
    reason="gh-pages branch must exist locally for this smoke test",
)
def test_publish_poster_dogfood_short_sha_appended(cleanup):
    url = publish_poster(
        TINY_PNG, "digest", "2026-05-27", short_sha="abc1234"
    )
    assert url.endswith("/posters/digest/2026-05-27-abc1234.png")
    assert (
        WORKTREE_DIR / "posters" / "digest" / "2026-05-27-abc1234.png"
    ).exists()


def test_publish_poster_rejects_bad_inputs():
    with pytest.raises(ValueError):
        publish_poster(b"", "scoreboard", "2026-05-27")
    with pytest.raises(ValueError):
        publish_poster(TINY_PNG, "scoreboard", "bad-date")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        publish_poster(TINY_PNG, "bogus", "2026-05-27")  # type: ignore[arg-type]
