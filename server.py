#!/usr/bin/env python3
import argparse
import os
import json
import glob
import webbrowser
import threading
import time
import subprocess
import shutil
import re
import hashlib
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse, unquote
from pathlib import Path
import socket
import sys

DEFAULT_PORT = 8888
DEFAULT_HOST = '0.0.0.0'
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}
SETUP_MARKER = os.path.expanduser('~/.config/ccpeek/.setup-done')
UNIT_PATH = os.path.expanduser('~/.config/systemd/user/ccpeek.service')
TASK_NAME = 'CCPeek'
CLAUDE_SOURCE = 'claude'
CODEX_SOURCE = 'codex'
CACHE_REFRESH_INTERVAL = 30

_RE_MD_FENCED = re.compile(r'```[^\n]*\n(.*?)```', re.DOTALL)
_RE_MD_INLINE = re.compile(r'`([^`]+)`')
_RE_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_RE_MD_EMPH = re.compile(r'(?<!\w)(\*{1,3}|_{1,3}|~~)(.*?)\1(?!\w)')
_META_TAGS = ('local-command-caveat', 'command-name', 'command-message', 'command-args',
              'local-command-stdout', 'local-command-stderr', 'system-reminder')
_RE_META_TAGS = re.compile(
    r'<(?:' + '|'.join(re.escape(t) for t in _META_TAGS) +
    r')[^>]*>.*?</(?:' + '|'.join(re.escape(t) for t in _META_TAGS) +
    r')>\s*', re.DOTALL)
_RE_CODEX_AGENTS_BOOTSTRAP = re.compile(
    r'^# AGENTS\.md instructions for .+?\r?\n\r?\n<INSTRUCTIONS>\r?\n[\s\S]*?\r?\n</INSTRUCTIONS>'
    r'(?:\s*<environment_context>[\s\S]*?</environment_context>)?\s*$'
)

_search_cache = {}
_search_cache_lock = threading.Lock()
_search_request_state = {}
_search_request_state_lock = threading.Lock()
_conv_cache = {}
_conv_cache_lock = threading.Lock()
_conv_cache_ready = threading.Event()

def extract_first_text(content, max_len=200):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = ''
        for item in content:
            if isinstance(item, str):
                text = item
                break
            if isinstance(item, dict) and item.get('type') == 'text':
                text = item.get('text', '')
                break
    else:
        return ''
    if not text:
        return ''
    if max_len and len(text) > max_len:
        return text[:max_len] + '...'
    return text

class CCPeekServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = False
    daemon_threads = True

class CCPeekHandler(SimpleHTTPRequestHandler):
    _path_cache = {}

    def _json_response(self, data, status=200, headers=None):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        if data is not None:
            self.wfile.write(json.dumps(data).encode())

    @staticmethod
    def _etag_for_json(data):
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
        return '"' + hashlib.md5(payload).hexdigest() + '"'

    def do_GET(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'rb') as f:
                self.wfile.write(f.read())
        elif parsed_path.path == '/api/health':
            self._json_response({"service": "ccpeek", "version": "1.0"})
        elif parsed_path.path == '/api/conversations':
            query_params = parse_qs(parsed_path.query)
            include_internal = query_params.get('include_internal', ['false'])[0].lower() == 'true'
            self.handle_conversations(include_internal)
        elif parsed_path.path.startswith('/api/conversation/'):
            conversation_id = parsed_path.path.split('/')[-1]
            query_params = parse_qs(parsed_path.query)
            include_internal = query_params.get('include_internal', ['false'])[0].lower() == 'true'
            self.handle_conversation(conversation_id, include_internal)
        elif parsed_path.path == '/api/search':
            query_params = parse_qs(parsed_path.query)
            search_term = query_params.get('q', [''])[0]
            include_thinking = query_params.get('include_thinking', ['false'])[0].lower() == 'true'
            client_id = query_params.get('client_id', [''])[0]
            request_seq = query_params.get('request_seq', [''])[0]
            self.handle_search(
                unquote(search_term),
                include_thinking,
                client_id=client_id,
                request_seq=request_seq)
        else:
            super().do_GET()

    _INTERNAL_PREFIXES = ('<local-command-', '<command-message>', '<command-name>')

    @staticmethod
    def _extract_title_from_line(data, current_title=None):
        if current_title is not None:
            return current_title
        if data.get('type') == 'user' and data.get('message'):
            raw = extract_first_text(data['message'].get('content', ''), max_len=0)
            if raw:
                stripped = _RE_META_TAGS.sub('', raw).strip()
                if stripped:
                    return stripped[:200] + ('...' if len(stripped) > 200 else '')
        return None

    @staticmethod
    def _decode_project_dir(encoded):
        """Decode a Claude project directory name back to the original path.

        Claude encodes paths as: drive--segment-segment (on Windows)
        or segment-segment (on Unix), replacing both path separators
        and certain characters with hyphens.  The encoding is lossy,
        so we verify the result exists on disk.
        """
        cached = CCPeekHandler._path_cache.get(encoded)
        if cached:
            return cached

        sep = os.sep
        major = encoded.split('--', 1)
        if len(major) == 2 and len(major[0]) == 1 and major[0].isalpha():
            prefix = major[0].upper() + ':' + sep
            rest = major[1]
        else:
            prefix = sep
            rest = encoded

        def encode_part(name):
            return re.sub(r'[^A-Za-z0-9]', '-', name)

        def resolve(current_dir, remainder):
            if not remainder:
                return (current_dir if os.path.isdir(current_dir) else None), True
            try:
                entries = []
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_dir():
                                encoded_name = encode_part(entry.name)
                                if encoded_name:
                                    entries.append((encoded_name, entry.path))
                        except OSError:
                            continue
            except OSError:
                return os.path.join(current_dir, remainder), False

            # Prefer longer component matches so names like "basedin.nyc"
            # win over partial matches like "basedin".
            entries.sort(key=lambda item: len(item[0]), reverse=True)

            best_fallback = None
            for encoded_name, entry_path in entries:
                if remainder == encoded_name:
                    return entry_path, True
                prefix_match = encoded_name + '-'
                if remainder.startswith(prefix_match):
                    resolved, exact = resolve(entry_path, remainder[len(prefix_match):])
                    if exact:
                        return resolved, True
                    if best_fallback is None and '-' not in encoded_name:
                        best_fallback = resolved

            # If the exact path no longer exists, keep the unresolved tail as a
            # single component instead of expanding every hyphen into a separator.
            if best_fallback is not None:
                return best_fallback, False
            return os.path.join(current_dir, remainder), False

        resolved, _ = resolve(prefix, rest) if rest else (prefix, True)
        if resolved:
            CCPeekHandler._path_cache[encoded] = resolved
            return resolved

        fallback = prefix + rest.replace('-', sep)
        CCPeekHandler._path_cache[encoded] = fallback
        return fallback

    @staticmethod
    def _make_conversation_id(source, raw_id):
        return f'{source}:{raw_id}'

    @staticmethod
    def _split_conversation_id(conversation_id):
        if ':' not in conversation_id:
            return None, None
        source, raw_id = conversation_id.split(':', 1)
        if source not in {CLAUDE_SOURCE, CODEX_SOURCE} or not raw_id:
            return None, None
        return source, raw_id

    @staticmethod
    def _is_local_command_text(text):
        return (
            isinstance(text, str) and (
                '<command-name>' in text
                or '<local-command-stdout>' in text
                or 'Caveat: The messages below were generated by the user while running local commands' in text
            )
        )

    @staticmethod
    def _serialize_payload(payload):
        if payload is None:
            return ''
        if isinstance(payload, str):
            return payload
        try:
            return json.dumps(payload, ensure_ascii=False)
        except TypeError:
            return str(payload)

    @staticmethod
    def _try_parse_json(text):
        if not isinstance(text, str):
            return text
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return text

    @staticmethod
    def _is_codex_agents_bootstrap_message(text):
        return isinstance(text, str) and bool(
            _RE_CODEX_AGENTS_BOOTSTRAP.match(text.strip()))

    @staticmethod
    def _truncate_title(text, fallback='Untitled Conversation'):
        text = (text or '').strip()
        if not text:
            return fallback
        return text[:200] + ('...' if len(text) > 200 else '')

    def _load_job_sessions(self):
        """Load background job metadata, keyed by sessionId."""
        jobs_dir = os.path.expanduser('~/.claude/jobs')
        job_sessions = {}
        if os.path.exists(jobs_dir):
            for entry in os.scandir(jobs_dir):
                if entry.is_dir():
                    state_path = os.path.join(entry.path, 'state.json')
                    if os.path.exists(state_path):
                        try:
                            with open(state_path, 'r', encoding='utf-8') as f:
                                state = json.load(f)
                            sid = state.get('sessionId')
                            if sid:
                                job_sessions[sid] = state
                        except Exception:
                            pass
        return job_sessions

    @staticmethod
    def _read_claude_metadata(jsonl_file, claude_dir, project_dir_cache):
        """Read Claude conversation metadata from a JSONL file."""
        with open(jsonl_file, 'r', encoding='utf-8', errors='replace') as f:
            first_line = f.readline()
            if not first_line:
                return None
            data = json.loads(first_line)

            stats = os.stat(jsonl_file)

            f.seek(0)
            title = "Untitled Conversation"
            for i, line in enumerate(f):
                if i >= 50:
                    break
                try:
                    msg_data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                found = CCPeekHandler._extract_title_from_line(msg_data)
                if found:
                    title = found
                    break

            rel = os.path.relpath(jsonl_file, claude_dir)
            encoded = rel.split(os.sep)[0]
            if encoded not in project_dir_cache:
                project_dir_cache[encoded] = CCPeekHandler._decode_project_dir(encoded)
            project_dir = project_dir_cache[encoded]

            rel_parts = rel.split(os.sep)
            parent_id = None
            if 'subagents' in rel_parts:
                si = rel_parts.index('subagents')
                if si > 1:
                    parent_id = CCPeekHandler._make_conversation_id(
                        CLAUDE_SOURCE, rel_parts[si - 1])

            source_id = os.path.basename(jsonl_file).replace('.jsonl', '')
            conv_id = CCPeekHandler._make_conversation_id(CLAUDE_SOURCE, source_id)
            is_internal = CCPeekHandler._is_internal_thread(jsonl_file, title)

            return {
                'id': conv_id,
                'source': CLAUDE_SOURCE,
                'source_id': source_id,
                'path': jsonl_file,
                'project_dir': project_dir,
                'parent_id': parent_id,
                'title': title,
                'is_internal': is_internal,
                'timestamp': data.get('timestamp', ''),
                'modified': stats.st_mtime,
                'size': stats.st_size,
            }

    @staticmethod
    def _load_codex_session_index():
        index_path = os.path.expanduser('~/.codex/session_index.jsonl')
        sessions = {}
        if not os.path.exists(index_path):
            return sessions, None
        try:
            stats = os.stat(index_path)
            with open(index_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = data.get('id')
                    if session_id:
                        sessions[session_id] = data
        except IOError:
            return {}, None
        return sessions, stats.st_mtime

    @staticmethod
    def _extract_codex_content_text(content):
        texts = []
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, str):
                if item.strip():
                    texts.append(item)
            elif isinstance(item, dict):
                item_type = item.get('type')
                if item_type in {'input_text', 'output_text', 'text'}:
                    text = str(item.get('text', '')).strip()
                    if text:
                        texts.append(text)
        return '\n\n'.join(texts).strip()

    @staticmethod
    def _read_codex_metadata(session_file, session_index, index_mtime=None):
        session_meta = None
        fallback_title = None
        first_user_message_pending = True
        try:
            with open(session_file, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get('type') == 'session_meta' and not session_meta:
                        session_meta = data.get('payload') or {}
                    elif data.get('type') == 'response_item':
                        payload = data.get('payload') or {}
                        if payload.get('type') != 'message' or payload.get('role') != 'user':
                            continue
                        text = CCPeekHandler._extract_codex_content_text(
                            payload.get('content', []))
                        if not text:
                            continue
                        is_bootstrap = (
                            first_user_message_pending and
                            CCPeekHandler._is_codex_agents_bootstrap_message(text)
                        )
                        first_user_message_pending = False
                        if is_bootstrap:
                            continue
                        if not text.lstrip().startswith('<environment_context>'):
                            fallback_title = text
                            break
        except IOError:
            return None

        if not session_meta:
            return None

        source_id = session_meta.get('id')
        if not source_id:
            return None

        stats = os.stat(session_file)
        indexed = session_index.get(source_id, {})
        title = indexed.get('thread_name') or fallback_title or 'Untitled Conversation'
        conv_id = CCPeekHandler._make_conversation_id(CODEX_SOURCE, source_id)

        return {
            'id': conv_id,
            'source': CODEX_SOURCE,
            'source_id': source_id,
            'path': session_file,
            'project_dir': session_meta.get('cwd') or '',
            'parent_id': None,
            'title': CCPeekHandler._truncate_title(title),
            'is_internal': False,
            'timestamp': session_meta.get('timestamp', ''),
            'modified': stats.st_mtime,
            'size': stats.st_size,
            '_index_mtime': index_mtime,
        }

    @staticmethod
    def _append_event(events, source, role, kind, timestamp, text='',
                      name='', payload=None, hidden_by_default=False):
        event = {
            'source': source,
            'role': role,
            'kind': kind,
            'timestamp': timestamp,
            'text': text or '',
            'name': name or '',
            'payload': payload,
            'hidden_by_default': hidden_by_default,
        }
        events.append(event)

    @staticmethod
    def _flush_text_buffer(events, source, role, timestamp, text_parts,
                           hidden_by_default=False):
        text = '\n\n'.join(part for part in text_parts if part).strip()
        text_parts.clear()
        if text:
            CCPeekHandler._append_event(
                events, source, role, 'message', timestamp, text=text,
                hidden_by_default=hidden_by_default)

    @staticmethod
    def _load_claude_events(jsonl_path, suppress_errors=False):
        events = []
        try:
            with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = 'user' if data.get('type') == 'user' else 'assistant'
                    timestamp = data.get('timestamp', '')
                    content = (data.get('message') or {}).get('content')
                    if not content:
                        continue

                    if isinstance(content, str):
                        if role == 'user' and CCPeekHandler._is_local_command_text(content):
                            continue
                        CCPeekHandler._append_event(
                            events, CLAUDE_SOURCE, role, 'message', timestamp,
                            text=content)
                        continue

                    if isinstance(content, dict):
                        CCPeekHandler._append_event(
                            events, CLAUDE_SOURCE, role, 'message', timestamp,
                            text=CCPeekHandler._serialize_payload(content))
                        continue

                    text_parts = []
                    for item in content:
                        if isinstance(item, str):
                            if role == 'user' and CCPeekHandler._is_local_command_text(item):
                                continue
                            text_parts.append(item)
                            continue
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get('type')
                        if item_type == 'text':
                            text_parts.append(str(item.get('text', '')))
                        elif item_type == 'thinking':
                            CCPeekHandler._flush_text_buffer(
                                events, CLAUDE_SOURCE, role, timestamp, text_parts)
                            CCPeekHandler._append_event(
                                events, CLAUDE_SOURCE, 'assistant', 'thinking',
                                timestamp, text=str(item.get('thinking', '')))
                        elif item_type == 'tool_use':
                            CCPeekHandler._flush_text_buffer(
                                events, CLAUDE_SOURCE, role, timestamp, text_parts)
                            CCPeekHandler._append_event(
                                events, CLAUDE_SOURCE, role, 'tool_use', timestamp,
                                name=item.get('name', 'Tool Use'),
                                payload=item.get('input', {}))
                        elif item_type == 'tool_result':
                            CCPeekHandler._flush_text_buffer(
                                events, CLAUDE_SOURCE, role, timestamp, text_parts)
                            CCPeekHandler._append_event(
                                events, CLAUDE_SOURCE, role, 'tool_result', timestamp,
                                payload=item.get('content', ''))
                    CCPeekHandler._flush_text_buffer(
                        events, CLAUDE_SOURCE, role, timestamp, text_parts)
        except IOError:
            if suppress_errors:
                return []
            raise
        return events

    @staticmethod
    def _load_codex_events(session_path, suppress_errors=False):
        events = []
        first_user_message_pending = True
        try:
            with open(session_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = data.get('timestamp', '')
                    row_type = data.get('type')

                    if row_type == 'response_item':
                        payload = data.get('payload') or {}
                        payload_type = payload.get('type')
                        if payload_type == 'message':
                            role = payload.get('role') or 'assistant'
                            text = CCPeekHandler._extract_codex_content_text(
                                payload.get('content', []))
                            if not text:
                                continue
                            is_bootstrap = (
                                role == 'user' and
                                first_user_message_pending and
                                CCPeekHandler._is_codex_agents_bootstrap_message(text)
                            )
                            if role == 'user':
                                first_user_message_pending = False
                            hidden = (
                                role == 'developer'
                                or text.lstrip().startswith('<environment_context>')
                                or is_bootstrap
                            )
                            CCPeekHandler._append_event(
                                events, CODEX_SOURCE, role, 'message', timestamp,
                                text=text, hidden_by_default=hidden)
                        elif payload_type == 'function_call':
                            CCPeekHandler._append_event(
                                events, CODEX_SOURCE, 'assistant', 'tool_use',
                                timestamp, name=payload.get('name', 'Tool Call'),
                                payload=CCPeekHandler._try_parse_json(
                                    payload.get('arguments', '')))
                        elif payload_type == 'function_call_output':
                            CCPeekHandler._append_event(
                                events, CODEX_SOURCE, 'assistant', 'tool_result',
                                timestamp, payload=payload.get('output', ''))
                    elif row_type == 'event_msg':
                        payload = data.get('payload') or {}
                        if payload.get('type') == 'agent_message':
                            text = str(payload.get('message', '')).strip()
                            if text:
                                CCPeekHandler._append_event(
                                    events, CODEX_SOURCE, 'assistant',
                                    'commentary', timestamp, text=text)
        except IOError:
            if suppress_errors:
                return []
            raise
        return events

    @staticmethod
    def _load_normalized_events(entry, suppress_errors=False):
        source = entry.get('source')
        path = entry.get('path')
        if source == CLAUDE_SOURCE:
            return CCPeekHandler._load_claude_events(
                path, suppress_errors=suppress_errors)
        if source == CODEX_SOURCE:
            return CCPeekHandler._load_codex_events(
                path, suppress_errors=suppress_errors)
        return []

    @staticmethod
    def _find_conversation_entry(conversation_id):
        with _conv_cache_lock:
            entry = _conv_cache.get(conversation_id)
        if entry:
            return entry

        source, raw_id = CCPeekHandler._split_conversation_id(conversation_id)
        if source == CLAUDE_SOURCE:
            claude_dir = os.path.expanduser('~/.claude/projects')
            if os.path.exists(claude_dir):
                pdc = {}
                for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'),
                                            recursive=True):
                    if Path(jsonl_file).stem == raw_id:
                        return CCPeekHandler._read_claude_metadata(
                            jsonl_file, claude_dir, pdc)
        elif source == CODEX_SOURCE:
            codex_dir = os.path.expanduser('~/.codex/sessions')
            session_index, _ = CCPeekHandler._load_codex_session_index()
            if os.path.exists(codex_dir):
                for session_file in glob.glob(
                        os.path.join(codex_dir, '**/rollout-*.jsonl'),
                        recursive=True):
                    entry = CCPeekHandler._read_codex_metadata(
                        session_file, session_index)
                    if entry and entry.get('source_id') == raw_id:
                        return entry
        return None

    @staticmethod
    def _events_to_search_parts(events):
        text_parts = []
        thinking_parts = []
        for event in events:
            if event.get('hidden_by_default'):
                continue
            kind = event.get('kind')
            if kind == 'thinking':
                thinking_parts.append(
                    CCPeekHandler._strip_inline_markdown(event.get('text', '')))
            elif kind in {'message', 'commentary'}:
                text_parts.append(
                    CCPeekHandler._strip_inline_markdown(event.get('text', '')))
            elif kind == 'tool_use':
                payload = event.get('payload')
                tool_text = (event.get('name', '') + ' ' +
                             CCPeekHandler._serialize_payload(payload)).strip()
                text_parts.append(tool_text)
            elif kind == 'tool_result':
                text_parts.append(
                    CCPeekHandler._serialize_payload(event.get('payload')))
        return text_parts, thinking_parts

    def handle_conversations(self, include_internal=False):
        """Serve conversations from the background-refreshed cache."""
        _conv_cache_ready.wait()

        conversations = []
        job_sessions = self._load_job_sessions()

        with _conv_cache_lock:
            entries = list(_conv_cache.values())

        for entry in entries:
            if not include_internal and entry.get('is_internal'):
                continue
            conv = {
                k: v for k, v in entry.items()
                if k != 'is_internal' and not k.startswith('_')
            }
            job = job_sessions.get(entry['source_id']) if entry.get('source') == CLAUDE_SOURCE else None
            if job and entry.get('source') == CLAUDE_SOURCE:
                conv['is_background'] = True
                conv['job_state'] = job.get('state')
            conversations.append(conv)

        conversations.sort(
            key=lambda x: (x['modified'], x.get('timestamp', ''), x['id']),
            reverse=True)

        etag = self._etag_for_json(conversations)
        if self.headers.get('If-None-Match') == etag:
            self._json_response(None, 304, {'ETag': etag})
            return

        self._json_response(conversations, headers={'ETag': etag})

    def handle_conversation(self, conversation_id, include_internal=False):
        """Get normalized transcript events for a specific conversation."""
        if '/' in conversation_id or '\\' in conversation_id:
            self._json_response({'error': 'Invalid conversation ID'}, 400)
            return

        _conv_cache_ready.wait()
        source, raw_id = self._split_conversation_id(conversation_id)
        if not source or not raw_id:
            self._json_response({'error': 'Invalid conversation ID'}, 400)
            return

        entry = self._find_conversation_entry(conversation_id)
        if not entry or not os.path.exists(entry.get('path', '')):
            self._json_response({'error': 'Conversation not found', 'conversation_id': conversation_id}, 404)
            return

        try:
            events = self._load_normalized_events(entry)
        except PermissionError:
            self._json_response({
                'error': 'File is locked (conversation may be active)',
                'conversation_id': conversation_id,
                'path': entry['path']
            }, 503)
            return
        except IOError as e:
            self._json_response({
                'error': f'Error reading file: {str(e)}',
                'conversation_id': conversation_id,
                'path': entry['path']
            }, 500)
            return

        if not include_internal and entry.get('is_internal'):
            self._json_response({'error': 'Conversation not found', 'conversation_id': conversation_id}, 404)
            return

        self._json_response(events)

    @staticmethod
    def _strip_inline_markdown(text):
        text = _RE_MD_FENCED.sub(r'\1', text)
        text = _RE_MD_INLINE.sub(r'\1', text)
        text = _RE_MD_LINK.sub(r'\1', text)
        text = _RE_MD_EMPH.sub(r'\2', text)
        return text

    def _extract_content_parts(self, content):
        """Extract text, tool, and thinking content separately from a message.

        Returns: (text_content, tool_content, thinking_content) tuple of strings
        """
        text_parts = []
        tool_parts = []
        thinking_parts = []

        if isinstance(content, str):
            return (content, '', '')
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                elif isinstance(item, dict):
                    item_type = item.get('type')
                    if item_type == 'text':
                        text_parts.append(str(item.get('text', '')))
                    elif item_type == 'tool_result':
                        result_content = item.get('content', '')
                        if isinstance(result_content, str):
                            tool_parts.append(result_content)
                        else:
                            tool_parts.append(json.dumps(result_content))
                    elif item_type == 'tool_use':
                        tool_parts.append(json.dumps(item.get('input', {})))
                    elif item_type == 'thinking':
                        thinking_parts.append(str(item.get('thinking', '')))
            return (' '.join(text_parts), ' '.join(tool_parts),
                    ' '.join(thinking_parts))
        elif isinstance(content, dict):
            return ('', json.dumps(content), '')
        return (str(content), '', '')

    def _create_snippet(self, text, match_pos, max_len=200):
        """Create a snippet around the match position."""
        context_before = 60
        context_after = max_len - context_before - 10

        start = max(0, match_pos - context_before)
        end = min(len(text), match_pos + context_after)

        snippet = text[start:end]
        snippet = ' '.join(snippet.split())

        if start > 0:
            snippet = '...' + snippet
        if end < len(text):
            snippet = snippet + '...'

        return snippet

    def _snippet_from_parts(self, parts, pattern, max_len=200):
        """Find the message part containing the first match and snippet from it alone."""
        for part in parts:
            m = pattern.search(part)
            if m:
                return self._create_snippet(part, m.start(), max_len)
        return ''

    def _token_snippet_from_parts(self, parts, token_patterns, max_len=200):
        """Find the message part containing the rarest token and snippet from it."""
        for part in parts:
            all_present = all(p.search(part) for _tok, p, _wp in token_patterns)
            if not all_present:
                continue
            rarest_match = None
            rarest_count = float('inf')
            for _tok, p, _wp in token_patterns:
                m = p.search(part)
                if m:
                    count = len(p.findall(part))
                    if count < rarest_count:
                        rarest_count = count
                        rarest_match = m
            if rarest_match:
                return self._create_snippet(part, rarest_match.start(), max_len)
        # Fallback: find any part with any token
        rarest_match = None
        rarest_count = float('inf')
        best_part = None
        for part in parts:
            for _tok, p, _wp in token_patterns:
                m = p.search(part)
                if m:
                    count = len(p.findall(part))
                    if count < rarest_count:
                        rarest_count = count
                        rarest_match = m
                        best_part = part
        if rarest_match and best_part:
            return self._create_snippet(best_part, rarest_match.start(), max_len)
        return ''

    @staticmethod
    def _is_internal_thread(jsonl_path, first_user_title=None):
        """Check if a conversation is a useless internal thread (local command output).

        Note: Subagent threads are NOT considered internal - they contain useful context.
        Only threads with local command markers are hidden by default.
        """
        if first_user_title:
            if any(first_user_title.startswith(p) for p in CCPeekHandler._INTERNAL_PREFIXES):
                return True
            return False
        # No real user message found; check if the file has only command messages
        try:
            with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get('type') == 'user' and data.get('message'):
                        text = extract_first_text(data['message'].get('content', ''))
                        if text and any(text.startswith(p) for p in CCPeekHandler._INTERNAL_PREFIXES):
                            return True
                        return False
        except IOError:
            pass
        return False

    @staticmethod
    def _build_search_pattern(search_term):
        broad = r'(?:\n|\\n|\s+)'
        narrow = r'(?:\n|\s+)'
        result = []
        i = 0
        while i < len(search_term):
            if (i + 1 < len(search_term)
                    and search_term[i] == '\\' and search_term[i + 1] == 'n'):
                result.append(broad)
                i += 2
            elif search_term[i] == '\n':
                result.append(narrow)
                i += 1
            else:
                j = i + 1
                while j < len(search_term):
                    if search_term[j] == '\n':
                        break
                    if (j + 1 < len(search_term)
                            and search_term[j] == '\\' and search_term[j + 1] == 'n'):
                        break
                    j += 1
                result.append(re.escape(search_term[i:j]))
                i = j
        inner = ''.join(result)
        pattern = re.compile(inner, re.IGNORECASE | re.DOTALL)
        left = r'\b' if re.match(r'\w', search_term[0]) else r'(?<!\w)'
        right = r'\b' if re.match(r'\w', search_term[-1]) else r'(?!\w)'
        word_pattern = re.compile(left + inner + right, re.IGNORECASE | re.DOTALL)
        return pattern, word_pattern

    @staticmethod
    def _build_search_cache():
        """Populate _search_cache with normalized searchable blobs."""
        _conv_cache_ready.wait()
        with _conv_cache_lock:
            entries = list(_conv_cache.values())

        current_ids = set()
        for entry in entries:
            conv_id = entry['id']
            current_ids.add(conv_id)
            mtime = entry.get('modified')

            with _search_cache_lock:
                cached = _search_cache.get(conv_id)
                if cached and cached['mtime'] == mtime:
                    continue

            events = CCPeekHandler._load_normalized_events(
                entry, suppress_errors=True)
            text_parts, thinking_parts = CCPeekHandler._events_to_search_parts(events)
            text_blob = ' '.join(text_parts)
            thinking_blob = ' '.join(thinking_parts)
            cache_entry = {
                'text': text_blob,
                'thinking': thinking_blob,
                'text_lower': text_blob.lower(),
                'thinking_lower': thinking_blob.lower(),
                'text_parts': text_parts,
                'thinking_parts': thinking_parts,
                'mtime': mtime,
                'path': entry.get('path'),
                'is_internal': entry.get('is_internal', False),
            }
            with _search_cache_lock:
                _search_cache[conv_id] = cache_entry

        with _search_cache_lock:
            for stale in set(_search_cache) - current_ids:
                del _search_cache[stale]

    @staticmethod
    def _build_token_patterns(search_term):
        tokens = search_term.split()
        if len(tokens) < 2:
            return None
        patterns = []
        for tok in tokens:
            escaped = re.escape(tok)
            p = re.compile(escaped, re.IGNORECASE)
            left = r'\b' if re.match(r'\w', tok[0]) else r'(?<!\w)'
            right = r'\b' if re.match(r'\w', tok[-1]) else r'(?!\w)'
            wp = re.compile(left + escaped + right, re.IGNORECASE)
            patterns.append((tok, p, wp))
        return patterns

    @staticmethod
    def _supports_plain_search(search_term):
        return '\n' not in search_term and '\\n' not in search_term

    @staticmethod
    def _has_plain_word_match(lower_text, lower_term):
        start = 0
        term_len = len(lower_term)
        while True:
            pos = lower_text.find(lower_term, start)
            if pos == -1:
                return False
            end = pos + term_len
            left_ok = pos == 0 or not (lower_text[pos - 1].isalnum() or lower_text[pos - 1] == '_')
            right_ok = end == len(lower_text) or not (lower_text[end].isalnum() or lower_text[end] == '_')
            if left_ok and right_ok:
                return True
            start = pos + 1

    @staticmethod
    def _normalize_search_request_seq(request_seq):
        try:
            return int(request_seq)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _note_search_request(client_id, request_seq):
        request_seq = CCPeekHandler._normalize_search_request_seq(request_seq)
        if not client_id or request_seq is None:
            return
        with _search_request_state_lock:
            current = _search_request_state.get(client_id)
            if current is None or request_seq > current:
                _search_request_state[client_id] = request_seq

    @staticmethod
    def _is_search_request_stale(client_id, request_seq):
        request_seq = CCPeekHandler._normalize_search_request_seq(request_seq)
        if not client_id or request_seq is None:
            return False
        with _search_request_state_lock:
            current = _search_request_state.get(client_id)
        return current is not None and request_seq < current

    def _token_search(self, text, token_patterns, lower_text=None):
        lower_text = text.lower() if lower_text is None else lower_text
        total = 0
        word_total = 0
        for _tok, _p, _wp in token_patterns:
            if _tok.lower() not in lower_text:
                return None
            total += 1
            word_total += 1
        return total, word_total

    def _token_snippet(self, text, token_patterns, max_len=200):
        rarest_tok = None
        rarest_count = float('inf')
        rarest_match = None
        for _tok, p, _wp in token_patterns:
            m = p.search(text)
            if m:
                count = len(p.findall(text))
                if count < rarest_count:
                    rarest_count = count
                    rarest_match = m
                    rarest_tok = _tok
        if rarest_match:
            return self._create_snippet(text, rarest_match.start(), max_len)
        return ''

    def _token_snippet_from_parts_fast(self, parts, token_patterns, max_len=200):
        lowered_tokens = [tok.lower() for tok, _, _ in token_patterns]
        for part in parts:
            part_lower = part.lower()
            if all(tok in part_lower for tok in lowered_tokens):
                first_pos = min(part_lower.find(tok) for tok in lowered_tokens)
                return self._create_snippet(part, first_pos, max_len)
        for part in parts:
            part_lower = part.lower()
            for tok in lowered_tokens:
                pos = part_lower.find(tok)
                if pos != -1:
                    return self._create_snippet(part, pos, max_len)
        return ''

    def handle_search(self, search_term, include_thinking=False,
                      client_id='', request_seq=''):
        """Search across all conversations for a term.

        Returns all matches with is_internal flag so client can filter locally.
        Uses _search_cache for speed; rebuilds only stale/new entries.
        Phrases are matched exactly first; if the query has multiple tokens
        and the exact phrase doesn't match, falls back to requiring all
        tokens to appear independently (match_type="all_terms").
        """
        if not search_term or len(search_term) < 2:
            self._json_response({'matches': {}})
            return

        self._note_search_request(client_id, request_seq)
        self._build_search_cache()
        if self._is_search_request_stale(client_id, request_seq):
            self._json_response({'matches': {}, 'cancelled': True})
            return

        pattern, word_pattern = self._build_search_pattern(search_term)
        token_patterns = self._build_token_patterns(search_term)
        plain_search = self._supports_plain_search(search_term)
        search_term_lower = search_term.lower() if plain_search else None
        matches = {}

        with _search_cache_lock:
            items = list(_search_cache.items())

        for idx, (conv_id, entry) in enumerate(items):
            if idx % 16 == 0 and self._is_search_request_stale(client_id, request_seq):
                self._json_response({'matches': {}, 'cancelled': True})
                return
            text_blob = entry['text']
            thinking_blob = entry['thinking']
            text_lower = entry['text_lower']
            thinking_lower = entry['thinking_lower']

            if plain_search:
                text_count = text_lower.count(search_term_lower)
                text_word_count = (
                    1 if text_count and self._has_plain_word_match(
                        text_lower, search_term_lower) else 0
                )
            else:
                text_found = pattern.findall(text_blob)
                text_count = len(text_found)
                text_word_count = len(word_pattern.findall(text_blob)) if text_count else 0

            thinking_count = 0
            thinking_word_count = 0
            if include_thinking and thinking_blob:
                if plain_search:
                    thinking_count = thinking_lower.count(search_term_lower)
                    thinking_word_count = (
                        1 if thinking_count and self._has_plain_word_match(
                            thinking_lower, search_term_lower) else 0
                    )
                else:
                    thinking_found = pattern.findall(thinking_blob)
                    thinking_count = len(thinking_found)
                    thinking_word_count = (
                        len(word_pattern.findall(thinking_blob))
                        if thinking_count else 0)

            if text_count or thinking_count:
                snippet = None
                if text_count:
                    if plain_search:
                        snippet = self._token_snippet_from_parts_fast(
                            entry['text_parts'],
                            [(search_term, pattern, word_pattern)])
                    else:
                        snippet = self._snippet_from_parts(
                            entry['text_parts'], pattern)
                if not snippet and thinking_count:
                    if plain_search:
                        snippet = self._token_snippet_from_parts_fast(
                            entry['thinking_parts'],
                            [(search_term, pattern, word_pattern)])
                    else:
                        snippet = self._snippet_from_parts(
                            entry['thinking_parts'], pattern)

                matches[conv_id] = {
                    'text_count': text_count,
                    'thinking_count': thinking_count,
                    'text_word_count': text_word_count,
                    'thinking_word_count': thinking_word_count,
                    'is_internal': entry['is_internal'],
                    'snippet': snippet or '',
                }
                continue

            if not token_patterns:
                continue

            text_tok = self._token_search(
                text_blob, token_patterns, lower_text=text_lower)
            think_tok = None
            if include_thinking and thinking_blob:
                think_tok = self._token_search(
                    thinking_blob, token_patterns, lower_text=thinking_lower)

            if not text_tok and not think_tok:
                continue

            t_count, t_wcount = text_tok or (0, 0)
            th_count, th_wcount = think_tok or (0, 0)

            snippet = self._token_snippet_from_parts_fast(
                entry['text_parts'],
                token_patterns) if text_tok else ''
            if not snippet and think_tok:
                snippet = self._token_snippet_from_parts_fast(
                    entry['thinking_parts'], token_patterns)

            matches[conv_id] = {
                'text_count': t_count,
                'thinking_count': th_count,
                'text_word_count': t_wcount,
                'thinking_word_count': th_wcount,
                'is_internal': entry['is_internal'],
                'snippet': snippet or '',
                'match_type': 'all_terms',
                'tokens': [tok for tok, _, _ in token_patterns],
            }

        self._json_response({'matches': matches})

    def log_message(self, format, *args):
        # Suppress request logging
        pass

def is_port_in_use(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (OSError, socket.timeout):
        return False

def verify_ccpeek_instance(host, port):
    """Verify that the service on host:port is actually ccpeek."""
    import http.client
    try:
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request('GET', '/api/health')
        response = conn.getresponse()
        if response.status == 200:
            data = json.loads(response.read().decode())
            return data.get('service') == 'ccpeek'
        return False
    except Exception:
        return False
    finally:
        conn.close()

def find_free_port(start_port=DEFAULT_PORT):
    port = start_port
    while port < start_port + 100:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', port))
                return port
        except OSError:
            port += 1
    return None

def _is_wsl():
    try:
        with open('/proc/version', 'r') as f:
            return 'microsoft' in f.read().lower()
    except OSError:
        return False

def open_browser(host, port):
    """Open browser after a short delay"""
    time.sleep(0.5)
    url = f'http://{host}:{port}'

    # WSL2: open on the Windows side so the user's default browser launches
    if _is_wsl():
        try:
            subprocess.Popen(['cmd.exe', '/c', 'start', url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            pass  # cmd.exe not on PATH — fall through

    # Use xdg-open to respect system's default browser
    try:
        # Use setsid to detach browser from our process group
        subprocess.Popen(['setsid', 'xdg-open', url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True)
    except:
        # Fallback to webbrowser module if xdg-open fails
        try:
            webbrowser.open(url)
        except:
            print(f"Could not auto-open browser. Please visit: {url}")

def resolve_display_host(host):
    """Provide a user-facing host string for status messages."""
    if host not in {'0.0.0.0', '::'}:
        return host

    # Try to guess a non-loopback address for convenience
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.0.2.1', 80))  # TEST-NET-1, no traffic sent
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return host

def get_ccpeek_bin():
    found = shutil.which('ccpeek')
    if found:
        return os.path.realpath(found)
    return os.path.abspath(sys.argv[0])

def is_setup_done():
    return os.path.exists(SETUP_MARKER)

def mark_setup_done():
    os.makedirs(os.path.dirname(SETUP_MARKER), exist_ok=True)
    Path(SETUP_MARKER).touch()

FIREWALL_RULE_NAME = 'CCPeek'


def run_setup(port):
    """Interactive first-time setup wizard.

    Returns True if background service started successfully, False otherwise.
    """
    print("-- ccpeek setup --\n")

    mechanism = "Task Scheduler" if sys.platform == 'win32' else "systemd"

    try:
        answer = input(
            f"Start ccpeek automatically on login via {mechanism} (port {port})? [Y/n] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = 'n'

    mark_setup_done()

    if answer in ('n', 'no'):
        print(f"Skipped {mechanism} registration")
        started = False
    elif sys.platform == 'win32':
        started = _setup_windows_task(port, True)
    else:
        started = _setup_systemd_service(port, True)

    if sys.platform == 'win32':
        _setup_firewall_rule(port, interactive=True)

    print("Use --setup to register, --remove to unregister\n")
    return started


def _firewall_rule_exists():
    """Check whether the ccpeek firewall rule already exists."""
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f"Get-NetFirewallRule -DisplayName '{FIREWALL_RULE_NAME}' "
         f"-ErrorAction SilentlyContinue"],
        capture_output=True, text=True
    )
    return result.returncode == 0 and FIREWALL_RULE_NAME in result.stdout


def _setup_firewall_rule(port, interactive=False):
    """Add or remove a Windows Firewall inbound rule for LAN access."""
    if _firewall_rule_exists():
        print(f"Firewall rule '{FIREWALL_RULE_NAME}' already exists")
        return

    if interactive:
        try:
            answer = input(
                f"Allow LAN access through Windows Firewall (port {port})? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = 'n'
        if answer in ('n', 'no'):
            print("Skipped firewall rule")
            return

    ps_cmd = (
        f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE_NAME}' "
        f"-Direction Inbound -Protocol TCP -LocalPort {port} "
        f"-Action Allow -Profile Any"
    )
    try:
        _run_ps_elevated(ps_cmd)
    except FileNotFoundError:
        print("PowerShell not available, skipped firewall rule")
        return

    if _firewall_rule_exists():
        print(f"Added firewall rule '{FIREWALL_RULE_NAME}'")
    else:
        print("Could not add firewall rule")


def _remove_firewall_rule():
    if _firewall_rule_exists():
        _run_ps_elevated(
            f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE_NAME}'"
        )
        print(f"Removed firewall rule '{FIREWALL_RULE_NAME}'")


def run_remove():
    if sys.platform == 'win32':
        _setup_windows_task(0, False)
        _remove_firewall_rule()
    else:
        _setup_systemd_service(0, False)


def run_restart():
    if sys.platform == 'win32':
        result = subprocess.run(
            ['schtasks', '/query', '/tn', TASK_NAME],
            capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(
                ['schtasks', '/end', '/tn', TASK_NAME],
                capture_output=True
            )
            time.sleep(1)
            subprocess.run(
                ['schtasks', '/run', '/tn', TASK_NAME],
                capture_output=True
            )
            time.sleep(2)
            print(f"Restarted scheduled task '{TASK_NAME}'")
            return True
    else:
        if os.path.exists(UNIT_PATH):
            try:
                subprocess.run(
                    ['systemctl', '--user', 'restart', 'ccpeek'],
                    check=True
                )
                time.sleep(1)
                print("Restarted ccpeek systemd service")
                return True
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass

    # No task/service found; kill any running ccpeek process and note it
    killed = False
    for proc_file in glob.glob('/proc/*/cmdline'):
        try:
            with open(proc_file, 'rb') as f:
                cmdline = f.read().decode('utf-8', errors='replace')
            if 'server.py' in cmdline and 'ccpeek' in cmdline.lower():
                pid = int(proc_file.split('/')[2])
                if pid != os.getpid():
                    os.kill(pid, 15)
                    killed = True
        except (OSError, ValueError):
            pass

    if not killed and sys.platform == 'win32':
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter "
             "\"Name='pythonw.exe' OR Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'server\\.py' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True
        )
        killed = result.returncode == 0

    if killed:
        print("Stopped running ccpeek process (no task/service found to restart)")
    else:
        print("No ccpeek task, service, or process found")
    return False


def _setup_systemd_service(port, enable):
    """Create or remove the systemd user service."""
    if enable:
        bin_path = get_ccpeek_bin()
        unit = (
            "[Unit]\n"
            "Description=ccpeek - Claude Code Chat History Viewer\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f'ExecStart="{bin_path}" --no-browser --port {port}\n'
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
        with open(UNIT_PATH, 'w') as f:
            f.write(unit)

        try:
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', '--now', 'ccpeek'], check=True)
            time.sleep(1)
            print(f"Registered and started ccpeek on port {port}")
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            if os.path.exists(UNIT_PATH):
                os.remove(UNIT_PATH)
            print("Could not register systemd service, starting foreground server")
            return False
    else:
        if os.path.exists(UNIT_PATH):
            subprocess.run(['systemctl', '--user', 'disable', '--now', 'ccpeek'],
                          capture_output=True)
            os.remove(UNIT_PATH)
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
            print("Removed existing ccpeek systemd service")
        else:
            print("Skipped systemd registration")
        return False


def _run_ps_elevated(ps_cmd):
    """Run a PowerShell command, elevating via UAC if needed.

    Tries direct execution first.  On failure, re-launches the same
    command inside an elevated PowerShell via Start-Process -Verb RunAs,
    using -EncodedCommand to avoid nested quoting issues.
    """
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command', ps_cmd],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return
    import base64
    encoded = base64.b64encode(ps_cmd.encode('utf-16-le')).decode('ascii')
    subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f"Start-Process powershell -Verb RunAs -Wait "
         f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'"],
        capture_output=True
    )


def _setup_windows_task(port, enable):
    """Create or remove the Windows scheduled task."""
    if enable:
        pythonw = shutil.which('pythonw')
        if not pythonw:
            python_dir = os.path.dirname(sys.executable)
            candidate = os.path.join(python_dir, 'pythonw.exe')
            if os.path.exists(candidate):
                pythonw = candidate

        if not pythonw:
            print("Could not find pythonw.exe, starting foreground server")
            return False

        script = os.path.abspath(__file__)
        workdir = os.path.dirname(script)
        task_args = f'"{script}" --no-browser --port {port}'

        username = os.environ.get('USERNAME', '')
        ps_cmd = (
            f"$action = New-ScheduledTaskAction -Execute '{pythonw}' "
            f"-Argument '{task_args}' -WorkingDirectory '{workdir}'; "
            f"$trigger = New-ScheduledTaskTrigger -AtLogOn -User '{username}'; "
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' "
            f"-Action $action -Trigger $trigger -Force; "
            f"Start-ScheduledTask -TaskName '{TASK_NAME}'"
        )

        try:
            _run_ps_elevated(ps_cmd)
        except FileNotFoundError:
            print("PowerShell not available, starting foreground server")
            return False

        result = subprocess.run(
            ['schtasks', '/query', '/tn', TASK_NAME],
            capture_output=True
        )
        if result.returncode != 0:
            print("Could not create scheduled task, starting foreground server")
            return False

        time.sleep(1)
        print(f"Created and started scheduled task '{TASK_NAME}' on port {port}")
        return True
    else:
        try:
            result = subprocess.run(
                ['schtasks', '/query', '/tn', TASK_NAME],
                capture_output=True
            )
            if result.returncode == 0:
                _run_ps_elevated(
                    f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
                    f"-Confirm:$false"
                )
                print(f"Removed scheduled task '{TASK_NAME}'")
            else:
                print("Skipped Task Scheduler registration")
        except FileNotFoundError:
            print("Skipped Task Scheduler registration")
        return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    default_host = os.environ.get('CCPEEK_HOST', DEFAULT_HOST)
    default_port = int(os.environ.get('CCPEEK_PORT', DEFAULT_PORT))

    env_browser = os.environ.get('CCPEEK_OPEN_BROWSER')
    env_no_browser = os.environ.get('CCPEEK_NO_BROWSER')
    default_open_browser = True
    if env_browser is not None:
        default_open_browser = env_browser.lower() in {'1', 'true', 'yes'}
    elif env_no_browser is not None:
        default_open_browser = env_no_browser.lower() not in {'1', 'true', 'yes'}

    parser = argparse.ArgumentParser(description='Start the CCPeek server')
    parser.add_argument('--host', default=default_host, help='Interface to bind (default: %(default)s)')
    parser.add_argument('--port', type=int, default=default_port, help='Preferred port to bind (default: %(default)s)')
    parser.add_argument('--open-browser', dest='open_browser', action='store_true', help='Open a browser window after startup')
    parser.add_argument('--no-browser', dest='open_browser', action='store_false', help='Do not launch a browser window')
    parser.add_argument('--setup', action='store_true', help='Register as a background service')
    parser.add_argument('--remove', action='store_true', help='Unregister the background service')
    parser.add_argument('--restart', action='store_true', help='Restart the background service')
    parser.set_defaults(open_browser=default_open_browser)

    args = parser.parse_args(argv)
    host = args.host

    # --remove: unregister and exit
    if args.remove:
        run_remove()
        sys.exit(0)

    # --restart: restart the background service and exit
    if args.restart:
        run_restart()
        sys.exit(0)

    # Setup wizard: on --setup or first interactive launch
    systemd_started = False
    if args.setup or (not is_setup_done() and sys.stdin.isatty()):
        systemd_started = run_setup(args.port)

    # If ccpeek is already listening on the target port, reuse it
    if is_port_in_use(host, args.port):
        if verify_ccpeek_instance(host, args.port):
            display_host = resolve_display_host(host)
            print(f"ccpeek is already running at http://{display_host}:{args.port}")
            if args.open_browser and host in LOCAL_HOSTS:
                open_browser(host if host != 'localhost' else '127.0.0.1', args.port)
            sys.exit(0)
        elif not systemd_started:
            # Port in use by another service - find a free port
            args.port = find_free_port(args.port + 1)
            if args.port is None:
                print("Could not find a free port in range")
                sys.exit(1)
            print(f"Port in use by another service, using port {args.port}")

    # --setup is config-only; don't start a foreground server
    if args.setup:
        sys.exit(0)

    port = args.port
    try:
        httpd = CCPeekServer((host, port), CCPeekHandler)
    except OSError as err:
        print(f"Failed to start server on {host}:{port} -> {err}")
        sys.exit(1)

    display_host = resolve_display_host(host)
    print(f"CCPeek server starting on http://{display_host}:{port}")
    if host in {'0.0.0.0', '::'}:
        print("Listening on all network interfaces")
    print("Press Ctrl+C to stop")

    def _refresh_conv_cache():
        claude_dir = os.path.expanduser('~/.claude/projects')
        codex_dir = os.path.expanduser('~/.codex/sessions')
        while True:
            with _conv_cache_lock:
                existing_cache = dict(_conv_cache)
            cached_codex_by_path = {
                entry.get('path'): entry
                for entry in existing_cache.values()
                if entry.get('source') == CODEX_SOURCE and entry.get('path')
            }
            next_cache = {}
            if os.path.exists(claude_dir):
                pdc = {}
                for f in sorted(glob.glob(
                        os.path.join(claude_dir, '**/*.jsonl'),
                        recursive=True)):
                    try:
                        stats = os.stat(f)
                        mtime = stats.st_mtime
                        conv_id = CCPeekHandler._make_conversation_id(
                            CLAUDE_SOURCE, Path(f).stem)
                        cached = existing_cache.get(conv_id)
                        if (cached and cached['modified'] == mtime
                                and cached.get('size') == stats.st_size
                                and not cached.get('title', '').startswith('<')):
                            next_cache[conv_id] = cached
                            continue
                        entry = CCPeekHandler._read_claude_metadata(f, claude_dir, pdc)
                        if entry:
                            next_cache[entry['id']] = entry
                    except Exception:
                        pass

            if os.path.exists(codex_dir):
                session_index, index_mtime = CCPeekHandler._load_codex_session_index()
                for f in sorted(glob.glob(
                        os.path.join(codex_dir, '**/rollout-*.jsonl'),
                        recursive=True)):
                    try:
                        stats = os.stat(f)
                        cached = cached_codex_by_path.get(f)
                        if (cached and cached.get('modified') == stats.st_mtime
                                and cached.get('size') == stats.st_size
                                and cached.get('_index_mtime') == index_mtime):
                            next_cache[cached['id']] = cached
                            continue

                        entry = CCPeekHandler._read_codex_metadata(
                            f, session_index, index_mtime=index_mtime)
                        if entry:
                            next_cache[entry['id']] = entry
                    except Exception:
                        pass

            with _conv_cache_lock:
                _conv_cache.clear()
                _conv_cache.update(next_cache)
            _conv_cache_ready.set()
            time.sleep(CACHE_REFRESH_INTERVAL)
    threading.Thread(target=_refresh_conv_cache, daemon=True).start()

    if args.open_browser and host in LOCAL_HOSTS:
        threading.Thread(target=open_browser, args=(host if host != 'localhost' else '127.0.0.1', port), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nCCPeek server stopped")
        httpd.shutdown()

if __name__ == '__main__':
    main()
