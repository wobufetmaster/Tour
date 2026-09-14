# Tour — Code Review & Walkthrough Tool

This replaces a heavily modified Woboq install used for code reviews. Reviews are
authored from a git diff, saved as JSON in the repo, and later presented live to a
room as a step-by-step walkthrough.

## Hard constraints (do not violate)

- **Air-gapped. Python 3 standard library ONLY.** No pip, no npm, no CDN, no vendored
  third-party code. If you think you need a library, you don't.
- **Exactly two source files:** `tour.py` and `index.html`. All CSS and JS live inside
  `index.html`. No build step.
- Runs on Linux. May shell out to: `git`, `ctags` (universal-ctags), `gcc`, `clang`,
  `nm`, and any standard coreutils.
- Repos are git. Every review is pinned to a base and head commit.
- Must handle C (gcc and clang projects), Python, and JavaScript (Sencha ExtJS).
- Maintainer budget is ~half of one engineer. Prefer boring, obvious code over clever
  code. Fewer features that work beat many that are flaky.

## Build order — do NOT skip ahead

Ship each phase working before starting the next. Phase 1 is the whole point.

1. **v1 — diff → stops → walkthrough.** Server, config loading, `git diff`, draft
   generation, authoring UI, JSON save/load, walkthrough viewer. No symbol indexing.
2. **v2 — presentation.** Presentation mode, presenter tab with private notes, live
   flagging during walkthrough, sync between tabs.
3. **v3 — symbols.** Per-target ctags index, click-to-definition, preprocessed-C
   indexing for macro-generated symbols, Sencha `Ext.define` pass.
4. **v4 — smart drafts.** Suggested "context" stops for definitions/callers of changed
   symbols.

## Running it

```
python3 tour.py [--repo PATH] [--port 8765]
```

Opens `http://localhost:8765/`. `--repo` defaults to cwd; the repo root is found via
`git rev-parse --show-toplevel`. Reads `tour.json` (config) from the repo root.

## Config file: `<repo>/tour.json`

```json
{
  "name": "flightctl",
  "languages": ["c", "python", "js"],
  "tours_dir": ".tours",
  "targets": {
    "flightctl": {
      "binary": "build/flightctl",
      "sources": ["src/ctl/**", "src/common/**"],
      "compile_commands": "build/ctl/compile_commands.json"
    },
    "flightd": {
      "binary": "build/flightd",
      "sources": ["src/daemon/**", "src/common/**"],
      "cflags": ["-Iinclude", "-DDAEMON"]
    }
  }
}
```

- `targets` is optional; only needed for v3 symbol scoping. A repo with one binary can
  omit it entirely.
- `binary`, `compile_commands`, `cflags` are all optional per target. `sources` are
  glob patterns relative to the repo root.
- `compile_commands` is preferred for flags; `cflags` is the fallback for gcc projects
  without one.

## Review file: `<repo>/.tours/<id>.json`

```json
{
  "schema": 1,
  "id": "2026-09-14-ipc-refactor",
  "title": "IPC refactor",
  "author": "sean",
  "created": "2026-09-14T10:32:00",
  "base": "a1b2c3d",
  "head": "d4e5f6a",
  "stops": [
    {
      "id": "s1",
      "file": "src/common/ipc.c",
      "start": 120,
      "end": 148,
      "side": "head",
      "kind": "change",
      "note": "New ring buffer replaces the linked list. Lock-free for single producer.",
      "presenter_note": "Remind them we benchmarked this against the mutex version.",
      "flags": [
        {"at": "2026-09-16T14:02:00", "by": "sean", "text": "Follow up: bounds check on wrap"}
      ]
    }
  ]
}
```

- `stops` is an ordered array; order is the walkthrough order.
- `side` is `"head"` (default) or `"base"` — which version of the file the range refers to.
- `kind` is one of `change`, `context`, `question`.
- `note` is shown to the room. `presenter_note` is only shown in the presenter tab.
- `flags` are appended live during walkthroughs and never removed by the tool.
- Line numbers are 1-based, inclusive.
- Never reorder or rewrite fields the tool doesn't understand; round-trip unknown keys.
- Write files with `indent=2` and sorted keys so git diffs of reviews are readable.

## HTTP API (all JSON)

```
GET  /api/config                      -> parsed tour.json
GET  /api/reviews                     -> list of {id, title, author, created}
GET  /api/review/<id>                 -> full review file
PUT  /api/review/<id>                 -> save full review file (atomic write: tmp + rename)
POST /api/review/<id>/flag            -> {stop_id, text} appends a flag, returns review
POST /api/draft                       -> {base, head, title} -> new review with one stop per diff hunk
GET  /api/diff?base=..&head=..        -> parsed unified diff: [{file, hunks:[{old_start, old_len, new_start, new_len, lines}]}]
GET  /api/file?path=..&rev=..         -> {lines: [...]} contents at a commit (git show)
GET  /api/refs                        -> recent commits, branches, tags for pickers
GET  /api/state                       -> {review_id, stop_index} current presenter position (v2)
PUT  /api/state                       -> set presenter position (v2)
GET  /api/symbol?name=..&target=..    -> definitions [{file, line, kind, target}] (v3)
```

Everything else serves `index.html`. Reject any `path` that escapes the repo root.
Never use `shell=True`; pass argv lists to `subprocess.run`.

## UI

Three views, all in `index.html`, switched by URL hash.

### `#author/<id>` — authoring

The goal is that nobody starts from a blank page.

- Left: file list from the diff, with hunk counts. Right: side-by-side or unified diff
  (toggle), syntax highlighted.
- Below/right: the ordered stop list. Drag to reorder; also `Ctrl+↑/↓` moves the
  selected stop.
- Create a stop: click-drag line numbers (or click first, shift-click last) then press
  `n`. A note box opens inline. `Enter` saves, `Esc` cancels.
- Every stop in the list shows file:lines, kind badge, first line of note.
- Autosave via `PUT` 1s after any change. Show a small "saved" indicator.
- New review flow: pick base/head from `/api/refs` (default base = merge-base with
  main, head = HEAD), title, then `POST /api/draft`. The draft has one `change` stop
  per hunk in file order; the author edits from there.

### `#walk/<id>` — walkthrough (projector)

Minimal chrome. This is shown on a projector to a room.

- Large monospace font (configurable, default 18px). Current stop's range is
  highlighted; show ~8 lines of context above and below, dimmed.
- Note rendered in a panel beside or below the code (basic markdown: paragraphs,
  `code`, lists, bold).
- Header: review title, `file:start-end`, "stop 7 / 23".
- Keys: `Space`/`→`/`j` next, `←`/`k` previous, `Home`/`End` first/last,
  `f` open the full file at this point (scrollable, same highlighting), `Esc` return to
  the current stop, `d` toggle showing the diff for this stop instead of head file,
  `g` then a number to jump to a stop, `?` key help overlay.
- `x` (v2): flag the current stop — prompts for text, `POST /flag`.

### `#present/<id>` — presenter tab (v2)

Same as walkthrough but shows `presenter_note`, the upcoming stop's title, and the
list of stops. Advancing in the presenter tab updates `/api/state`; the `#walk` tab
polls `/api/state` every 500ms and follows. The `#walk` tab also works standalone with
its own keys if no presenter tab is open.

## Syntax highlighting

Hand-written tokenizers in JS, one per language: comments, strings, numbers,
keywords, preprocessor lines (C). Regex-based, line-oriented, good enough. Do not
attempt a full parser. Keep C's multi-line comment state across lines.

## Language handling (v3)

Symbol index is stored in `sqlite3` at `<repo>/.tours/index.db` (gitignored).
Table: `symbols(name, kind, file, line, target, scope)`. Rebuild on demand via a
button and on server start if stale (compare mtimes).

- **Python:** use stdlib `ast`. Walk every `.py`, record `FunctionDef`,
  `AsyncFunctionDef`, `ClassDef`, top-level `Assign` names. Do not use ctags for Python.
- **C, basic:** `ctags --output-format=json --fields=+nSK --kinds-c=+p -R` over each
  target's `sources`, tagging results with the target name. Same name in different
  targets is expected and correct. `static` symbols resolve by file.
- **C, disambiguation via nm:** if `binary` exists, run `nm --defined-only` on it; a
  symbol present in a target's binary is preferred when the name is ambiguous.
- **C, macros:** `#define`s are tagged by ctags directly. For macro-generated
  definitions (e.g. `DEFINE_HANDLER(foo)` producing `foo_handler`), preprocess each
  translation unit with `gcc -E -dD` (or `clang -E -dD`) using the target's flags,
  run ctags on the preprocessed output, and map each result back to the original
  `file:line` using the `# <line> "<file>"` markers in the output. Skip lines
  originating from system headers. Store these with `kind = "macro-generated"`.
  Also expose `GET /api/expand?file=..&start=..&end=..&target=..` returning the
  preprocessed text for a range, for an "expand macros" toggle in the walkthrough.
- **JavaScript / Sencha:** run ctags for plain functions, then a regex pass over `.js`
  files for `Ext.define('Name', {`, `extend: 'Name'`, `alias: 'widget.x'`,
  `xtype: 'x'`, `requires: [...]`. Record the defined class name, its `extend`
  parent, and aliases as symbols so `xtype: 'gridpanel'` can resolve to the class.

## Non-goals

- Not a code browser for the whole repo. It shows files at a commit; that's it.
- No user accounts, no auth, no database beyond the symbol index. It runs on one
  machine; the JSON in git is the source of truth.
- No comments threads, approvals, or merge gating. Flags during a walkthrough are the
  only "discussion" feature.

## Testing

Include a `tests/` directory using only `unittest`: diff parsing, draft generation
from a fixture repo (create it with `git init` in a tempdir), review round-trip, path
escape rejection, line-marker mapping for preprocessed C. Provide a `make test` or
`python3 -m unittest`.
