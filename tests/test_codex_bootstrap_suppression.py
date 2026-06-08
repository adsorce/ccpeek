import json
import tempfile
import unittest
from pathlib import Path

from server import CCPeekHandler


class CodexBootstrapSuppressionTests(unittest.TestCase):
    def test_first_agents_bootstrap_message_is_hidden_and_skipped_for_title(self):
        bootstrap = (
            "# AGENTS.md instructions for C:\\Dropbox\\Projects\\ccpeek\n\n"
            "<INSTRUCTIONS>\nrule\n</INSTRUCTIONS>\n"
            "<environment_context>\nctx\n</environment_context>"
        )
        visible = "Actual user request"
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "rollout-test.jsonl"
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "session-1",
                        "cwd": "C:\\Dropbox\\Projects\\ccpeek",
                        "timestamp": "2026-06-08T00:00:00Z",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-06-08T00:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": bootstrap}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-06-08T00:00:02Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": visible}],
                    },
                },
            ]
            session_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            entry = CCPeekHandler._read_codex_metadata(str(session_path), {})
            events = CCPeekHandler._load_codex_events(str(session_path))

        self.assertEqual(entry["title"], visible)
        self.assertTrue(events[0]["hidden_by_default"])
        self.assertEqual(events[0]["text"], bootstrap)
        self.assertFalse(events[1]["hidden_by_default"])
        self.assertEqual(events[1]["text"], visible)
