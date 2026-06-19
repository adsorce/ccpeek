# ccpeek

Claude Code chat history viewer. Single-file Python server (`server.py`) serving a single-page app (`index.html`).

## Status

- MiMoCode/OpenCode transcript discovery must exclude imported Claude sessions across both legacy `claude_import` rows and newer `external_import` rows keyed by `*session_id`; otherwise Claude Code chats reappear as native MiMoCode/OpenCode duplicates.
- Sidebar conversation refresh now patches keyed `.conversation-item` nodes in place, so active search tabs can stay fresh without forcing a full sidebar DOM rebuild on each poll.

## Running

ccpeek runs on port 8888 via the Windows Scheduled Task "CCPeek" (or systemd user service on Linux). The task launches `pythonw.exe server.py --no-browser --port 8888` at login. Do not start a second instance; the port will already be in use.

Always use the already-running ccpeek instance at `http://127.0.0.1:8888` for verification. Do not launch ad hoc foreground servers on alternate ports, because that leaves the main scheduled-task instance stale and makes browser validation misleading.

After editing `server.py`, restart the existing service via the wrapper command:

```
ccpeek --restart
```

Do not use `python server.py`, `python server.py --port ...`, or similar one-off launches for normal development verification unless the user explicitly asks for a separate instance.

For `index.html` (client-side only), a browser refresh against the existing `:8888` instance is sufficient.

## Testing

Test search changes against the live API at `http://127.0.0.1:8888/api/search?q=<term>`. Verify highlight and scroll-to behavior in the browser using CDP tools after loading a conversation with matches. After any `server.py` edit, run `ccpeek --restart` and then re-test against the same `:8888` instance.
