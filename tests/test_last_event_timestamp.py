import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from server import CCPeekHandler


def epoch(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


class LastEventTimestampTests(unittest.TestCase):
    def write(self, tmp, lines, name='session.jsonl'):
        path = Path(tmp) / name
        path.write_text('\n'.join(json.dumps(l) for l in lines) + '\n', encoding='utf-8')
        return str(path)

    def test_returns_last_timestamped_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [
                {'type': 'user', 'timestamp': '2026-08-01T10:00:00.000Z'},
                {'type': 'assistant', 'timestamp': '2026-08-01T10:05:00.000Z'},
                {'type': 'assistant', 'timestamp': '2026-08-01T12:30:15.500Z'},
            ])
            self.assertEqual(
                CCPeekHandler._last_event_timestamp(path),
                epoch('2026-08-01T12:30:15.500Z'))

    def test_skips_trailing_lines_without_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [
                {'type': 'user', 'timestamp': '2026-08-01T10:00:00.000Z'},
                {'type': 'file-history-snapshot', 'snapshot': {}},
                {'type': 'summary', 'summary': 'done'},
            ])
            self.assertEqual(
                CCPeekHandler._last_event_timestamp(path),
                epoch('2026-08-01T10:00:00.000Z'))

    def test_grows_the_tail_window_past_huge_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [
                {'type': 'user', 'timestamp': '2026-08-01T09:00:00.000Z'},
                {'type': 'tool_result', 'text': 'x' * 300000},
            ])
            self.assertGreater(os.path.getsize(path), 65536)
            self.assertEqual(
                CCPeekHandler._last_event_timestamp(path),
                epoch('2026-08-01T09:00:00.000Z'))

    def test_returns_none_without_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [{'type': 'summary'}, {'type': 'summary'}])
            self.assertIsNone(CCPeekHandler._last_event_timestamp(path))

    def test_returns_none_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                CCPeekHandler._last_event_timestamp(str(Path(tmp) / 'nope.jsonl')))

    def test_ignores_unparsable_and_non_iso_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(
                json.dumps({'timestamp': '2026-08-01T08:00:00.000Z'}) + '\n'
                + json.dumps({'timestamp': 'not-a-date'}) + '\n'
                + '{ truncated\n',
                encoding='utf-8')
            self.assertEqual(
                CCPeekHandler._last_event_timestamp(str(path)),
                epoch('2026-08-01T08:00:00.000Z'))


if __name__ == '__main__':
    unittest.main()
