# ccpeek

A sleek, fast viewer for Claude Code chat history stored locally on your machine. Browse, search, and export your AI conversations with a beautiful dark interface inspired by Claude's native design.

![ccpeek screenshot](screenshot.png)

## ✨ Features

### Core Functionality
- **Auto-launching server** - Just type `ccpeek` and your browser opens automatically
- **Zero configuration** - Automatically discovers all Claude Code conversations in `~/.claude/projects/`
- **Lightning-fast search** - Search across all conversations and messages simultaneously
- **Smart filtering** - Hide/show tool uses and results for cleaner reading
- **Export to Markdown** - Download any conversation as a formatted `.md` file
- **Real-time updates** - Auto-refreshes every 30 seconds to catch new conversations

### UI/UX Features
- **Claude-inspired design** - Familiar dark theme matching Claude's interface
- **Collapsible sidebar** - More room for reading when you need it
- **Search highlighting** - Yellow highlights with orange for current match
- **Match navigation** - Previous/next buttons to jump between search results
- **Match counting** - See how many matches in each conversation
- **Copy messages** - One-click copy for any message
- **Thinking blocks** - View Claude's thinking process in special formatted blocks
- **Tool use indicators** - Clear visual distinction between messages, tools, and results

### Smart Filtering
- **Local command filtering** - Automatically hides `/model` and other local commands
- **Tool toggle** - Hide all tool uses and results for distraction-free reading
- **Export respects filters** - Markdown exports honor your current filter settings

### Keyboard Shortcuts
- `/` - Focus search
- `Esc` - Clear search
- `j/k` - Navigate conversations
- `Enter` - Open selected conversation
- `n/p` - Next/previous search result
- `?` - Show keyboard shortcuts

## 🚀 Installation

### Prerequisites
- Python 3.x installed
- Claude Code installed with existing chat history in `~/.claude/projects/`

### Install Steps

1. Clone the repository:
```bash
git clone https://github.com/yourusername/ccpeek.git
cd ccpeek
```

2. Make the scripts executable:
```bash
chmod +x ccpeek server.py
```

3. Create a system-wide command (requires sudo):
```bash
sudo ln -sf $(pwd)/ccpeek /usr/local/bin/ccpeek
```

That's it! Now you can run `ccpeek` from anywhere.

## 📖 Usage

Simply run from your terminal:
```bash
ccpeek
```

This will:
1. On first run, ask if you want ccpeek to start automatically on login (systemd)
2. Start a local server on port 8888 (or connect to an already-running instance)
3. Automatically open your default browser
4. Load all your Claude Code conversations

### First-Run Setup

On the very first launch, ccpeek asks whether to register itself as a systemd user service:

```
$ ccpeek
── ccpeek setup ──

Start ccpeek automatically on login via systemd (port 8888)? [y/N] y
Registered and started ccpeek on port 8888
Re-run anytime with: ccpeek --setup

ccpeek is already running at http://127.0.0.1:8888
```

Choosing **N** (the default) skips registration — ccpeek starts a normal foreground server instead:

```
$ ccpeek
── ccpeek setup ──

Start ccpeek automatically on login via systemd (port 8888)? [y/N]
Skipped systemd registration
Re-run anytime with: ccpeek --setup

CCPeek server starting on http://127.0.0.1:8888
Press Ctrl+C to stop
```

Either way, if ccpeek is already running when you type `ccpeek`, it simply opens your browser — no duplicate servers:

```
$ ccpeek
ccpeek is already running at http://127.0.0.1:8888
```

Re-run the setup wizard anytime with `ccpeek --setup`.

### Viewing Conversations
- Click any conversation in the sidebar to load it
- Use `j/k` keys to navigate with keyboard
- Press `Enter` to open the selected conversation

### Searching
- Type in the search box or press `/` to focus
- Search works across conversation titles AND message content
- Use `n/p` to navigate between matches
- Click the `✕` to clear search

### Filtering Tools
- Click "Show/Hide Tools" to toggle tool visibility
- When hidden, tool uses and results are completely filtered out
- Export respects your current filter setting

### Exporting
- Select a conversation first
- Click "Export" button
- Downloads as a formatted Markdown file

## 🛠️ Technical Details

### Architecture
- **Backend**: Python HTTP server with automatic browser launching
- **Frontend**: Vanilla JavaScript with no dependencies
- **Storage**: Reads JSONL files from `~/.claude/projects/`
- **Singleton**: Detects running instances and reuses them instead of starting duplicates

### WSL2 Support
When running inside WSL2, ccpeek automatically detects the environment and opens your default **Windows** browser via `cmd.exe /c start` instead of `xdg-open`, which avoids silent failures caused by the lack of a controlling TTY in background/daemon contexts.

### Running as a systemd Service
On first launch, ccpeek will ask if you'd like to register it as a systemd user service that starts automatically on login. You can re-run this prompt anytime:

```bash
ccpeek --setup
```

To manage the service manually:
```bash
systemctl --user status ccpeek    # check status
systemctl --user restart ccpeek   # restart
systemctl --user disable ccpeek   # stop auto-start
```

### File Structure
```
ccpeek/
├── ccpeek              # Main executable script
├── server.py           # Python server with API endpoints
├── index.html          # Single-page web application
├── contrib/
│   └── ccpeek.service  # systemd user service unit
└── README.md           # This file
```

### API Endpoints
- `GET /` - Serves the main HTML interface
- `GET /api/conversations` - Returns list of all conversations with metadata
- `GET /api/conversation/{id}` - Returns all messages for a specific conversation

## 🎨 Customization

### Changing the Port
```bash
ccpeek --port 9999
```
Or set it permanently via environment variable: `export CCPEEK_PORT=9999`

### Modifying the Theme
All styles are contained in `index.html`. Look for the `<style>` section to customize colors, fonts, and layout.

## 🐛 Troubleshooting

### "Permission denied" when running ccpeek
Make sure the scripts are executable:
```bash
chmod +x ~/ccpeek/ccpeek ~/ccpeek/server.py
```

### "Command not found: ccpeek"
The symlink might not be created. Run:
```bash
sudo ln -sf ~/ccpeek/ccpeek /usr/local/bin/ccpeek
```

### Port already in use
If ccpeek detects something already listening on port 8888, it assumes it's an existing instance and opens the browser to it. To use a different port: `ccpeek --port 9999`.

### No conversations showing
Ensure you have Claude Code chat history in `~/.claude/projects/`. The tool only shows existing conversations.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

### Development Setup
1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

### Ideas for Contribution
- Add support for other AI chat formats
- Implement conversation search/filter by date
- Add syntax highlighting for code blocks
- Create themes (light mode, custom colors)
- Add conversation statistics and analytics
- Implement conversation merging
- Add support for attachments/images

## 📄 License

This project is open source and available under the [MIT License](LICENSE).

## 🙏 Acknowledgments

- Inspired by [Claude Code](https://claude.ai/code) by Anthropic
- Built for the Claude Code community
- Special thanks to all contributors

## 📊 Stats

- **Language**: Python (backend) + HTML/CSS/JavaScript (frontend)
- **Dependencies**: None (uses Python standard library only)
- **Size**: < 50KB total
- **Performance**: Handles thousands of conversations smoothly

---

**Note**: This tool is not officially affiliated with Anthropic or Claude. It's a community project designed to enhance the Claude Code experience.

## 📸 Screenshots

### Main Interface
![Main Interface](screenshots/main.png)

### Search Functionality
![Search](screenshots/search.png)

### Tool Filtering
![Filtering](screenshots/filtering.png)

### Export Options
![Export](screenshots/export.png)

---

Made with ❤️ for the Claude Code community