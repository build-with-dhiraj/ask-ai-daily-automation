"""Behavioural tests for the Slack-post 1-retry policy in daily_digest.

These tests exercise post_to_slack under GITHUB_ACTIONS=true so the CI guard
is satisfied, then patch urllib.request.urlopen to drive specific failure
shapes (URLError, HTTPError, 200-with-non-ok-body) and assert:
  - retry happens exactly once on URLError + retryable 5xx
  - retry does NOT happen on 4xx, on 200/non-ok body, or on a second failure

No real HTTP; no Slack contact. Mirrors the pattern in test_slack_post_ci_guard.
"""

import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

_ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """Minimal urlopen() context-manager stand-in."""

    def __init__(self, body: bytes = b"ok", status: int = 200) -> None:
        self._body = body
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self._status


def _http_error(code: int) -> HTTPError:
    # urllib.error.HTTPError requires (url, code, msg, hdrs, fp).
    return HTTPError("https://hooks.slack.com/x", code, "synthetic", {}, None)


class TestDigestSlackRetry(unittest.TestCase):
    def setUp(self) -> None:
        # Patch sleep across all tests so backoff doesn't slow the suite.
        self._sleep_patch = patch("time.sleep")
        self._sleep_patch.start()
        self._env_patch = patch.dict(
            "os.environ",
            {"GITHUB_ACTIONS": "true", "SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"},
            clear=False,
        )
        self._env_patch.start()
        self.mod = _load("daily_digest", "daily_digest.py")

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._sleep_patch.stop()

    def test_retries_once_on_urlerror_then_succeeds(self) -> None:
        urlopen = patch(
            "urllib.request.urlopen",
            side_effect=[URLError("net"), _FakeResp(b"ok")],
        )
        with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
            result = self.mod.post_to_slack([], "x")
        self.assertTrue(result)
        self.assertEqual(mock_url.call_count, 2)

    def test_retries_once_on_503_then_succeeds(self) -> None:
        urlopen = patch(
            "urllib.request.urlopen",
            side_effect=[_http_error(503), _FakeResp(b"ok")],
        )
        with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
            result = self.mod.post_to_slack([], "x")
        self.assertTrue(result)
        self.assertEqual(mock_url.call_count, 2)

    def test_no_retry_on_4xx(self) -> None:
        urlopen = patch(
            "urllib.request.urlopen",
            side_effect=[_http_error(403), _FakeResp(b"ok")],
        )
        with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
            result = self.mod.post_to_slack([], "x")
        self.assertFalse(result)
        self.assertEqual(mock_url.call_count, 1)

    def test_no_retry_on_200_non_ok_body(self) -> None:
        urlopen = patch(
            "urllib.request.urlopen",
            side_effect=[_FakeResp(b"channel_archived")],
        )
        with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
            result = self.mod.post_to_slack([], "x")
        self.assertFalse(result)
        self.assertEqual(mock_url.call_count, 1)

    def test_two_consecutive_urlerror_gives_up(self) -> None:
        # Caps duplicate-post risk: a second connect failure within 5s means
        # Slack is genuinely down, not flaky — give up rather than spin.
        urlopen = patch(
            "urllib.request.urlopen",
            side_effect=[URLError("net1"), URLError("net2")],
        )
        with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
            result = self.mod.post_to_slack([], "x")
        self.assertFalse(result)
        self.assertEqual(mock_url.call_count, 2)


if __name__ == "__main__":
    unittest.main()
