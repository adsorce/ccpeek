# Project Status

## 2026-06-08

- Planned and started a Codex transcript normalization change to suppress the initial `# AGENTS.md instructions for ...` bootstrap block everywhere.
- Added a regression test target covering title fallback and hidden-by-default event normalization for that Codex bootstrap message.
- Isolated a search regression in `server.py`: all-terms fallback and plain-word scoring were doing expensive whole-corpus counting and regex word-boundary scans on every query.
- Patched the search path to use cached lowercase blobs, token-presence scoring for all-terms fallback, and a cheap boundary check for plain searches; warm endpoint timings on the full local corpus are back near `244 ms` for `main line of the pr` and `222 ms` for `pr`.
- Operational rule: always restart the real app with `ccpeek.cmd`; do not verify changes by running duplicate ad hoc ccpeek server instances on alternate ports.
- Added server-side search supersession keyed by browser client id and request sequence, so older in-flight searches can return `cancelled: true` when a newer query replaces them.
- Added a browser-side search controller with a longer idle delay and duplicate-term suppression to reduce needless search starts while typing.
- Fixed Codex AGENTS bootstrap suppression for the real wire format, where the first user message may append an `<environment_context>...</environment_context>` block after `</INSTRUCTIONS>`.
- Tightened the search box fix scope: hidden top-bar search navigation arrows no longer reserve width when absent, the arrow buttons still hide below `1024px`, and placeholder wrapping no longer grows the search box height; unrelated header/button reflow changes were reverted.
- Restored search and arrow-navigation behavior toward `8f52f98` by excluding normalized tool-call and tool-result payloads from the search index again; added a regression test so sidebar match counts do not get inflated by tool rows the UI hides by default.
- Fixed the remaining sidebar/search-count drift by making `_search_cache` stat transcript files directly instead of trusting stale conversation-cache metadata, so live transcript reads and sidebar badges stay in sync between refresh cycles; title hits no longer inflate the badge count for in-thread navigation.
