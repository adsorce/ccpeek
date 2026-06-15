import sqlite3
import tempfile
import unittest
from pathlib import Path

from server import CCPeekHandler, MIMOCODE_SOURCE


class OpenCodeImportFilterTests(unittest.TestCase):
    def test_read_opencode_db_skips_legacy_and_external_claude_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'mimocode.db'
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.executescript("""
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
                    message_id TEXT,
                    time_created INTEGER,
                    data TEXT
                );
                CREATE TABLE claude_import (
                    source_uuid TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_mtime INTEGER NOT NULL,
                    time_imported INTEGER NOT NULL,
                    message_ids TEXT
                );
                CREATE TABLE external_import (
                    source TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_mtime INTEGER NOT NULL,
                    time_imported INTEGER NOT NULL,
                    message_ids TEXT
                );
            """)
            cur.executemany(
                "INSERT INTO session (id, title, directory, parent_id, time_created, time_updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ('native-session', 'Native chat', 'C:\\work', None, 1, 3),
                    ('legacy-import', 'Legacy Claude import', 'C:\\work', None, 1, 2),
                    ('external-import', 'External Claude import', 'C:\\work', None, 1, 1),
                ],
            )
            cur.execute(
                "INSERT INTO claude_import "
                "(source_uuid, session_id, source_path, source_mtime, time_imported, message_ids) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ('claude-1', 'legacy-import', 'legacy.jsonl', 1, 1, '[]'),
            )
            cur.execute(
                "INSERT INTO external_import "
                "(source, source_key, session_id, source_path, source_mtime, time_imported, message_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ('cc', 'claude-2', 'external-import', 'external.jsonl', 1, 1, '[]'),
            )
            conn.commit()
            conn.close()

            results = []
            CCPeekHandler._read_opencode_db(
                str(db_path), 'MiMoCode', 'mimo', MIMOCODE_SOURCE, results)

        self.assertEqual([entry['source_id'] for entry in results], ['native-session'])
