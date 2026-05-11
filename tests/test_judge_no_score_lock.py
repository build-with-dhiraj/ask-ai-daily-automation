"""Regression guard: ensure no global mutex serializes Langfuse score writes.

A previous `_LANGFUSE_SCORE_WRITE_LOCK = threading.Lock()` in `judge_runner.py`
wrapped the per-sample `lf.create_score()` burst (14 calls per sample) inside a
`with` block. That serialized ALL judge workers through a single mutex, capping
throughput at ~1x regardless of `JUDGE_CONCURRENCY`.

The Langfuse SDK v3.7.0 already uses a thread-safe non-blocking queue inside
`create_score()` (`self._score_ingestion_queue.put(event, block=False)` in
`langfuse/_client/resource_manager.py`), so the outer lock provided zero
additional safety while being the dominant bottleneck of the eval.

This test fails if anyone reintroduces the lock.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("judge_runner", _ROOT / "judge_runner.py")
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["judge_runner"] = _mod
_spec.loader.exec_module(_mod)


class TestNoScoreWriteLock(unittest.TestCase):
    def test_score_write_lock_is_not_present(self) -> None:
        self.assertFalse(
            hasattr(_mod, "_LANGFUSE_SCORE_WRITE_LOCK"),
            "_LANGFUSE_SCORE_WRITE_LOCK was reintroduced; it serializes all "
            "concurrent score writes and caps eval throughput. The Langfuse "
            "SDK's create_score() already uses a thread-safe queue.",
        )


if __name__ == "__main__":
    unittest.main()
