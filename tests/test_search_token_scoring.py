import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server import (
    CLAUDE_SOURCE,
    CCPeekHandler,
    _conv_cache,
    _conv_cache_ready,
    _search_cache,
)


class SearchTokenScoringTests(unittest.TestCase):
    def test_opencode_import_filter_handles_nonstandard_session_id_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "mimocode.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    directory TEXT,
                    parent_id TEXT,
                    time_created INTEGER,
                    time_updated INTEGER
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    time_created INTEGER,
                    data TEXT
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY,
                    message_id TEXT,
                    time_created INTEGER,
                    data TEXT
                );
                CREATE TABLE claude_import (
                    imported_session_id TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
                ("ses_imported", "New Session", "C:/proj", None, 1000, 2000),
            )
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                (
                    "msg1",
                    "ses_imported",
                    1000,
                    json.dumps({"role": "assistant", "providerID": "anthropic", "modelID": "claude-opus-4-6"}),
                ),
            )
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                ("msg2", "ses_imported", 1001, json.dumps({"role": "user"})),
            )
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?)",
                ("part1", "msg2", 1001, json.dumps({"type": "text", "text": "Thanks for the call today!"})),
            )
            conn.execute(
                "INSERT INTO claude_import VALUES (?)",
                ("ses_imported",),
            )
            conn.commit()
            conn.close()

            results = []
            CCPeekHandler._read_opencode_db(
                str(db_path), "MiMoCode", "mimo", "mimocode", results
            )

        self.assertEqual(results, [])

    def test_search_cache_uses_live_file_stats_before_conv_cache_refresh(self):
        original_conv_cache = dict(_conv_cache)
        original_search_cache = dict(_search_cache)
        ready_was_set = _conv_cache_ready.is_set()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.jsonl"
            conv_id = "claude:test-conv"

            def write_rows(rows):
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )

            write_rows([
                {
                    "type": "assistant",
                    "timestamp": "2026-06-08T00:00:01Z",
                    "message": {"content": "first arrows"},
                },
            ])

            try:
                _conv_cache.clear()
                _search_cache.clear()
                _conv_cache_ready.set()
                _conv_cache[conv_id] = {
                    "id": conv_id,
                    "source": CLAUDE_SOURCE,
                    "path": str(path),
                    "modified": 0,
                    "size": 0,
                    "is_internal": False,
                }

                CCPeekHandler._build_search_cache()
                self.assertEqual(_search_cache[conv_id]["text_lower"].count("arrows"), 1)

                old_mtime = path.stat().st_mtime
                write_rows([
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-08T00:00:01Z",
                        "message": {"content": "first arrows"},
                    },
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-08T00:00:02Z",
                        "message": {"content": "second arrows"},
                    },
                ])
                os.utime(path, (old_mtime + 2, old_mtime + 2))

                CCPeekHandler._build_search_cache()
                self.assertEqual(_search_cache[conv_id]["text_lower"].count("arrows"), 2)
            finally:
                _conv_cache.clear()
                _conv_cache.update(original_conv_cache)
                _search_cache.clear()
                _search_cache.update(original_search_cache)
                if ready_was_set:
                    _conv_cache_ready.set()
                else:
                    _conv_cache_ready.clear()

    def test_search_parts_ignore_tool_events(self):
        events = [
            {"kind": "message", "text": "visible text"},
            {"kind": "commentary", "text": "commentary text"},
            {"kind": "tool_use", "name": "search", "payload": {"q": "arrows"}},
            {"kind": "tool_result", "payload": "tool output"},
            {"kind": "thinking", "text": "thinking text"},
            {"kind": "message", "text": "hidden text", "hidden_by_default": True},
        ]

        text_parts, thinking_parts = CCPeekHandler._events_to_search_parts(events)

        self.assertEqual(text_parts, ["visible text", "commentary text"])
        self.assertEqual(thinking_parts, ["thinking text"])

    def test_all_terms_fallback_scores_token_presence_not_repetition(self):
        handler = object.__new__(CCPeekHandler)
        patterns = CCPeekHandler._build_token_patterns("main line of the pr")

        result = handler._token_search(
            "main main main line of the pr pr pr",
            patterns,
        )

        self.assertEqual(result, (5, 5))

    def test_plain_word_match_requires_boundaries(self):
        self.assertTrue(CCPeekHandler._has_plain_word_match("main pr line", "pr"))
        self.assertFalse(CCPeekHandler._has_plain_word_match("main prompt line", "pr"))

    def test_search_request_sequence_marks_older_requests_stale(self):
        CCPeekHandler._note_search_request("client-a", 2)

        self.assertTrue(CCPeekHandler._is_search_request_stale("client-a", 1))
        self.assertFalse(CCPeekHandler._is_search_request_stale("client-a", 2))
        self.assertFalse(CCPeekHandler._is_search_request_stale("client-a", 3))
