"""Post-RCA regression tests: daily_eval.main() actually exercises the
poster pipeline by default, and falls back to the legacy text post only
under the three explicit failure modes.

These are the tests that should have caught the silent gate fall-through
on workflow_dispatch run #26517069840 (the gate was opt-in, never
exported, so main() silently took the legacy path on the first
dogfood). With the gate inverted (POSTER_DISABLE=1 kill-switch), the
poster path is the default and these tests pin that behaviour.

Strategy: drive main() with `--samples /tmp/...json` so it skips the
Metabase fetch, mock the judge loop + finalize step + Langfuse cleanup
+ the idempotency marker check, then patch the two Slack-facing
functions and assert on which one is called.
"""
from __future__ import annotations

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
    """Import-or-reuse: if already in sys.modules (because another test
    imported the same path earlier and patched it), use that one so all
    our `mock.patch.object` patches target the same module object that
    `from X import Y` inside main() will reach via sys.modules."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
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


def _write_samples(tmpdir: str) -> str:
    path = os.path.join(tmpdir, "samples.json")
    with open(path, "w") as fh:
        json.dump([{"trace_id": "t1", "stratum": "all"}], fh)
    return path


def _stub_snapshot(path: str) -> None:
    with open(path, "w") as fh:
        json.dump(
            {
                "date": "2026-05-26",
                "n_judged": 1,
                "n_sampled": 1,
                "acc_fail_pct": 4.2,
                "exp_fail_pct": 11.0,
                "pass_pct": 81.0,
                "axial_fail_pct": {"A5": 4.2, "A2": 1.1, "A1": 1.0},
            },
            fh,
        )


class _FakeJudgeOutcome:
    def __init__(self):
        self.new_results = []
        self.stopped_reason = "complete"


class _BasePosterMainTest(unittest.TestCase):
    """Shared scaffolding: stub the eval pipeline so main() reaches the
    Slack-post branch without doing real I/O."""

    def setUp(self) -> None:
        self.eval_mod = _import("daily_eval", "daily_eval.py")
        self.ps = _import("scripts.poster_slack", "scripts/poster_slack.py")
        self.tmpdir = tempfile.mkdtemp(prefix="eval_main_test_")
        self.samples_path = _write_samples(self.tmpdir)
        self.snapshot_path = "/tmp/daily_eval_yesterday_summary.json"
        _stub_snapshot(self.snapshot_path)
        # argv: prepend "daily_eval" so argparse sees argv[0] as script
        self._argv = sys.argv
        sys.argv = [
            "daily_eval",
            "--samples", self.samples_path,
            "--no-write-scores",
            "--output", os.path.join(self.tmpdir, "results.json"),
            "--label", "test-label",
        ]

    def tearDown(self) -> None:
        sys.argv = self._argv
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_main(self, env_overrides: dict, *, render_side_effect=None,
                  publish_returns=True, render_returns_url="https://example/p.png"):
        """Patch everything heavy, run main(), return (exit_code, m_render,
        m_post_blocks, m_post_text, stderr_buf)."""
        outcome = _FakeJudgeOutcome()
        block_text = "DAILY EVAL\nacc_fail 4.2%"

        # Capture stderr so tests can assert on the [warn] reason line.
        from io import StringIO
        stderr_buf = StringIO()

        m_render = mock.MagicMock(return_value=render_returns_url)
        if render_side_effect is not None:
            m_render.side_effect = render_side_effect
        m_post_blocks = mock.MagicMock(return_value=publish_returns)
        m_post_text = mock.MagicMock(return_value=True)

        env = {
            "SLACK_WEBHOOK_URL": "https://hooks.slack.test/x",
            "GITHUB_ACTIONS": "true",
            "POSTER_DISABLE": None,
            "POSTER_DRY_RUN": None,
            "FORCE_REPOST": "1",
            # Metabase env not needed because we use --samples.
        }
        env.update(env_overrides)

        with _EnvScope(**env), \
             mock.patch.object(self.eval_mod, "run_judge_loop",
                               return_value=outcome), \
             mock.patch.object(self.eval_mod, "finalize_eval_run",
                               return_value=block_text), \
             mock.patch.object(self.eval_mod, "_eval_already_posted_today",
                               return_value=False), \
             mock.patch.object(self.eval_mod, "_eval_write_posted_marker"), \
             mock.patch.object(self.ps, "render_and_publish", m_render), \
             mock.patch.object(self.ps, "post_blocks_to_slack", m_post_blocks), \
             mock.patch.object(self.eval_mod, "post_to_slack", m_post_text), \
             mock.patch("time.sleep"), \
             mock.patch("sys.stderr", stderr_buf):
            exit_code = self.eval_mod.main()
        return exit_code, m_render, m_post_blocks, m_post_text, stderr_buf.getvalue()


class TestEvalMainPosterPathByDefault(_BasePosterMainTest):

    # REGRESSION: any new opt-in gate keyed on an undefined env var must break
    # this assertion. The whole point of the gate inversion was that the poster
    # path is the DEFAULT and only POSTER_DISABLE=1 (or a render/publish/post
    # failure) takes the legacy path.
    def test_main_runs_poster_path_by_default(self) -> None:
        """No POSTER_DISABLE in env: poster pipeline fires, legacy stays untouched."""
        exit_code, m_render, m_post_blocks, m_post_text, _ = self._run_main({})
        self.assertEqual(exit_code, 0)
        self.assertEqual(m_render.call_count, 1)
        # Main + thread reply = at least 2 block posts.
        self.assertGreaterEqual(m_post_blocks.call_count, 2)
        m_post_text.assert_not_called()

    def test_main_falls_back_when_POSTER_DISABLE_set(self) -> None:
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {"POSTER_DISABLE": "1"}
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_not_called()
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        # First positional arg after webhook is the text block.
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(
            sent_text.startswith("⚠️ Poster degraded (see workflow logs)"),
            f"missing degradation marker: {sent_text[:80]!r}",
        )
        self.assertIn("cause=disabled", stderr)

    def test_main_falls_back_on_render_failure(self) -> None:
        from scripts.poster_renderer import PosterRenderError
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {}, render_side_effect=PosterRenderError("scoreboard", "boom"),
        )
        self.assertEqual(exit_code, 0)
        m_render.assert_called_once()
        # No main block post should have fired (render returned None / raised).
        m_post_blocks.assert_not_called()
        m_post_text.assert_called_once()
        sent_text = m_post_text.call_args.args[1]
        self.assertTrue(sent_text.startswith("⚠️ Poster degraded"))
        self.assertIn("cause=render", stderr)

    def test_programming_error_propagates_not_swallowed(self) -> None:
        """B1: a NameError from render_and_publish is NOT recoverable;
        it must propagate so CI surfaces the bug red. Catching `Exception`
        would have silently degraded to the legacy fallback and hidden it."""
        with self.assertRaises(NameError):
            self._run_main({}, render_side_effect=NameError("oops"))

    def test_main_falls_back_on_publish_unreachable(self) -> None:
        """SRE item 4: render returns a URL but the verify probe says it is
        not reachable -> caller degrades to legacy with cause=publish_unreachable."""
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
        # The cause string still bucketed under render in the top-level handler
        # because PosterPublishUnreachableError surfaces out of render_and_publish.
        # That is acceptable: the reason= field contains the precise message,
        # and the structured-JSON degradation signal (follow-up #16) will pick
        # the cause apart further.
        self.assertIn("cause=render", stderr)
        self.assertIn("gh-pages URL not reachable", stderr)

    def test_main_falls_back_on_publish_failure(self) -> None:
        # render succeeds, but post_blocks_to_slack returns False.
        exit_code, m_render, m_post_blocks, m_post_text, stderr = self._run_main(
            {}, publish_returns=False,
        )
        # publish_returns=False -> post_blocks_to_slack(False) means
        # posted=False -> fallback fires. Plus the warn line should land.
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
