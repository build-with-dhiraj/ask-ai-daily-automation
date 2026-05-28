"""Post-RCA regression tests: daily_digest.main() actually exercises the
poster pipeline by default, and falls back to the legacy Block Kit text
post only under the three explicit failure modes.

These are the tests that should have caught the silent gate fall-through
on workflow_dispatch run #26517069840. Pin the inverted gate behaviour
so the next operator cannot silently re-introduce an opt-in.

Strategy: mock every upstream fetcher at module level so main() reaches
the Slack-post branch without doing any real I/O, then patch the two
Slack-facing functions and assert which one was called.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _import(name: str, relpath: str):
    """Import-or-reuse: if already in sys.modules, return that object so
    mock.patch.object targets the same module that `from X import Y`
    inside main() resolves to via sys.modules.

    Code Reviewer F6 hardening: when we reuse the cached entry, assert
    its __file__ resolves to the SAME path we'd otherwise load from. A
    future test that does `del sys.modules['scripts.poster_slack']` or
    assigns a fake module to that key would otherwise silently poison
    the next test that calls _import; this raises early with a clear
    cross-pollution message instead.
    """
    expected = (_ROOT / relpath).resolve()
    if name in sys.modules:
        existing = sys.modules[name]
        existing_file = getattr(existing, "__file__", "") or ""
        try:
            existing_resolved = Path(existing_file).resolve()
        except (OSError, ValueError):
            existing_resolved = None
        if existing_resolved != expected:
            raise RuntimeError(
                f"sys.modules[{name!r}] points to {existing_file!r}, "
                f"expected {str(expected)!r}; another test polluted the cache"
            )
        return existing
    spec = importlib.util.spec_from_file_location(name, expected)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _EnvScope:
    """Tiny context manager for env mutation that always restores."""

    def __init__(self, **kv):
        self.kv = kv
        self.prev: dict = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.prev[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, exc_type, exc, tb):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _minimal_today_summary() -> dict:
    # Must carry every key listed in _DIGEST_REQUIRED_KEYS inside
    # daily_digest.py:main()'s poster-path precondition (SRE item 9), so the
    # default happy-path test doesn't trip the snapshot guard. If you add a
    # new required key in main(), add it here too.
    return {
        "date": "2026-05-26",
        "downvote_rate_pct": 0.4,
        "academic_fail_pct": 4.2,
        "langfuse_errors_total": 5,
        "total_traces_24h": 1000,
        "cost_latency_answer_by_model": {},
        "cost_latency_classifier": {"cost_usd": 0.0},
    }


def _minimal_insights() -> dict:
    return {
        "headline": "Test headline.",
        "insights": [{"topic_label": "T", "icon": "📈",
                      "claim": "Claim.", "evidence": "",
                      "context": None, "spark_series": None}],
        "kill_switch_breach": False,
        "_llm_unavailable": False,
    }


class _BaseDigestMainTest(unittest.TestCase):
    """Shared scaffolding: stub every digest upstream so main() reaches
    the Slack-post branch without doing any real I/O."""

    def setUp(self) -> None:
        self.digest = _import("daily_digest", "daily_digest.py")
        self.ps = _import("scripts.poster_slack", "scripts/poster_slack.py")
        self.tmpdir = tempfile.mkdtemp(prefix="digest_main_test_")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_main(self, env_overrides: dict, *, render_side_effect=None,
                  publish_returns=True, render_returns_url="https://example/p.png",
                  today_summary_override=None):
        """Patch everything heavy, run main(), return (exit_code, m_render,
        m_post_blocks, m_post_text, stderr).

        today_summary_override: when not None, replaces the default
        _minimal_today_summary() return value (used by the snapshot-precondition
        tests to inject an empty / partial dict). None means "use the default
        minimal happy-path summary".
        """
        from io import StringIO
        stderr_buf = StringIO()

        m_render = mock.MagicMock(return_value=render_returns_url)
        if render_side_effect is not None:
            m_render.side_effect = render_side_effect
        m_post_blocks = mock.MagicMock(return_value=publish_returns)
        m_post_text = mock.MagicMock(return_value=True)
        today_summary_value = (
            today_summary_override if today_summary_override is not None
            else _minimal_today_summary()
        )

        env = {
            "GITHUB_ACTIONS": "true",
            "POSTER_DISABLE": None,
            "POSTER_DRY_RUN": None,
            "FORCE_REPOST": "1",
            "DIGEST_FAIL_ON_LANGFUSE_ERROR": "0",
        }
        env.update(env_overrides)

        patches = [
            mock.patch.object(self.digest, "_preflight_langfuse_or_exit"),
            mock.patch.object(self.digest, "fetch_metabase_card",
                              return_value=[]),
            mock.patch.object(self.digest, "fetch_metabase_card_detailed",
                              return_value=([], None)),
            mock.patch.object(self.digest, "fetch_langfuse_scores",
                              return_value=([], 0, True, False)),
            mock.patch.object(self.digest, "fetch_langfuse_errors",
                              return_value=([], 0, True, False)),
            mock.patch.object(self.digest, "fetch_langfuse_traces_total",
                              return_value=(1000, True)),
            mock.patch.object(
                self.digest,
                "fetch_yesterday_cost_and_latency_from_stream_logs",
                return_value={"ok": True, "answer_by_model": {},
                              "classifier": None},
            ),
            mock.patch.object(
                self.digest,
                "fetch_feedback_breakdown_from_stream_logs",
                return_value={"ok": True, "by_model": {}},
            ),
            mock.patch.object(self.digest, "load_eval_summary",
                              return_value={}),
            mock.patch.object(self.digest, "load_classifier_snapshot",
                              return_value=None),
            mock.patch.object(self.digest, "_summarise_today_for_snapshot",
                              return_value=today_summary_value),
            mock.patch.object(self.digest, "_load_yesterday_snapshot",
                              return_value=None),
            mock.patch.object(self.digest, "fmt_top_insights",
                              return_value=_minimal_insights()),
            mock.patch.object(self.digest, "_write_digest_snapshot"),
            mock.patch.object(self.digest, "_already_posted_today",
                              return_value=False),
            mock.patch.object(self.digest, "_write_posted_marker"),
            mock.patch.object(self.digest, "build_blocks",
                              return_value=[{"type": "section",
                                             "text": {"type": "mrkdwn",
                                                      "text": "stub"}}]),
            mock.patch.object(self.digest, "build_plain_fallback",
                              return_value="fallback line one\nfallback line two"),
            mock.patch.object(self.ps, "render_and_publish", m_render),
            mock.patch.object(self.ps, "post_blocks_to_slack", m_post_blocks),
            mock.patch.object(self.digest, "post_to_slack", m_post_text),
            mock.patch("time.sleep"),
            mock.patch("sys.stderr", stderr_buf),
        ]
        with _EnvScope(**env), contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            exit_code = self.digest.main()
        return exit_code, m_render, m_post_blocks, m_post_text, stderr_buf.getvalue()


class TestDigestMainPosterPathByDefault(_BaseDigestMainTest):

    # REGRESSION: any new opt-in gate keyed on an undefined env var must break
    # this assertion. The whole point of the gate inversion was that the poster
    # path is the DEFAULT and only POSTER_DISABLE=1 (or a render/publish/post
    # failure) takes the legacy path.
    def test_main_runs_poster_path_by_default(self) -> None:
        """No POSTER_DISABLE in env: poster pipeline fires, legacy stays untouched."""
        exit_code, m_render, m_post_blocks, m_post_text, _ = self._run_main({})
        self.assertEqual(exit_code, 0)
        self.assertEqual(m_render.call_count, 1)
        # Single-message-per-surface (Commit 21ccf6f): exactly one block post,
        # no programmatic thread reply.
        self.assertEqual(m_post_blocks.call_count, 1)
        m_post_text.assert_not_called()

    def test_main_falls_back_when_POSTER_DISABLE_set(self) -> None:
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {"POSTER_DISABLE": "1"}
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_not_called()
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        # Second positional arg is the fallback_text string.
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(
            sent_text.startswith("⚠️ Poster degraded (see workflow logs)"),
            f"missing degradation marker: {sent_text[:80]!r}",
        )
        self.assertIn("cause=disabled", stderr)

    def test_main_falls_back_on_render_failure(self) -> None:
        from scripts.poster_renderer import PosterRenderError
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {}, render_side_effect=PosterRenderError("digest", "boom"),
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_called_once()
        # No main block post should have fired (render returned None / raised).
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        self.assertIn("cause=render", stderr)

    # Removed in Variant D: thread_reply path eliminated per Commit 21ccf6f
    # (single-message-per-surface; build_thread_blocks is no longer called from
    # the main poster path, so the F4 wiring regression cannot recur).

    def test_main_falls_back_when_snapshot_empty(self) -> None:
        """SRE item 9: an empty today_summary means the poster would render
        all-zero metrics + green kill-switch (silent-wrong-output). Force
        degrade with cause=snapshot up front."""
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {}, today_summary_override={},
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_not_called()
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        self.assertIn("cause=snapshot", stderr)
        self.assertIn("empty", stderr)

    def test_main_falls_back_when_snapshot_missing_required_key(self) -> None:
        """SRE item 9: a today_summary missing one of the must-have keys must
        also degrade with cause=snapshot and the offending key listed in
        the reason= field."""
        broken = _minimal_today_summary()
        broken.pop("downvote_rate_pct", None)
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {}, today_summary_override=broken,
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_not_called()
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        self.assertIn("cause=snapshot", stderr)
        self.assertIn("downvote_rate_pct", stderr)

    def test_programming_error_propagates_not_swallowed(self) -> None:
        """B1: a NameError from render_and_publish is NOT recoverable;
        it must propagate so CI surfaces the bug red. Catching `Exception`
        would have silently degraded to the legacy fallback and hidden it."""
        with self.assertRaises(NameError):
            self._run_main({}, render_side_effect=NameError("oops"))

    def test_main_falls_back_on_publish_unreachable(self) -> None:
        """SRE item 4: render returns a URL but the verify probe says it is
        not reachable -> caller degrades to legacy with cause=publish_unreachable.

        After F2 (narrowed internal except in render_and_publish), the typed
        PosterPublishUnreachableError propagates out and the caller's typed
        except clause logs cause=publish_unreachable specifically.
        """
        from scripts.poster_publisher import PosterPublishUnreachableError
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {},
            render_side_effect=PosterPublishUnreachableError(
                "gh-pages URL not reachable within 120s: https://x/p.png"
            ),
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_called_once()
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        self.assertIn("cause=publish_unreachable", stderr)
        self.assertIn("gh-pages URL not reachable", stderr)

    def test_main_falls_back_on_publish_error(self) -> None:
        """F2 regression: a PosterPublishError raised inside render_and_publish
        (e.g. gh-pages git push 403) must surface as cause=publish, not the
        misleading cause=render reason=render_and_publish returned None that
        dogfood run #26532281104 produced before the bare-except was narrowed.
        """
        from scripts.poster_publisher import PosterPublishError
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {},
            render_side_effect=PosterPublishError(
                "command failed (128): git push origin HEAD:gh-pages"
            ),
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_called_once()
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        # The whole point of F2: cause=publish, NOT cause=render.
        self.assertIn("cause=publish", stderr)
        self.assertNotIn("cause=render", stderr)
        self.assertIn("git push origin HEAD:gh-pages", stderr)

    def test_main_falls_back_on_publish_failure(self) -> None:
        # render succeeds, but post_blocks_to_slack returns False.
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {}, publish_returns=False,
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_called_once()
        # Main attempt did fire (returned False), thread-reply did NOT.
        self.assertEqual(m_post_blocks.call_count, 1)
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        self.assertIn("cause=post", stderr)


if __name__ == "__main__":
    unittest.main()
