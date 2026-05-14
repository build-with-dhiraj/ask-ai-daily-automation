"""Staging skips per-UTC-date idempotency entirely.

The staging Slack channel is a test surface — operators must be able to
re-dispatch repeatedly throughout the same UTC day and see fresh posts. The
day-level marker is therefore disabled for SLACK_TARGET=staging in both
daily_digest.py and daily_eval.py. Prod (and the implicit-prod fallback)
keep the original idempotency behavior unchanged.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _BaseMarkerTest(unittest.TestCase):
    """Shared scaffolding: an isolated DIGEST_STATE_DIR per test."""

    module_name: str = ""
    filename: str = ""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_dir = self._tmp.name
        self.mod = _load(self.module_name, self.filename)

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _marker(self, prefix: str, target: str) -> Path:
        return Path(self._state_dir) / f"{prefix}-{target}-{self._today()}.marker"


class TestDigestStagingSkipsIdempotency(_BaseMarkerTest):
    module_name = "daily_digest_staging_test"
    filename = "daily_digest.py"

    def test_staging_bypasses_marker_check_even_when_marker_present(self) -> None:
        marker = self._marker("digest-posted", "staging")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pretend-prior-run\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {"DIGEST_STATE_DIR": self._state_dir, "SLACK_TARGET": "staging"},
            clear=False,
        ):
            self.assertFalse(
                self.mod._already_posted_today("digest-posted"),
                "staging must NOT short-circuit on a same-day marker",
            )

    def test_staging_write_marker_is_noop(self) -> None:
        with patch.dict(
            "os.environ",
            {"DIGEST_STATE_DIR": self._state_dir, "SLACK_TARGET": "staging"},
            clear=False,
        ):
            self.mod._write_posted_marker("digest-posted")
        # No marker file of any kind should have been created for staging.
        contents = os.listdir(self._state_dir) if os.path.isdir(self._state_dir) else []
        self.assertEqual(
            [], contents, f"staging must not write any marker; found {contents}"
        )

    def test_prod_still_blocks_same_day_repost(self) -> None:
        marker = self._marker("digest-posted", "prod")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pretend-prior-run\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {"DIGEST_STATE_DIR": self._state_dir, "SLACK_TARGET": "prod"},
            clear=False,
        ):
            self.assertTrue(
                self.mod._already_posted_today("digest-posted"),
                "prod must continue to short-circuit on a same-day marker",
            )
        # And the marker must still be on disk afterwards (we never removed it).
        self.assertTrue(marker.exists(), "prod marker must persist")

    def test_default_no_env_defaults_to_prod_behavior(self) -> None:
        marker = self._marker("digest-posted", "prod")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pretend-prior-run\n", encoding="utf-8")
        env = {"DIGEST_STATE_DIR": self._state_dir}
        # Explicitly drop SLACK_TARGET so we exercise the implicit-prod path.
        with patch.dict("os.environ", env, clear=False):
            os.environ.pop("SLACK_TARGET", None)
            self.assertTrue(
                self.mod._already_posted_today("digest-posted"),
                "absent SLACK_TARGET must default to prod (blocks same-day repost)",
            )


class TestEvalStagingSkipsIdempotency(_BaseMarkerTest):
    module_name = "daily_eval_staging_test"
    filename = "daily_eval.py"

    def test_staging_bypasses_marker_check_even_when_marker_present(self) -> None:
        marker = self._marker("eval-posted", "staging")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pretend-prior-run\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {"DIGEST_STATE_DIR": self._state_dir, "SLACK_TARGET": "staging"},
            clear=False,
        ):
            self.assertFalse(
                self.mod._eval_already_posted_today("eval-posted"),
                "staging must NOT short-circuit on a same-day marker",
            )

    def test_staging_write_marker_is_noop(self) -> None:
        with patch.dict(
            "os.environ",
            {"DIGEST_STATE_DIR": self._state_dir, "SLACK_TARGET": "staging"},
            clear=False,
        ):
            self.mod._eval_write_posted_marker("eval-posted")
        contents = os.listdir(self._state_dir) if os.path.isdir(self._state_dir) else []
        self.assertEqual(
            [], contents, f"staging must not write any marker; found {contents}"
        )

    def test_prod_still_blocks_same_day_repost(self) -> None:
        marker = self._marker("eval-posted", "prod")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pretend-prior-run\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {"DIGEST_STATE_DIR": self._state_dir, "SLACK_TARGET": "prod"},
            clear=False,
        ):
            self.assertTrue(
                self.mod._eval_already_posted_today("eval-posted"),
                "prod must continue to short-circuit on a same-day marker",
            )
        self.assertTrue(marker.exists(), "prod marker must persist")

    def test_default_no_env_defaults_to_prod_behavior(self) -> None:
        marker = self._marker("eval-posted", "prod")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pretend-prior-run\n", encoding="utf-8")
        env = {"DIGEST_STATE_DIR": self._state_dir}
        with patch.dict("os.environ", env, clear=False):
            os.environ.pop("SLACK_TARGET", None)
            self.assertTrue(
                self.mod._eval_already_posted_today("eval-posted"),
                "absent SLACK_TARGET must default to prod (blocks same-day repost)",
            )


if __name__ == "__main__":
    unittest.main()
