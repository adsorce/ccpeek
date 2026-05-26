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

class CCPeekHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'rb') as f:
                self.wfile.write(f.read())
        elif parsed_path.path == '/api/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"service": "ccpeek", "version": "1.0"}).encode())
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
            self.handle_search(unquote(search_term))
        else:
            super().do_GET()

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
            for joiner in (sep, '-', '_'):
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
                            title = "Untitled Conversation"
                            f.seek(0)
                            for line in f:
                                msg_data = json.loads(line)
                                if msg_data.get('type') == 'user' and msg_data.get('message'):
                                    content = msg_data['message'].get('content', '')
                                    if isinstance(content, str):
                                        title = content[:100] + ('...' if len(content) > 100 else '')
                                    elif isinstance(content, list) and content:
                                        # Handle array format
                                        first_content = content[0]
                                        if isinstance(first_content, dict) and first_content.get('type') == 'text':
                                            text = first_content.get('text', '')
                                            title = text[:100] + ('...' if len(text) > 100 else '')
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

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(conversations).encode())

    def handle_conversation(self, conversation_id, include_internal=False):
        """Get messages for a specific conversation"""
        claude_dir = os.path.expanduser('~/.claude/projects')
        jsonl_path = None

        # Reject IDs with path separators
        if '/' in conversation_id or '\\' in conversation_id:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Invalid conversation ID'}).encode())
            return

        # Find the file by exact basename match
        for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
            if Path(jsonl_file).stem == conversation_id:
                jsonl_path = jsonl_file
                break

        if not jsonl_path or not os.path.exists(jsonl_path):
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'Conversation not found',
                'conversation_id': conversation_id
            }).encode())
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
                        if first_user_title is None and data.get('type') == 'user':
                            content = data.get('message', {}).get('content', '')
                            if isinstance(content, str):
                                first_user_title = content[:100]
                            elif isinstance(content, list) and content:
                                first_item = content[0]
                                if isinstance(first_item, dict) and first_item.get('type') == 'text':
                                    first_user_title = first_item.get('text', '')[:100]
                    except json.JSONDecodeError:
                        continue
        except PermissionError:
            self.send_response(503)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'File is locked (conversation may be active)',
                'conversation_id': conversation_id,
                'path': jsonl_path
            }).encode())
            return
        except IOError as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': f'Error reading file: {str(e)}',
                'conversation_id': conversation_id,
                'path': jsonl_path
            }).encode())
            return

        # Check if this is an internal thread and block access if not requested
        if not include_internal and self._is_internal_thread(jsonl_path, first_user_title):
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'Conversation not found',
                'conversation_id': conversation_id
            }).encode())
            return

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(messages).encode())

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

    def _is_internal_thread(self, jsonl_path, first_user_title=None):
        """Check if a conversation is a useless internal thread (local command output).

        Note: Subagent threads are NOT considered internal - they contain useful context.
        Only threads with local command markers are hidden by default.
        """
        if first_user_title:
            # Any <local-command-*> variant
            if first_user_title.startswith('<local-command-'):
                return True
            # Slash command invocations
            if first_user_title.startswith('<command-message>'):
                return True
        return False

    def handle_search(self, search_term):
        """Search across all conversations for a term.

        Returns all matches with is_internal flag so client can filter locally.
        Returns separate counts for text and tool content for visibility filtering.
        """
        if not search_term or len(search_term) < 2:
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'matches': {}}).encode())
            return

        claude_dir = os.path.expanduser('~/.claude/projects')
        matches = {}
        pattern = re.compile(re.escape(search_term), re.IGNORECASE)

        if os.path.exists(claude_dir):
            for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
                conv_id = os.path.basename(jsonl_file).replace('.jsonl', '')
                text_count = 0
                tool_count = 0
                thinking_count = 0
                text_snippet = None
                tool_snippet = None
                thinking_snippet = None
                first_user_title = None

                try:
                    with open(jsonl_file, 'r', encoding='utf-8', errors='replace') as f:
                        for line in f:
                            try:
                                data = json.loads(line)
                                # Capture first user message for internal thread check
                                if first_user_title is None and data.get('type') == 'user':
                                    content = data.get('message', {}).get('content', '')
                                    if isinstance(content, str):
                                        first_user_title = content[:100]
                                    elif isinstance(content, list) and content:
                                        first_item = content[0]
                                        if isinstance(first_item, dict) and first_item.get('type') == 'text':
                                            first_user_title = first_item.get('text', '')[:100]

                                if data.get('message') and data['message'].get('content'):
                                    content = data['message']['content']
                                    text_part, tool_part, thinking_part = self._extract_content_parts(content)

                                    if text_part:
                                        found = pattern.findall(text_part)
                                        if found:
                                            text_count += len(found)
                                            if text_snippet is None:
                                                match_obj = pattern.search(text_part)
                                                if match_obj:
                                                    text_snippet = self._create_snippet(text_part, match_obj.start())

                                    if tool_part:
                                        found = pattern.findall(tool_part)
                                        if found:
                                            tool_count += len(found)
                                            if tool_snippet is None:
                                                match_obj = pattern.search(tool_part)
                                                if match_obj:
                                                    tool_snippet = self._create_snippet(tool_part, match_obj.start())

                                    if thinking_part:
                                        found = pattern.findall(thinking_part)
                                        if found:
                                            thinking_count += len(found)
                                            if thinking_snippet is None:
                                                match_obj = pattern.search(thinking_part)
                                                if match_obj:
                                                    thinking_snippet = self._create_snippet(thinking_part, match_obj.start())

                            except (json.JSONDecodeError, TypeError, AttributeError):
                                continue

                    if text_count > 0 or tool_count > 0 or thinking_count > 0:
                        is_internal = self._is_internal_thread(jsonl_file, first_user_title)
                        matches[conv_id] = {
                            'text_count': text_count,
                            'tool_count': tool_count,
                            'thinking_count': thinking_count,
                            'is_internal': is_internal,
                            'snippet': text_snippet or tool_snippet or thinking_snippet or ''
                        }

                except IOError as e:
                    print(f"Error reading {jsonl_file}: {e}")

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'matches': matches}).encode())

    def log_message(self, format, *args):
        # Suppress request logging
        pass

def is_port_in_use(host, port):
    """Check if something is already listening on host:port."""
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
    """Find a free port starting from start_port."""
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
    """Detect if running inside WSL."""
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
    """Resolve the absolute path to the ccpeek executable."""
    found = shutil.which('ccpeek')
    if found:
        return os.path.realpath(found)
    return os.path.abspath(sys.argv[0])

def is_setup_done():
    """Check if the first-time setup wizard has already run."""
    return os.path.exists(SETUP_MARKER)

def mark_setup_done():
    """Write the marker file so the wizard doesn't repeat."""
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
    """Remove the ccpeek Windows Firewall rule if it exists."""
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f"Get-NetFirewallRule -DisplayName '{FIREWALL_RULE_NAME}' "
         f"-ErrorAction SilentlyContinue"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and FIREWALL_RULE_NAME in result.stdout:
        _run_ps_elevated(
            f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE_NAME}'"
        )
        print(f"Removed firewall rule '{FIREWALL_RULE_NAME}'")


def run_remove():
    """Remove the background service registration."""
    if sys.platform == 'win32':
        _setup_windows_task(0, False)
        _remove_firewall_rule()
    else:
        _setup_systemd_service(0, False)


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
    parser.set_defaults(open_browser=default_open_browser)

    args = parser.parse_args(argv)
    host = args.host

    # --remove: unregister and exit
    if args.remove:
        run_remove()
        sys.exit(0)

    # Setup wizard: on --setup or first interactive launch
    systemd_started = False
    if args.setup or (not is_setup_done() and sys.stdin.isatty()):
        systemd_started = run_setup(args.port)

    # If systemd started successfully, check if it's running and exit
    if systemd_started:
        if is_port_in_use(host, args.port) and verify_ccpeek_instance(host, args.port):
            display_host = resolve_display_host(host)
            print(f"ccpeek is already running at http://{display_host}:{args.port}")
            if args.open_browser and host in LOCAL_HOSTS:
                open_browser(host if host != 'localhost' else '127.0.0.1', args.port)
            sys.exit(0)
        # systemd claimed to start but port not in use - fall through to foreground

    # If an instance is already listening, verify it's ccpeek before redirecting
    if is_port_in_use(host, args.port):
        if verify_ccpeek_instance(host, args.port):
            display_host = resolve_display_host(host)
            print(f"ccpeek is already running at http://{display_host}:{args.port}")
            if args.open_browser and host in LOCAL_HOSTS:
                open_browser(host if host != 'localhost' else '127.0.0.1', args.port)
            sys.exit(0)
        else:
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
        httpd = HTTPServer((host, port), CCPeekHandler)
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
