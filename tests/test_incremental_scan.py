import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from server import CCPeekHandler, CLAUDE_SOURCE, CODEX_SOURCE


def epoch(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


def claude_user(text, ts, entrypoint='cli'):
    return {'type': 'user', 'timestamp': ts, 'entrypoint': entrypoint,
            'message': {'content': [{'type': 'text', 'text': text}]}}


def claude_assistant(text, ts, model='claude-opus-5'):
    return {'type': 'assistant', 'timestamp': ts,
            'message': {'model': model,
                        'content': [{'type': 'text', 'text': text}]}}


def codex_user(text, ts):
    return {'type': 'response_item', 'timestamp': ts,
            'payload': {'type': 'message', 'role': 'user',
                        'content': [{'type': 'input_text', 'text': text}]}}


def write(path, events, mode='w'):
    with open(path, mode, encoding='utf-8', newline='\n') as f:
        for e in events:
            f.write(json.dumps(e) + '\n')


class IncrementalScanTests(unittest.TestCase):
    """A delta refresh must land on the same entry a full re-parse would."""

    def claude_entry(self, tmp, path):
        return CCPeekHandler._read_claude_metadata(str(path), tmp, {})

    def test_claude_delta_matches_full_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            write(path, [
                claude_user('first ask', '2026-08-01T10:00:00.000Z'),
                claude_assistant('working', '2026-08-01T10:00:05.000Z'),
            ])
            before = self.claude_entry(tmp, path)
            self.assertEqual(before['title'], 'first ask')

            write(path, [
                claude_user('second ask', '2026-08-01T11:00:00.000Z'),
                claude_assistant('done', '2026-08-01T11:00:09.000Z'),
            ], mode='a')

            stats = os.stat(path)
            delta = CCPeekHandler._refresh_from_append(before, str(path), stats)
            full = self.claude_entry(tmp, path)

            self.assertIsNotNone(delta)
            for key in ('id', 'title', 'modified', 'size', 'model',
                        'entrypoint', 'project_dir', 'timestamp'):
                self.assertEqual(delta[key], full[key], key)
            self.assertEqual(delta['title'], 'second ask')
            self.assertEqual(delta['modified'], epoch('2026-08-01T11:00:09.000Z'))
            self.assertEqual(delta['_scan_offset'], stats.st_size)

    def test_codex_delta_matches_full_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout-1.jsonl'
            write(path, [
                {'type': 'session_meta', 'timestamp': '2026-08-01T10:00:00.000Z',
                 'payload': {'id': 'sess-1', 'cwd': 'C:/repo'}},
                {'type': 'turn_context', 'timestamp': '2026-08-01T10:00:01.000Z',
                 'payload': {'model': 'gpt-5'}},
                codex_user('first ask', '2026-08-01T10:00:02.000Z'),
            ])
            before = CCPeekHandler._read_codex_metadata(str(path), {})
            self.assertEqual(before['title'], 'first ask')

            write(path, [codex_user('second ask', '2026-08-01T12:00:00.000Z')],
                  mode='a')

            stats = os.stat(path)
            delta = CCPeekHandler._refresh_from_append(before, str(path), stats)
            full = CCPeekHandler._read_codex_metadata(str(path), {})

            self.assertIsNotNone(delta)
            for key in ('id', 'title', 'modified', 'size', 'model', 'project_dir'):
                self.assertEqual(delta[key], full[key], key)
            self.assertEqual(delta['title'], 'second ask')

    def test_appends_without_a_user_message_keep_the_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            write(path, [
                claude_user('only ask', '2026-08-01T10:00:00.000Z'),
                claude_assistant('ok', '2026-08-01T10:00:02.000Z'),
            ])
            before = self.claude_entry(tmp, path)

            write(path, [claude_assistant('still going', '2026-08-01T10:30:00.000Z')],
                  mode='a')
            delta = CCPeekHandler._refresh_from_append(
                before, str(path), os.stat(path))

            self.assertEqual(delta['title'], 'only ask')
            self.assertEqual(delta['modified'], epoch('2026-08-01T10:30:00.000Z'))

    def test_partial_trailing_line_is_left_for_the_next_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            write(path, [
                claude_user('only ask', '2026-08-01T10:00:00.000Z'),
                claude_assistant('ok', '2026-08-01T10:00:02.000Z'),
            ])
            before = self.claude_entry(tmp, path)

            with open(path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(
                    claude_user('half written', '2026-08-01T11:00:00.000Z'))[:40])
            delta = CCPeekHandler._refresh_from_append(
                before, str(path), os.stat(path))
            self.assertIsNone(delta)

            with open(path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(
                    claude_user('half written', '2026-08-01T11:00:00.000Z'))[40:] + '\n')
            delta = CCPeekHandler._refresh_from_append(
                before, str(path), os.stat(path))
            self.assertEqual(delta['title'], 'half written')

    def test_truncated_file_forces_a_full_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            write(path, [
                claude_user('first ask', '2026-08-01T10:00:00.000Z'),
                claude_user('second ask', '2026-08-01T10:10:00.000Z'),
            ])
            before = self.claude_entry(tmp, path)
            write(path, [claude_user('rewritten', '2026-08-01T10:20:00.000Z')])
            self.assertIsNone(CCPeekHandler._refresh_from_append(
                before, str(path), os.stat(path)))

    def test_unresolved_head_fields_force_a_full_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            # no assistant line yet, so model is still unknown
            write(path, [claude_user('only ask', '2026-08-01T10:00:00.000Z')])
            before = self.claude_entry(tmp, path)
            self.assertEqual(before['model'], '')
            write(path, [claude_assistant('ok', '2026-08-01T10:05:00.000Z')],
                  mode='a')
            self.assertIsNone(CCPeekHandler._refresh_from_append(
                before, str(path), os.stat(path)))

    def test_entry_without_scan_offset_forces_a_full_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            write(path, [
                claude_user('only ask', '2026-08-01T10:00:00.000Z'),
                claude_assistant('ok', '2026-08-01T10:00:02.000Z'),
            ])
            before = self.claude_entry(tmp, path)
            before.pop('_scan_offset')
            write(path, [claude_user('later ask', '2026-08-01T11:00:00.000Z')],
                  mode='a')
            self.assertIsNone(CCPeekHandler._refresh_from_append(
                before, str(path), os.stat(path)))

    def test_scan_offset_skips_a_trailing_partial_line_on_full_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / '-c--repo'
            proj.mkdir()
            path = proj / 'abc.jsonl'
            write(path, [claude_user('only ask', '2026-08-01T10:00:00.000Z')])
            complete = os.path.getsize(path)
            with open(path, 'a', encoding='utf-8', newline='\n') as f:
                f.write('{"type":"user","timesta')
            entry = self.claude_entry(tmp, path)
            self.assertEqual(entry['_scan_offset'], complete)
            self.assertEqual(entry['source'], CLAUDE_SOURCE)


if __name__ == '__main__':
    unittest.main()
