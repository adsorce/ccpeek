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
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import socket
import sys

DEFAULT_PORT = 8888
DEFAULT_HOST = '127.0.0.1'
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}
SETUP_MARKER = os.path.expanduser('~/.config/ccpeek/.setup-done')
UNIT_PATH = os.path.expanduser('~/.config/systemd/user/ccpeek.service')

class CCPeekHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'rb') as f:
                self.wfile.write(f.read())
        elif parsed_path.path == '/api/conversations':
            self.handle_conversations()
        elif parsed_path.path.startswith('/api/conversation/'):
            conversation_id = parsed_path.path.split('/')[-1]
            self.handle_conversation(conversation_id)
        else:
            super().do_GET()

    def handle_conversations(self):
        """Get list of all conversations"""
        claude_dir = os.path.expanduser('~/.claude/projects')
        conversations = []

        if os.path.exists(claude_dir):
            for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
                try:
                    # Get first message to extract metadata
                    with open(jsonl_file, 'r') as f:
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

                            conversations.append({
                                'id': os.path.basename(jsonl_file).replace('.jsonl', ''),
                                'path': jsonl_file,
                                'title': title,
                                'timestamp': data.get('timestamp', ''),
                                'modified': stats.st_mtime,
                                'size': stats.st_size
                            })
                except Exception as e:
                    print(f"Error reading {jsonl_file}: {e}")

        # Sort by modified time (newest first)
        conversations.sort(key=lambda x: x['modified'], reverse=True)

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(conversations).encode())

    def handle_conversation(self, conversation_id):
        """Get messages for a specific conversation"""
        claude_dir = os.path.expanduser('~/.claude/projects')
        jsonl_path = None

        # Find the file
        for jsonl_file in glob.glob(os.path.join(claude_dir, '**/*.jsonl'), recursive=True):
            if conversation_id in jsonl_file:
                jsonl_path = jsonl_file
                break

        if not jsonl_path or not os.path.exists(jsonl_path):
            self.send_error(404, 'Conversation not found')
            return

        messages = []
        with open(jsonl_path, 'r') as f:
            for line in f:
                try:
                    data = json.loads(line)
                    messages.append(data)
                except:
                    continue

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(messages).encode())

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

def run_setup(port):
    """Interactive first-time setup wizard."""
    print("── ccpeek setup ──\n")

    try:
        answer = input(
            f"Start ccpeek automatically on login via systemd (port {port})? [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = 'n'

    if answer in ('y', 'yes'):
        bin_path = get_ccpeek_bin()
        unit = (
            "[Unit]\n"
            "Description=ccpeek - Claude Code Chat History Viewer\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={bin_path} --no-browser --port {port}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
        with open(UNIT_PATH, 'w') as f:
            f.write(unit)

        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'ccpeek'], check=True)
        time.sleep(1)  # give the service a moment to bind
        print(f"Registered and started ccpeek on port {port}")
    else:
        if os.path.exists(UNIT_PATH):
            subprocess.run(['systemctl', '--user', 'disable', '--now', 'ccpeek'],
                          capture_output=True)
            os.remove(UNIT_PATH)
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
            print("Removed existing ccpeek systemd service")
        else:
            print("Skipped systemd registration")

    mark_setup_done()
    print("Re-run anytime with: ccpeek --setup\n")


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
    parser.add_argument('--setup', action='store_true', help='Run interactive setup wizard')
    parser.set_defaults(open_browser=default_open_browser)

    args = parser.parse_args(argv)
    host = args.host

    # Setup wizard: on --setup or first interactive launch
    if args.setup or (not is_setup_done() and sys.stdin.isatty()):
        run_setup(args.port)

    # If an instance is already listening, just open the browser and exit
    if is_port_in_use(host, args.port):
        display_host = resolve_display_host(host)
        print(f"ccpeek is already running at http://{display_host}:{args.port}")
        if args.open_browser and host in LOCAL_HOSTS:
            open_browser(host if host != 'localhost' else '127.0.0.1', args.port)
        sys.exit(0)

    # --setup is config-only; don't start a server
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
