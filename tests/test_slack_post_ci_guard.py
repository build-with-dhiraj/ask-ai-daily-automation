"""Unit tests for the GITHUB_ACTIONS=true Slack-post guard in daily_digest and daily_eval.

These guards prevent accidental Slack posts when the scripts are run locally
(e.g. `python3 daily_digest.py` from a developer Mac). GitHub Actions sets
GITHUB_ACTIONS=true on every job; local shells do not.

No network calls are made by these tests (urllib.request.urlopen is patched).
"""

import importlib.util
import io
import sys
import unittest
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


class TestDigestSlackCIGuard(unittest.TestCase):
    def test_digest_post_skipped_when_not_in_actions(self) -> None:
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "", "SLACK_WEBHOOK_URL": "https://example.com/x"}, clear=False):
            mod = _load("daily_digest", "daily_digest.py")
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf), patch("urllib.request.urlopen") as mock_url:
                result = mod.post_to_slack([], "x")
            self.assertFalse(result)
            self.assertIn("Not running in GitHub Actions", buf.getvalue())
            mock_url.assert_not_called()

    def test_digest_post_attempted_when_in_actions(self) -> None:
        # GITHUB_ACTIONS=true but empty SLACK_WEBHOOK_URL → returns False for the
        # empty-webhook reason (NOT the CI-guard reason). Proves the guard does
        # not incorrectly short-circuit under CI.
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "SLACK_WEBHOOK_URL": ""}, clear=False):
            mod = _load("daily_digest", "daily_digest.py")
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf), patch("urllib.request.urlopen") as mock_url:
                result = mod.post_to_slack([], "x")
            self.assertFalse(result)
            self.assertIn("SLACK_WEBHOOK_URL not set", buf.getvalue())
            self.assertNotIn("Not running in GitHub Actions", buf.getvalue())
            mock_url.assert_not_called()


class TestEvalSlackCIGuard(unittest.TestCase):
    def test_eval_post_skipped_when_not_in_actions(self) -> None:
        # Contract change (2026-05-12): post_to_slack now returns bool (False
        # on the local-run guard) instead of None, so the call site can
        # branch on success. Old assertion was `assertIsNone(result)`.
        with patch.dict("os.environ", {"GITHUB_ACTIONS": ""}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf), patch("urllib.request.urlopen") as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, False)
            self.assertIn("Not running in GitHub Actions", buf.getvalue())
            mock_url.assert_not_called()

    def test_eval_post_attempted_when_in_actions(self) -> None:
        # GITHUB_ACTIONS=true → guard passes → urllib.request.urlopen IS called.
        # We mock urlopen so no real HTTP request happens.
        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"ok"

        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf), patch("urllib.request.urlopen", return_value=_FakeResp()) as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, True)
            self.assertNotIn("Not running in GitHub Actions", buf.getvalue())
            mock_url.assert_called_once()


if __name__ == "__main__":
    unittest.main()
