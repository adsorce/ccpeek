# Codex Transcript Support for ccpeek

## Summary
Adapt ccpeek to read local Codex CLI sessions while preserving the existing Claude Code UX. Codex should fit into the current ccpeek mental model: unified sidebar, shared search, shared reader, shared tool filtering, shared export, and the same bias toward hiding log noise by default.

## Non-Negotiable Constraints
- Preserve current Claude behavior unless a change is strictly required to support source-agnostic rendering.
- Do not create a separate Codex page, separate router, or source switch.
- Do not invent new product concepts for Codex if the current Claude behavior already covers them.
- Do not add guessed Codex resume behavior in v1.
- Prefer omission or hidden-by-default treatment over exposing raw Codex bookkeeping/log noise.

## Phase 1: Normalize the Backend
- Keep work in `server.py`; do not split the project into new modules in this pass.
- Introduce a normalized conversation summary shape returned by `/api/conversations`:
  - `id`, `source`, `title`, `timestamp`, `modified`, `path`, `project_dir`, `parent_id`, `is_background`
- Introduce a normalized transcript event shape returned by `/api/conversation/{id}`:
  - `source`, `role`, `kind`, `timestamp`, `text`, `name`, `payload`, `hidden_by_default`
- Namespace conversation IDs:
  - Claude: `claude:<raw_id>`
  - Codex: `codex:<session_id>`
- Refactor current Claude loading to emit normalized summaries and normalized transcript events instead of raw JSONL rows.
- Preserve current Claude metadata behavior:
  - title extraction
  - internal-thread filtering
  - subagent parent/child handling
  - background job metadata

## Phase 2: Add Codex Source Support
- Add a Codex source adapter inside `server.py`.
- Read Codex data from:
  - `~/.codex/sessions/**/rollout-*.jsonl`
  - `~/.codex/session_index.jsonl`
- Use `session_index.jsonl` as the primary title source.
- If a session index entry is missing, fall back to the first visible user text found in the session log; otherwise use a stable untitled fallback.
- Codex event mapping:
  - `session_meta`: summary metadata only
  - `response_item.message`: transcript event with preserved role (`user`, `assistant`, `developer`)
  - `event_msg.agent_message`: assistant commentary event
  - `response_item.function_call`: tool-use event
  - `response_item.function_call_output`: tool-result event
  - `response_item.reasoning`: skip entirely in v1
  - `turn_context`, `token_count`, `task_started`, and other bookkeeping events: ignore
- Codex default visibility rules:
  - normal user messages: visible
  - assistant messages: visible
  - assistant commentary: visible
  - tool calls/results: visible, subject to the existing tool toggle
  - developer/bootstrap scaffolding: hidden by default
  - encrypted reasoning: omitted

## Phase 3: Make Search Source-Agnostic
- Replace Claude-specific search indexing with indexing over normalized visible transcript events.
- Search should index:
  - visible message text
  - visible assistant commentary text
  - visible tool input text
  - visible tool output text
  - conversation titles
- Preserve current search behavior:
  - shared global search
  - snippets from matching content
  - whole-word scoring preference
  - no separate search UX for Codex

## Phase 4: Adapt the Existing UI
- Keep work in `index.html`; do not rebuild the UI structure.
- Update the frontend to consume normalized summaries and normalized transcript events instead of Claude raw rows.
- Keep one unified conversation list.
- Add only minimal source disambiguation:
  - a source badge in the sidebar item and/or header
- Update message classification/rendering to use normalized `role` and `kind`.
- Preserve current reader semantics:
  - normal messages render as chat messages
  - tool-use and tool-result events render through the existing tool UI
  - hidden/default-filtered Codex scaffolding should not clutter the transcript
- Preserve existing toggles:
  - the current tool toggle applies to Codex tool events too
- Keep the current resume bar Claude-only:
  - show for Claude where it works now
  - hide for Codex

## Phase 5: Export
- Reimplement export on top of normalized transcript events.
- Preserve the current export flow and user action.
- Respect current visibility rules:
  - hidden tool events stay hidden when tools are hidden
  - hidden-by-default Codex scaffolding stays excluded
- Export section labels should be derived from normalized roles/kinds and remain readable in Markdown.

## Verification Gates
- Gate 1: `server.py` still parses and the app still starts.
- Gate 2: existing Claude conversations behave the same in list, detail, search, tool filtering, export, and resume.
- Gate 3: Codex sessions appear in the unified sidebar with correct source labeling and stable titles.
- Gate 4: Codex transcript rendering shows:
  - user messages
  - assistant messages
  - assistant commentary
  - tool calls
  - tool outputs
  and does not show bookkeeping noise or encrypted reasoning.
- Gate 5: search returns Codex matches from messages, commentary, tool inputs, and tool outputs.
- Gate 6: export produces readable Markdown for both Claude and Codex.

## Required Test Scenarios
- Claude regression:
  - ordinary Claude conversation
  - Claude conversation with tool calls/results
  - Claude subagent/child conversation
- Codex happy path:
  - session with user + assistant messages
  - session with assistant commentary
  - session with function call and function call output
- Codex noise filtering:
  - session containing developer/bootstrap scaffolding
  - session containing reasoning events
  - session containing bookkeeping events only between visible transcript events
- Error handling:
  - malformed JSONL row
  - missing `session_index.jsonl`
  - unknown Codex event type
