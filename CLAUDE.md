# ccpeek

Claude Code chat history viewer. Single-file Python server (`server.py`) serving a single-page app (`index.html`).

## Running

ccpeek registers itself as a Windows Scheduled Task (or systemd user service on Linux) via `ccpeek --setup`. It runs `pythonw.exe server.py --no-browser --port 8888` in the background on login.

After editing server.py, restart the background instance to pick up changes. For index.html (client-side only), a browser refresh is sufficient.

```
python server.py --restart
```

Or on Windows with the cmd wrapper:

```
ccpeek --restart
```
