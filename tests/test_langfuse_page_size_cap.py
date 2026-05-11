"""Regression tests for Langfuse `limit` page-size cap (cleanup-mess/langfuse-400-fix).

Langfuse Cloud rejects `limit > 100` on `/api/public/observations` and
`/api/public/scores` with HTTP 400:

    {"message":"Invalid request data",
     "error":[{"origin":"number","code":"too_big","maximum":100,
               "inclusive":true,"path":["limit"],
               "message":"Too big: expected number to be <=100"}]}

These tests pin the new behaviour so we don't silently regress to limit=500.
No network — `urllib.request.urlopen` is monkey-patched.
"""

import importlib
import importlib.util
import json
import os
import sys
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]


def _load_module(env_overrides: Optional[Dict[str, Optional[str]]] = None):
    """Reload daily_digest so module-level LANGFUSE_PAGE_SIZE re-evaluates env."""
    saved = {}
    if env_overrides:
        for k, v in env_overrides.items():
            saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(
            "daily_digest_under_test", _ROOT / "daily_digest.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["daily_digest_under_test"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


class TestLangfusePageSizeDefault(unittest.TestCase):
    """LANGFUSE_PAGE_SIZE must default to 100, the Cloud cap."""

    def test_default_is_100(self) -> None:
        mod = _load_module({"LANGFUSE_OBSERVATION_PAGE_SIZE": None})
        self.assertEqual(mod.LANGFUSE_PAGE_SIZE, 100)

    def test_env_override_capped_at_100(self) -> None:
        """Even if someone sets the env var to 500, we must clamp to 100 — otherwise we
        re-introduce the HTTP 400 bug on every run."""
        mod = _load_module({"LANGFUSE_OBSERVATION_PAGE_SIZE": "500"})
        self.assertLessEqual(mod.LANGFUSE_PAGE_SIZE, 100)

    def test_env_override_below_cap_respected(self) -> None:
        mod = _load_module({"LANGFUSE_OBSERVATION_PAGE_SIZE": "50"})
        self.assertEqual(mod.LANGFUSE_PAGE_SIZE, 50)

    def test_env_override_below_1_clamped_to_1(self) -> None:
        mod = _load_module({"LANGFUSE_OBSERVATION_PAGE_SIZE": "0"})
        self.assertEqual(mod.LANGFUSE_PAGE_SIZE, 1)


class TestLangfuseFetchersUseCappedLimit(unittest.TestCase):
    """End-to-end shape check: the URLs built by both fetchers carry limit<=100."""

    def _good(self, payload: dict):
        body = json.dumps(payload).encode()
        resp = mock.MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a, **kw: False
        resp.read = lambda: body
        return resp

    def _capture_limits(
        self,
        fetch_fn: str,
        env_overrides: Optional[Dict[str, Optional[str]]] = None,
    ) -> List[int]:
        """Run a fetcher with mocked urlopen and return all `limit` query values seen."""
        mod = _load_module(env_overrides)
        captured: List[str] = []

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            captured.append(req.full_url)
            # Return an empty page so the paginator exits on the first call.
            return self._good({"data": [], "meta": {"totalItems": 0}})

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(mod.time, "sleep", return_value=None):
            getattr(mod, fetch_fn)()

        self.assertTrue(captured, f"{fetch_fn}: urlopen was not called")
        limits: List[int] = []
        for url in captured:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertIn("limit", q, f"{fetch_fn}: URL missing `limit` param: {url}")
            limits.append(int(q["limit"][0]))
        return limits

    def test_errors_fetcher_uses_limit_100_by_default(self) -> None:
        limits = self._capture_limits("fetch_langfuse_errors")
        for lim in limits:
            self.assertLessEqual(lim, 100, f"observations limit must be <=100, got {lim}")

    def test_scores_fetcher_uses_limit_100_by_default(self) -> None:
        limits = self._capture_limits("fetch_langfuse_scores")
        for lim in limits:
            self.assertLessEqual(lim, 100, f"scores limit must be <=100, got {lim}")

    def test_errors_fetcher_capped_even_when_env_says_500(self) -> None:
        limits = self._capture_limits(
            "fetch_langfuse_errors",
            env_overrides={"LANGFUSE_OBSERVATION_PAGE_SIZE": "500"},
        )
        for lim in limits:
            self.assertLessEqual(
                lim, 100,
                f"env override must not bypass the 100 cap; got limit={lim}",
            )

    def test_scores_fetcher_capped_even_when_env_says_500(self) -> None:
        limits = self._capture_limits(
            "fetch_langfuse_scores",
            env_overrides={"LANGFUSE_OBSERVATION_PAGE_SIZE": "500"},
        )
        for lim in limits:
            self.assertLessEqual(
                lim, 100,
                f"env override must not bypass the 100 cap; got limit={lim}",
            )


if __name__ == "__main__":
    unittest.main()
