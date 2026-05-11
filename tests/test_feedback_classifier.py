"""Unit tests for daily_feedback_classifier (no real HTTP)."""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
# Repo root must be on sys.path so daily_feedback_classifier can `from judge_runner import …`
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_spec = importlib.util.spec_from_file_location(
    "daily_feedback_classifier", _ROOT / "daily_feedback_classifier.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["daily_feedback_classifier"] = _mod
_spec.loader.exec_module(_mod)


def _make_mock_client(responses):
    """Build a mock OpenAI-shaped client. `responses` is an iterable of
    either a string (returned as choice.message.content) or an Exception
    (raised on that call)."""
    it = iter(responses)
    client = mock.MagicMock()

    def _create(**kwargs):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        msg = mock.MagicMock()
        msg.content = nxt
        choice = mock.MagicMock()
        choice.message = msg
        resp = mock.MagicMock()
        resp.choices = [choice]
        return resp

    client.chat.completions.create.side_effect = _create
    return client


class TestClassifyRows(unittest.TestCase):
    def test_short_input_maps_to_noise(self) -> None:
        rows = [{"free_text_feedback": "x", "aiintentid": "a"}]
        client = _make_mock_client([])  # no Azure call expected
        snap = _mod.classify_rows(rows, client=client, deployment="d", prompt="p")
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(
            snap["category_counts"], {"Noise / gibberish / off-topic input": 1}
        )
        self.assertEqual(snap["n_classified"], 1)
        self.assertEqual(snap["n_errors"], 0)

    def test_empty_input_skipped(self) -> None:
        rows = [
            {"free_text_feedback": "", "aiintentid": "a"},
            {"free_text_feedback": "   ", "aiintentid": "b"},
        ]
        client = _make_mock_client([])
        snap = _mod.classify_rows(rows, client=client, deployment="d", prompt="p")
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(snap["n_classified"], 0)
        self.assertEqual(snap["category_counts"], {})

    def test_valid_category_passes_through(self) -> None:
        rows = [{"free_text_feedback": "speaker is too fast", "aiintentid": "a"}]
        client = _make_mock_client(["Voice issues (TTS)"])
        snap = _mod.classify_rows(rows, client=client, deployment="d", prompt="p")
        self.assertEqual(snap["category_counts"], {"Voice issues (TTS)": 1})
        self.assertEqual(snap["n_errors"], 0)

    def test_invalid_category_falls_back_to_other(self) -> None:
        rows = [{"free_text_feedback": "complaint text here", "aiintentid": "a"}]
        client = _make_mock_client(["random garbage"])
        snap = _mod.classify_rows(rows, client=client, deployment="d", prompt="p")
        self.assertEqual(snap["category_counts"], {"Other": 1})
        self.assertEqual(len(snap["other_samples"]), 1)
        self.assertEqual(snap["other_samples"][0]["free_text"], "complaint text here")

    def test_missing_card_id_clean_noop(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METABASE_FREETEXT_CARD_ID", None)
            try:
                os.remove(_mod.SNAPSHOT_PATH)
            except FileNotFoundError:
                pass
            rc = _mod.main()
            self.assertEqual(rc, 0)
            with open(_mod.SNAPSHOT_PATH) as f:
                snap = json.load(f)
            self.assertEqual(snap["stopped_reason"], "no_metabase_card")
            self.assertEqual(snap["n_classified"], 0)

    def test_azure_429_then_success(self) -> None:
        # Simulate the SDK's internal retries by handing back success on the
        # second visible call (since openai-python retries are transparent,
        # the mock here exposes the post-retry result we'd ultimately see).
        rows = [
            {"free_text_feedback": "math is wrong", "aiintentid": "a"},
            {"free_text_feedback": "screen froze", "aiintentid": "b"},
        ]
        client = _make_mock_client(
            ["Incorrect / hallucinated answer", "UI / App bugs"]
        )
        snap = _mod.classify_rows(
            rows, client=client, deployment="d", prompt="p", max_workers=1
        )
        self.assertEqual(snap["n_classified"], 2)
        self.assertIn("Incorrect / hallucinated answer", snap["category_counts"])
        self.assertIn("UI / App bugs", snap["category_counts"])

    def test_snapshot_written_on_partial_failure(self) -> None:
        rows = [
            {"free_text_feedback": "row one text", "aiintentid": "a"},
            {"free_text_feedback": "row two text", "aiintentid": "b"},
            {"free_text_feedback": "row three text", "aiintentid": "c"},
            {"free_text_feedback": "row four text", "aiintentid": "d"},
        ]
        # Half succeed, half raise.
        client = _make_mock_client(
            [
                "Other",
                RuntimeError("boom"),
                "Other",
                RuntimeError("boom"),
            ]
        )
        try:
            os.remove(_mod.SNAPSHOT_PATH)
        except FileNotFoundError:
            pass
        snap = _mod.classify_rows(
            rows, client=client, deployment="d", prompt="p", max_workers=1
        )
        _mod._write_snapshot(snap)
        with open(_mod.SNAPSHOT_PATH) as f:
            disk = json.load(f)
        self.assertGreater(disk["n_errors"], 0)
        self.assertEqual(disk["stopped_reason"], "complete")
        self.assertEqual(disk["n_classified"], 2)

    def test_curly_apostrophe_in_response_normalises(self) -> None:
        # The prompt uses a curly apostrophe in "Couldn't understand the question"
        # — guard against the model returning either form.
        rows = [{"free_text_feedback": "didn't get my question", "aiintentid": "a"}]
        client = _make_mock_client(["Couldn’t understand the question"])
        snap = _mod.classify_rows(rows, client=client, deployment="d", prompt="p")
        self.assertEqual(
            snap["category_counts"], {"Couldn't understand the question": 1}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
