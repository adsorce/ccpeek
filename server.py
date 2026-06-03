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
from http.server import HTTPServer, SimpleHTTPRequestHandler
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

_RE_MD_FENCED = re.compile(r'```[^\n]*\n(.*?)```', re.DOTALL)
_RE_MD_INLINE = re.compile(r'`([^`]+)`')
_RE_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_RE_MD_EMPH = re.compile(r'(?<!\w)(\*{1,3}|_{1,3}|~~)(.*?)\1(?!\w)')

_search_cache = {}
_search_cache_lock = threading.Lock()

def extract_first_text(content, max_len=100):
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
    return text[:max_len] + ('...' if len(text) > max_len else '') if text else ''

class CCPeekServer(HTTPServer):
    allow_reuse_address = False

class CCPeekHandler(SimpleHTTPRequestHandler):
    _path_cache = {}

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

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
            self.handle_search(unquote(search_term), include_thinking)
        else:
            super().do_GET()

    _INTERNAL_PREFIXES = ('<local-command-', '<command-message>', '<command-name>')

    @staticmethod
    def _extract_title_from_line(data, current_title=None):
        if current_title is not None:
            return current_title
        if data.get('type') == 'user' and data.get('message'):
            text = extract_first_text(data['message'].get('content', ''))
            if text and any(text.startswith(p) for p in CCPeekHandler._INTERNAL_PREFIXES):
                return None
            return text
        return None

    @staticmethod
    def _decode_project_dir(encoded):
        """Decode a Claude project directory name back to the original path.

        Claude encodes paths as: drive--segment-segment (on Windows)
        or segment-segment (on Unix), replacing both path separators
        and certain characters with hyphens.  The encoding is lossy,
        so we verify the result exists on disk.
        """
        sep = os.sep
        # Split on '--' only at the drive letter boundary (first occurrence)
        major = encoded.split('--', 1)
        if len(major) == 2 and len(major[0]) == 1 and major[0].isalpha():
            prefix = major[0].upper() + ':' + sep
            rest = major[1]
        else:
            prefix = sep
            rest = encoded

        segments = [s for s in rest.split('-') if s]
        # Try progressively merging adjacent segments with various joiners
        # to find a path that actually exists on disk.
        # '.' covers dot-prefixed dirs like .claude (encoded as -claude after
        # the preceding segment, producing '--' which split+filter leaves as
        # adjacent segments).
        def resolve(segs, idx, current):
            if idx == len(segs):
                full = prefix + current
                if os.path.isdir(full):
                    yield full
                return
            part = segs[idx]
            for joiner in (sep, '-', '_', '.'):
                next_path = (current + joiner + part) if current else part
                yield from resolve(segs, idx + 1, next_path)
            if current:
                next_path = current + sep + '.' + part
                yield from resolve(segs, idx + 1, next_path)

        for candidate in resolve(segments, 0, ''):
            return candidate
        # Fallback: simple dash-to-sep replacement
        return prefix + rest.replace('-', sep)

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

    def handle_conversations(self, include_internal=False):
        """Get list of all conversations"""
        claude_dir = os.path.expanduser('~/.claude/projects')
        conversations = []
        job_sessions = self._load_job_sessions()

        if os.path.exists(claude_dir):
            # Cache decoded project dirs per encoded dirname
            project_dir_cache = {}
            for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
                try:
                    # Get first message to extract metadata
                    with open(jsonl_file, 'r', encoding='utf-8', errors='replace') as f:
                        first_line = f.readline()
                        if first_line:
                            data = json.loads(first_line)

                            # Get file stats
                            stats = os.stat(jsonl_file)

                            # Try to find first user message for title
                            f.seek(0)
                            title = "Untitled Conversation"
                            for line in f:
                                msg_data = json.loads(line)
                                found = self._extract_title_from_line(msg_data)
                                if found:
                                    title = found
                                    break

                            # Skip internal threads unless requested
                            if not include_internal and self._is_internal_thread(jsonl_file, title):
                                continue

                            # Resolve project directory: walk up from the jsonl
                            # file to find the first child of the projects/ root.
                            # Subagents nest as <project>/<uuid>/subagents/agent-*.jsonl
                            rel = os.path.relpath(jsonl_file, claude_dir)
                            encoded = rel.split(os.sep)[0]
                            if encoded not in project_dir_cache:
                                project_dir_cache[encoded] = self._decode_project_dir(encoded)
                            project_dir = project_dir_cache[encoded]

                            # Detect parent conversation for subagent threads
                            rel_parts = rel.split(os.sep)
                            parent_id = None
                            if 'subagents' in rel_parts:
                                si = rel_parts.index('subagents')
                                if si > 1:
                                    parent_id = rel_parts[si - 1]

                            conv_id = os.path.basename(jsonl_file).replace('.jsonl', '')
                            CCPeekHandler._path_cache[conv_id] = jsonl_file
                            conv = {
                                'id': conv_id,
                                'path': jsonl_file,
                                'project_dir': project_dir,
                                'parent_id': parent_id,
                                'title': title,
                                'timestamp': data.get('timestamp', ''),
                                'modified': stats.st_mtime,
                                'size': stats.st_size
                            }
                            job = job_sessions.get(conv_id)
                            if job:
                                conv['is_background'] = True
                                conv['job_state'] = job.get('state')
                            conversations.append(conv)
                except Exception as e:
                    print(f"Error reading {jsonl_file}: {e}")

        # Sort by modified time (newest first)
        conversations.sort(key=lambda x: x['modified'], reverse=True)

        self._json_response(conversations)

    def handle_conversation(self, conversation_id, include_internal=False):
        """Get messages for a specific conversation"""
        claude_dir = os.path.expanduser('~/.claude/projects')

        # Reject IDs with path separators
        if '/' in conversation_id or '\\' in conversation_id:
            self._json_response({'error': 'Invalid conversation ID'}, 400)
            return

        jsonl_path = CCPeekHandler._path_cache.get(conversation_id)
        if not jsonl_path:
            for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
                if Path(jsonl_file).stem == conversation_id:
                    jsonl_path = jsonl_file
                    CCPeekHandler._path_cache[conversation_id] = jsonl_path
                    break

        if not jsonl_path or not os.path.exists(jsonl_path):
            self._json_response({'error': 'Conversation not found', 'conversation_id': conversation_id}, 404)
            return

        messages = []
        first_user_title = None
        try:
            with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        messages.append(data)
                        # Capture first user message title for internal check
                        first_user_title = self._extract_title_from_line(data, first_user_title)
                    except json.JSONDecodeError:
                        continue
        except PermissionError:
            self._json_response({
                'error': 'File is locked (conversation may be active)',
                'conversation_id': conversation_id,
                'path': jsonl_path
            }, 503)
            return
        except IOError as e:
            self._json_response({
                'error': f'Error reading file: {str(e)}',
                'conversation_id': conversation_id,
                'path': jsonl_path
            }, 500)
            return

        # Check if this is an internal thread and block access if not requested
        if not include_internal and self._is_internal_thread(jsonl_path, first_user_title):
            self._json_response({'error': 'Conversation not found', 'conversation_id': conversation_id}, 404)
            return

        self._json_response(messages)

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

    def _create_snippet(self, text, match_pos, max_len=80):
        """Create a snippet around the match position."""
        # Calculate context window
        context_before = 30
        context_after = max_len - context_before - 10

        start = max(0, match_pos - context_before)
        end = min(len(text), match_pos + context_after)

        snippet = text[start:end]

        # Clean up whitespace
        snippet = ' '.join(snippet.split())

        # Add ellipsis if truncated
        if start > 0:
            snippet = '...' + snippet
        if end < len(text):
            snippet = snippet + '...'

        return snippet

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
        word_pattern = re.compile(r'\b' + inner + r'\b', re.IGNORECASE | re.DOTALL)
        return pattern, word_pattern

    @staticmethod
    def _build_search_cache():
        """Populate _search_cache with text/thinking blobs keyed by conv_id.

        Only re-reads files whose mtime changed since last cache build.
        Removes stale entries for files that no longer exist.
        """
        claude_dir = os.path.expanduser('~/.claude/projects')
        if not os.path.exists(claude_dir):
            return

        current_ids = set()
        for jsonl_file in glob.glob(
                os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
            conv_id = os.path.basename(jsonl_file).replace('.jsonl', '')
            current_ids.add(conv_id)
            try:
                mtime = os.stat(jsonl_file).st_mtime
            except OSError:
                continue

            with _search_cache_lock:
                cached = _search_cache.get(conv_id)
                if cached and cached['mtime'] == mtime:
                    continue  # still fresh

            # Read and index outside the lock to avoid blocking other threads
            text_parts = []
            thinking_parts = []
            first_user_title = None
            try:
                with open(jsonl_file, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        first_user_title = CCPeekHandler._extract_title_from_line(
                            data, first_user_title)
                        content = (data.get('message') or {}).get('content')
                        if not content:
                            continue
                        items = content if isinstance(content, list) else [content]
                        for item in items:
                            if isinstance(item, str):
                                text_parts.append(
                                    CCPeekHandler._strip_inline_markdown(item))
                            elif isinstance(item, dict):
                                t = item.get('type')
                                if t == 'text':
                                    text_parts.append(
                                        CCPeekHandler._strip_inline_markdown(
                                            str(item.get('text', ''))))
                                elif t == 'thinking':
                                    thinking_parts.append(
                                        CCPeekHandler._strip_inline_markdown(
                                            str(item.get('thinking', ''))))
            except IOError:
                continue

            entry = {
                'text': ' '.join(text_parts),
                'thinking': ' '.join(thinking_parts),
                'mtime': mtime,
                'path': jsonl_file,
                'is_internal': CCPeekHandler._is_internal_thread(
                    jsonl_file, first_user_title),
            }
            with _search_cache_lock:
                _search_cache[conv_id] = entry
            CCPeekHandler._path_cache[conv_id] = jsonl_file

        # Evict stale entries
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
            wp = re.compile(r'\b' + escaped + r'\b', re.IGNORECASE)
            patterns.append((tok, p, wp))
        return patterns

    def _token_search(self, text, token_patterns):
        for _tok, p, _wp in token_patterns:
            if not p.search(text):
                return None
        total = 0
        word_total = 0
        for _tok, p, wp in token_patterns:
            total += len(p.findall(text))
            word_total += len(wp.findall(text))
        return total, word_total

    def _token_snippet(self, text, token_patterns, max_len=80):
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

    def handle_search(self, search_term, include_thinking=False):
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

        self._build_search_cache()

        pattern, word_pattern = self._build_search_pattern(search_term)
        token_patterns = self._build_token_patterns(search_term)
        matches = {}

        with _search_cache_lock:
            items = list(_search_cache.items())

        for conv_id, entry in items:
            text_found = pattern.findall(entry['text'])
            text_count = len(text_found)
            text_word_count = len(word_pattern.findall(entry['text'])) if text_count else 0

            thinking_count = 0
            thinking_word_count = 0
            if include_thinking and entry['thinking']:
                thinking_found = pattern.findall(entry['thinking'])
                thinking_count = len(thinking_found)
                thinking_word_count = (
                    len(word_pattern.findall(entry['thinking'])) if thinking_count else 0)

            if text_count or thinking_count:
                snippet = None
                m = pattern.search(entry['text'])
                if m:
                    snippet = self._create_snippet(entry['text'], m.start())
                elif include_thinking and entry['thinking']:
                    m = pattern.search(entry['thinking'])
                    if m:
                        snippet = self._create_snippet(entry['thinking'], m.start())

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

            text_tok = self._token_search(entry['text'], token_patterns)
            think_tok = None
            if include_thinking and entry['thinking']:
                think_tok = self._token_search(
                    entry['thinking'], token_patterns)

            if not text_tok and not think_tok:
                continue

            t_count, t_wcount = text_tok or (0, 0)
            th_count, th_wcount = think_tok or (0, 0)

            snippet = self._token_snippet(
                entry['text'], token_patterns) if text_tok else ''
            if not snippet and think_tok:
                snippet = self._token_snippet(
                    entry['thinking'], token_patterns)

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

    if args.open_browser and host in LOCAL_HOSTS:
        threading.Thread(target=open_browser, args=(host if host != 'localhost' else '127.0.0.1', port), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nCCPeek server stopped")
        httpd.shutdown()

if __name__ == '__main__':
    main()
