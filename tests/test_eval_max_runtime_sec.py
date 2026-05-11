"""Regression tests for `_eval_max_runtime_sec()` in daily_eval.py.

Context: cleanup-mess/eval-env-tuning changed the workflow default from
`EVAL_MAX_RUNTIME_SEC="14400"` (4h soft cap) to `EVAL_MAX_RUNTIME_SEC="0"`
(no soft cap; run to completion, with the job's `timeout-minutes: 600` as
runaway backstop). The Python env-reader must therefore treat "0" — and
any non-positive value — as "no cap" (None), the same as a missing/blank
env var.

These tests pin the contract so we don't regress to interpreting "0" as
"60s cap" (the old `max(60.0, float(raw))` behaviour would have produced
a 1-minute cap for raw="0" and silently broken every scheduled run).
"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _read_with_env(value):
    """Set env var (or unset for None) and call `_eval_max_runtime_sec()`."""
    env = dict(os.environ)
    if value is None:
        env.pop("EVAL_MAX_RUNTIME_SEC", None)
    else:
        env["EVAL_MAX_RUNTIME_SEC"] = value
    with mock.patch.dict(os.environ, env, clear=True):
        import daily_eval

        importlib.reload(daily_eval)
        return daily_eval._eval_max_runtime_sec()


class TestEvalMaxRuntimeSec(unittest.TestCase):
    """`_eval_max_runtime_sec()` returns float (cap in seconds) or None (no cap)."""

    def test_missing_env_returns_none(self):
        self.assertIsNone(_read_with_env(None))

    def test_blank_returns_none(self):
        self.assertIsNone(_read_with_env(""))
        self.assertIsNone(_read_with_env("   "))

    def test_zero_returns_none_no_cap(self):
        """'0' is the new workflow default — must mean 'no soft cap'."""
        self.assertIsNone(_read_with_env("0"))
        self.assertIsNone(_read_with_env("0.0"))

    def test_negative_returns_none_no_cap(self):
        """Defensive: any non-positive value is treated as 'no cap'."""
        self.assertIsNone(_read_with_env("-1"))
        self.assertIsNone(_read_with_env("-3600"))

    def test_unparseable_returns_none(self):
        """Invalid strings fall back to 'no cap' rather than crashing the run."""
        self.assertIsNone(_read_with_env("abc"))
        self.assertIsNone(_read_with_env("4h"))

    def test_positive_returns_float_seconds(self):
        """Regression: '14400' (the previous default) must still produce a 4h cap."""
        self.assertEqual(_read_with_env("14400"), 14400.0)
        self.assertEqual(_read_with_env("3600"), 3600.0)

    def test_small_positive_is_floored_at_60s(self):
        """Tiny positive values are floored to 60s to avoid pathological budgets."""
        self.assertEqual(_read_with_env("1"), 60.0)
        self.assertEqual(_read_with_env("30"), 60.0)
        self.assertEqual(_read_with_env("60"), 60.0)
        self.assertEqual(_read_with_env("61"), 61.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
