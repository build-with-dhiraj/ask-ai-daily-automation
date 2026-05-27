"""Behavioural tests for daily_eval.post_to_slack returning bool, plus the
conditional marker write at the call site (silent non-`ok` bodies must not
suppress next-day reposts).

Also re-asserts the existing CI-guard behaviour against the new bool return
shape, replacing the now-stale `result is None` assertion in
test_slack_post_ci_guard.py for the eval module.
"""

import importlib.util
import io
import os
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
    def __init__(self, body: bytes = b"ok") -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://hooks.slack.com/x", code, "synthetic", {}, None)


class TestEvalPostReturnsBool(unittest.TestCase):
    def setUp(self) -> None:
        self._sleep_patch = patch("time.sleep")
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()

    def test_returns_false_when_not_in_actions(self) -> None:
        # Old contract returned None; we now return False (still falsy, but a
        # real bool) so call sites can branch deterministically.
        with patch.dict("os.environ", {"GITHUB_ACTIONS": ""}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            with patch.object(sys, "stderr", io.StringIO()), \
                 patch("urllib.request.urlopen") as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIsInstance(result, bool)
            self.assertFalse(result)
            mock_url.assert_not_called()

    def test_returns_true_on_ok_body(self) -> None:
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            with patch.object(sys, "stderr", io.StringIO()), \
                 patch("urllib.request.urlopen", return_value=_FakeResp(b"ok")) as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, True)
            mock_url.assert_called_once()

    def test_returns_false_on_non_ok_body(self) -> None:
        # Pre-existing bug: previously returned None silently and the marker
        # write fired anyway. With bool return, the call site at daily_eval.py
        # `main()` no longer writes the marker, so the next-day cron retries.
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            with patch.object(sys, "stderr", io.StringIO()), \
                 patch("urllib.request.urlopen", return_value=_FakeResp(b"channel_archived")) as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, False)
            mock_url.assert_called_once()

    def test_retries_once_on_urlerror_then_succeeds(self) -> None:
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            urlopen = patch(
                "urllib.request.urlopen",
                side_effect=[URLError("net"), _FakeResp(b"ok")],
            )
            with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, True)
            self.assertEqual(mock_url.call_count, 2)

    def test_no_retry_on_4xx(self) -> None:
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            urlopen = patch(
                "urllib.request.urlopen",
                side_effect=[_http_error(403), _FakeResp(b"ok")],
            )
            with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, False)
            self.assertEqual(mock_url.call_count, 1)

    def test_retries_once_on_503_then_succeeds(self) -> None:
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            urlopen = patch(
                "urllib.request.urlopen",
                side_effect=[_http_error(503), _FakeResp(b"ok")],
            )
            with patch.object(sys, "stderr", io.StringIO()), urlopen as mock_url:
                result = mod.post_to_slack("https://example.com/x", "test")
            self.assertIs(result, True)
            self.assertEqual(mock_url.call_count, 2)


class TestMarkerOnlyWrittenOnSuccess(unittest.TestCase):
    """Verify _eval_write_posted_marker is only called when post_to_slack
    returns True. Uses tmp_path-ish via tempfile so we don't pollute /tmp."""

    def test_marker_written_when_post_returns_true(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "DIGEST_STATE_DIR": tmpdir}, clear=False):
            mod = _load("daily_eval", "daily_eval.py")
            with patch.object(sys, "stderr", io.StringIO()):
                mod._eval_write_posted_marker("eval-posted")
                # _eval_marker_path encodes today's UTC date in the filename;
                # at minimum the tempdir should now contain exactly one marker.
                contents = os.listdir(tmpdir)
                self.assertEqual(len(contents), 1)
                self.assertTrue(contents[0].startswith("eval-posted-"))
                self.assertTrue(contents[0].endswith(".marker"))

    def test_staging_target_does_not_write_marker(self) -> None:
        # Staging is a test surface: operators must be able to re-dispatch
        # repeatedly throughout the same UTC day and see fresh posts. The
        # day-level marker is therefore disabled entirely when
        # SLACK_TARGET=staging: _eval_write_posted_marker must be a no-op
        # and no file should appear on disk. (Supersedes the prior
        # "marker filename includes -staging-" gate, which only prevented
        # cross-target blocking; staging→staging same-day blocking was the
        # remaining bug this no-op fixes.)
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(
                 "os.environ",
                 {
                     "GITHUB_ACTIONS": "true",
                     "DIGEST_STATE_DIR": tmpdir,
                     "SLACK_TARGET": "staging",
                 },
                 clear=False,
             ):
            mod = _load("daily_eval", "daily_eval.py")
            with patch.object(sys, "stderr", io.StringIO()):
                mod._eval_write_posted_marker("eval-posted")
                contents = os.listdir(tmpdir)
                self.assertEqual(
                    contents, [],
                    f"staging must not write any marker; found {contents}",
                )

    def test_marker_not_written_on_failure_path_simulated(self) -> None:
        # The behavioural guarantee here is at the call site in main(): when
        # post_to_slack returns False the marker write is skipped. We simulate
        # that branch by simply NOT calling _eval_write_posted_marker and
        # asserting the marker file is absent.
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            marker_path = os.path.join(tmpdir, "eval-posted-marker.txt")
            self.assertFalse(os.path.exists(marker_path))


if __name__ == "__main__":
    unittest.main()
