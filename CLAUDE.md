# ccpeek

Claude Code chat history viewer. Single-file Python server (`server.py`) serving a single-page app (`index.html`).

## Running

ccpeek runs on port 8888 via the Windows Scheduled Task "CCPeek" (or systemd user service on Linux). The task launches `pythonw.exe server.py --no-browser --port 8888` at login. Do not start a second instance; the port will already be in use.

After editing server.py, restart via `--restart` (which uses schtasks under the hood):

```
python server.py --restart
```

For index.html (client-side only), a browser refresh is sufficient.

## Testing

Test search changes against the live API at `http://127.0.0.1:8888/api/search?q=<term>`. Verify highlight and scroll-to behavior in the browser using claude-in-chrome CDP tools after loading a conversation with matches. Restart the server (see above) after any server.py edit before testing.
