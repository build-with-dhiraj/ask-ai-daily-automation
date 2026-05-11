"""Unit tests for Langfuse fail-fast gate and HTTP retry hardening in daily_digest.

Covers the cleanup-mess/digest-hardening changes:
  1. `_http_get_langfuse` retries on 408/504 in addition to 429/502/503.
  2. `_assert_langfuse_or_exit` exits 1 under the strict gate, no-ops otherwise.
  3. Behaviour-card env vars are stripped of whitespace and emit a diagnostic
     when empty after strip.

No network. The HTTP retry test monkey-patches `urllib.request.urlopen`.
"""

import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_digest", _ROOT / "daily_digest.py")
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["daily_digest"] = _mod
_spec.loader.exec_module(_mod)


class TestAssertLangfuseOrExit(unittest.TestCase):
    """Verify the per-fetch fail-fast helper added in this PR."""

    def test_exits_when_strict_and_not_ok(self) -> None:
        with mock.patch.object(_mod, "_digest_fail_on_langfuse_error", return_value=True):
            with self.assertRaises(SystemExit) as cm:
                _mod._assert_langfuse_or_exit(False, "fetch_langfuse_scores", "detail-x")
            self.assertEqual(cm.exception.code, 1)

    def test_returns_when_strict_and_ok(self) -> None:
        with mock.patch.object(_mod, "_digest_fail_on_langfuse_error", return_value=True):
            # Must not raise.
            self.assertIsNone(_mod._assert_langfuse_or_exit(True, "fetch_langfuse_scores"))

    def test_returns_when_not_strict_even_if_not_ok(self) -> None:
        """When gate is off, preserve legacy behaviour: degraded blocks instead of exit."""
        with mock.patch.object(_mod, "_digest_fail_on_langfuse_error", return_value=False):
            self.assertIsNone(_mod._assert_langfuse_or_exit(False, "fetch_langfuse_errors"))

    def test_structured_log_line_includes_where(self) -> None:
        """Diagnosis is one-shot: log line must name the failing fetch site."""
        captured = io.StringIO()
        with mock.patch.object(_mod, "_digest_fail_on_langfuse_error", return_value=True), \
             mock.patch.object(sys, "stderr", captured):
            with self.assertRaises(SystemExit):
                _mod._assert_langfuse_or_exit(False, "fetch_langfuse_traces_total", "HTTP 504")
        log = captured.getvalue()
        self.assertIn("langfuse_fetch_failed", log)
        self.assertIn("where=fetch_langfuse_traces_total", log)
        self.assertIn("HTTP 504", log)


class TestHttpRetryIncludes408And504(unittest.TestCase):
    """`_http_get_langfuse` must retry on 408 and 504, not just 429/502/503."""

    def _mk_http_error(self, code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="https://example/api",
            code=code,
            msg="err",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )

    def _good_response(self, payload: dict):
        body = json.dumps(payload).encode()
        resp = mock.MagicMock()
        resp.__enter__ = lambda self_: self_
        resp.__exit__ = lambda *a, **kw: False
        resp.read = lambda: body
        return resp

    def test_504_then_200_succeeds_via_retry(self) -> None:
        calls = {"n": 0}

        def fake_urlopen(_req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._mk_http_error(504)
            return self._good_response({"data": [], "meta": {"totalItems": 0}})

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(_mod.time, "sleep", return_value=None):
            out = _mod._http_get_langfuse("https://example/api", {})
        self.assertEqual(out, {"data": [], "meta": {"totalItems": 0}})
        self.assertEqual(calls["n"], 2)

    def test_408_then_200_succeeds_via_retry(self) -> None:
        calls = {"n": 0}

        def fake_urlopen(_req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._mk_http_error(408)
            return self._good_response({"data": [{"id": "x"}]})

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(_mod.time, "sleep", return_value=None):
            out = _mod._http_get_langfuse("https://example/api", {})
        self.assertEqual(out, {"data": [{"id": "x"}]})
        self.assertEqual(calls["n"], 2)

    def test_non_retryable_400_raises_immediately(self) -> None:
        """Sanity: 400 must still propagate; we don't retry on client errors."""
        def fake_urlopen(_req, timeout=None):  # noqa: ANN001
            raise self._mk_http_error(400)

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(_mod.time, "sleep", return_value=None):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                _mod._http_get_langfuse("https://example/api", {})
            self.assertEqual(cm.exception.code, 400)


class TestBehaviourCardEnvStripsWhitespace(unittest.TestCase):
    """Module-level env reads strip whitespace; verify the predicate handles trailing newlines."""

    def test_module_already_strips(self) -> None:
        """`_env_strip_or_default` exists and strips whitespace."""
        # Indirectly exercise: a value like " 33282\n" must become "33282".
        with mock.patch.dict(os.environ, {"X_TEST_KEY": "  33282\n"}, clear=False):
            self.assertEqual(_mod._env_strip_or_default("X_TEST_KEY", ""), "33282")

    def test_empty_env_default_returned(self) -> None:
        with mock.patch.dict(os.environ, {"X_TEST_KEY": "   "}, clear=False):
            self.assertEqual(_mod._env_strip_or_default("X_TEST_KEY", "fallback"), "fallback")


class TestPageCapDefaults(unittest.TestCase):
    """Default page caps must be bounded (60), not unlimited (0)."""

    def test_default_caps_are_60(self) -> None:
        # Module was loaded without LANGFUSE_*_MAX_PAGES set → defaults apply.
        # If the env happened to be set in this process, skip the assertion.
        if "LANGFUSE_ERROR_MAX_PAGES" not in os.environ:
            self.assertEqual(_mod.LANGFUSE_ERROR_MAX_PAGES, 60)
        if "LANGFUSE_SCORE_MAX_PAGES" not in os.environ:
            self.assertEqual(_mod.LANGFUSE_SCORE_MAX_PAGES, 60)


if __name__ == "__main__":
    unittest.main()
