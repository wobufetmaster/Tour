# Tour

Author a code review from a git diff, save it as JSON in the repo, and later
walk a room through it stop by stop, with a presenter tab driving a projector
tab and click-to-definition across every binary in the project.

Python 3 standard library only. Two source files: `tour.py` (server) and
`index.html` (UI). No build step, no dependencies, no network. It replaces a
modified Woboq install; the design is in `CLAUDE.md`.

## Contents

- [Run](#run)
- [Workflow](#workflow)
- [Keys](#keys)
- [Configuration: tour.json](#configuration-tourjson)
- [Review files](#review-files)
- [Symbol index](#symbol-index)
- [Real-world check: kernel modules](#real-world-check-kernel-modules)
- [HTTP API](#http-api)
- [Testing](#testing)
- [Limitations](#limitations)

## Run

```
python3 tour.py [--repo PATH] [--port 8765] [--host 127.0.0.1] [--no-index] [--verbose]
```

Open <http://localhost:8765/>. `--repo` defaults to the current directory and
may be anywhere inside the repo; the root is found with `git rev-parse`.

External tools, all optional and all on `PATH`: `git` (required),
`universal-ctags` (C and JavaScript symbols), `gcc` or `clang` (macro
expansion), `nm` (binary disambiguation).

For a projector on another machine, run with `--host 0.0.0.0` and open
`http://<your-laptop>:8765/#walk/<id>` there. There is no authentication:
anyone on the network who can reach the port can read the repo's files at the
reviewed commits and edit reviews, so keep it on a trusted network.

## Workflow

1. **New review.** On the home page pick base and head and give it a title.
   Base defaults to the merge-base with `main`, head to `HEAD`; the fields take
   anything git understands (branch, tag, hash, `HEAD~3`). Create draft runs
   the diff and makes one `change` stop per hunk, in file order, trimmed to the
   lines that actually changed. A hunk that only deletes lines becomes a
   base-side stop so the removed code can be shown where it lived.

2. **Author** (`#author/<id>`). Changes tree on the left, the diff in the
   middle (unified or side-by-side), the ordered stop list on the right.
   - Click a stop to jump to it; edit its note, presenter note and kind
     (`change`, `context`, `question`) in the panel.
   - Add a stop: drag line numbers (or click one, shift-click another), press
     `n`, type the note. Enter saves, Shift+Enter adds a line, Esc cancels. In
     unified view a selection with removed lines and no added lines refers to
     the base side; in side-by-side view the gutter you drag decides.
   - Reorder by dragging or `Ctrl+↑/↓`. Delete with the button.
   - Click any name in the diff to see its definition; "Add as context stop"
     turns it into a `context` stop.
   - Autosave one second after any change. The status bar shows saved state.

   The result is `.tours/<date>-<slug>.json`. Commit it with the code.

3. **Present** (`#present/<id>`). Open on your laptop. Stop list on the left,
   the code in the middle, and on the right the public note, your private
   presenter note and what's up next. Every move you make is pushed to the
   server, with a heartbeat every two seconds. The status bar says whether a
   projector is connected.

4. **Walk** (`#walk/<id>`). Open on the projector. While a presenter tab is
   live it follows it (stop, view mode, definition peeks) twice a second, and
   reports "following presenter" in the status bar. Close the presenter and it
   becomes standalone within a few seconds, with all the same keys. The URL
   carries the stop number, so a reload lands on the same stop.

Flags (`x`) are appended to the stop in the review file with a timestamp and
author, from either tab, and show up in the other within a heartbeat. Notes
render basic markdown: paragraphs, `code`, fenced blocks, `-` lists, numbered
lists, `**bold**`.

Everything is theme-aware: the title-bar button (or `t` in a walkthrough)
toggles dark and light, the choice is remembered per browser, and the default
follows the OS.

## Keys

Walkthrough and presenter:

| Key | Action |
| --- | --- |
| `Space`, `→`, `j`, `PageDown` | next stop |
| `←`, `k`, `PageUp` | previous stop |
| `Home`, `End` | first, last stop |
| `g` then a number, `Enter` | jump to a stop |
| `f` | full file at this point (scrollable, same highlighting) |
| `d` | the diff for this stop instead of the file |
| `m` | this stop with macros expanded (C only) |
| click a name | jump to its definition; a chooser when ambiguous |
| `Esc` | back to the current stop |
| `x` | flag this stop for follow-up |
| `+`, `-` | font size (remembered) |
| `t` | dark / light |
| `?` | key help |

Author: `n` new stop from the selection, `↑`/`↓` select stop, `Ctrl+↑/↓`
reorder, `Esc` clear selection or close a modal.

## Configuration: tour.json

Optional, in the root of the reviewed repo. Everything has a default; you only
need it once the same name means different things in different binaries.

```json
{
  "name": "flightctl",
  "languages": ["c", "python", "js"],
  "tours_dir": ".tours",
  "targets": {
    "flightctl": {
      "binary": "build/flightctl",
      "sources": ["src/ctl/**", "src/common/**", "include/**"],
      "compile_commands": "build/ctl/compile_commands.json"
    },
    "flightd": {
      "binary": "build/flightd",
      "sources": ["src/daemon/**", "src/common/**", "include/**"],
      "cflags": ["-Iinclude", "-DDAEMON"]
    }
  }
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | directory name | shown on the home page; the implicit target's name |
| `tours_dir` | `.tours` | where reviews and the index live, relative to the repo |
| `languages` | `["c","python","js"]` | informational for now |
| `targets` | one implicit target over `**` | a binary or module, keyed by name |
| `targets.*.sources` | `["**"]` | globs relative to the repo root; `**` crosses directories, `*` and `?` do not |
| `targets.*.binary` | none | path to the built binary or `.ko`; `nm` on it breaks ties between definitions |
| `targets.*.compile_commands` | none | a compile database; flags are looked up per translation unit |
| `targets.*.cflags` | `[]` | fallback preprocessor flags for the whole target |
| `targets.*.compiler` | `gcc` | used with `cflags`; the compile database supplies its own |

A file matching several targets belongs to all of them (shared sources). A
file matching none is indexed with an empty target and still found by lookups.
If a compile database's recorded `directory` no longer exists (generated on
another machine), relative flags are resolved from the repo root instead.

## Review files

`<tours_dir>/<id>.json`, written with two-space indent and sorted keys so
diffs of reviews read well. Unknown keys round-trip untouched.

```json
{
  "schema": 1,
  "id": "2026-09-14-ipc-refactor",
  "title": "IPC refactor",
  "author": "sean",
  "created": "2026-09-14T10:32:00",
  "base": "a1b2c3d…",
  "head": "d4e5f6a…",
  "stops": [
    {
      "id": "s1",
      "file": "src/common/ipc.c",
      "start": 120,
      "end": 148,
      "side": "head",
      "kind": "change",
      "note": "New ring buffer replaces the linked list.",
      "presenter_note": "Remind them we benchmarked this.",
      "flags": [{"at": "2026-09-16T14:02:00", "by": "sean", "text": "Bounds check on wrap"}]
    }
  ]
}
```

`stops` order is the walkthrough order. `side` is `head` (default) or `base`.
`kind` is `change`, `context` or `question`. Line numbers are 1-based and
inclusive. `base` and `head` are full commit hashes. Saves are atomic (temp
file plus rename).

## Symbol index

`<tours_dir>/index.db`, a sqlite file, gitignored automatically through
`<tours_dir>/.gitignore`. Built from the **working tree**, in a background
thread at server start when stale (any tracked file or `tour.json` newer than
the index, or a different file count) and on demand from the home page's
index card or the `⟳ index` button in the author view. Rebuilds go to a temp
file and swap in atomically, so lookups never see a half-built index.

| Language | How | What is recorded |
| --- | --- | --- |
| Python | stdlib `ast`, never ctags | classes, functions (sync and async), methods, top-level assignments |
| C | `ctags --output-format=json` per target | functions, prototypes, macros, types, variables; `static` noted |
| C binaries | `nm --defined-only` per target binary | which names each binary actually contains |
| C macros | `gcc -E -dD` (or clang) per translation unit, ctags on the output | definitions that only exist after preprocessing, mapped back to the macro invocation's `file:line` via line markers; system headers skipped |
| JavaScript | ctags | functions |
| Sencha | regex over `Ext.define` bodies | class, `extend` parent, `alias`, `xtype`, `requires` (top-level properties only) |

Lookup ranking (`GET /api/symbol`): a `static` symbol in the file you clicked
from wins; then the wanted target (given, or inferred from the file when it
belongs to exactly one); then names the target's binary contains; prototypes
and `requires` entries sink. A definition shared by several targets is one
entry listing them all. The UI jumps straight to a clear winner and shows a
chooser otherwise.

Without ctags the index still has Python and Sencha symbols and reports the
warning. Preprocessing failures (missing headers, unknown flags) are recorded
per file on the index card; the rest of the index is unaffected.

## Real-world check: kernel modules

Tour was pointed at two out-of-tree kernel modules built against the
`linux-headers-6.8.0-139-generic` package, on a 4-core box.

**v4l2loopback** (3.4k lines of module C plus a 1.6k-line userspace tool).
Two targets: the `.ko` and the CLI. Index: 478 symbols in 2.3 s, no warnings.
`nm` on the `.ko` works like on any ELF object. The 64 macro-generated
symbols are the real kernel ones: `init_module` from `module_init(...)`, the
`__param_*` family from `module_param(...)`. Clicking `module_param` itself
correctly finds nothing, because that macro lives in kernel headers outside
the repo; ctags had read the invocation as a prototype, and the preprocessed
pass now removes that misparse. `m` on the `module_init` line shows the
expansion the compiler sees, in well under a second even though the file
includes the whole V4L2 stack.

```json
{
  "name": "v4l2loopback",
  "targets": {
    "v4l2loopback.ko": {
      "binary": "v4l2loopback.ko",
      "sources": ["v4l2loopback.c", "v4l2loopback.h", "v4l2loopback_formats.h"],
      "compile_commands": "compile_commands.json"
    },
    "v4l2loopback-ctl": {
      "binary": "utils/v4l2loopback-ctl",
      "sources": ["utils/**", "v4l2loopback.h"],
      "cflags": ["-I."]
    }
  }
}
```

**rtl8812au** (459k lines, 587 files, one `.ko`). Index: 47,892 symbols in
107 s, of which 5,871 macro-generated; one warning, for a Python 2 script in
`tools/`. The driver defines `platform_wifi_power_off` in twelve
`platform/*.c` files; the compile database says which one is built, so the
lookup prefers it. Names defined only in never-compiled files rank below
compiled ones, and where nothing is compiled (some `hal/` variants) you get an
honest chooser. About a dozen `EXPORT_SYMBOL(...)` lines survive as bogus
"prototype" entries, because that macro expands to nothing ctags can tag.

**Generating the compile database.** Kbuild's own generator works for
external modules, but its `-d` is the kernel build directory, not the module
directory, or the recorded `directory` is wrong and every include fails:

```
K=/lib/modules/$(uname -r)/build
make -C $K M=$PWD modules
python3 $K/scripts/clang-tools/gen_compile_commands.py -d $K -o $PWD/compile_commands.json $PWD
```

(`make -C $K M=$PWD compile_commands.json` does the same when the module's
Makefile is plain Kbuild.) A database generated on another machine, whose
`directory` no longer exists, is resolved from the repo root instead.

**What that changed in Tour.** Preprocessing runs in parallel and only for
units the compile database builds; path resolution in the macro pass is
memoized (a preprocessed kernel unit yields tens of thousands of header tags);
the same memoization makes macro expansion sub-second instead of eight
seconds; the index carries a schema version so an index from an older Tour
rebuilds itself instead of breaking the page; four-digit line numbers fit the
gutter.

## HTTP API

All JSON. Everything else serves `index.html`.

```
GET  /api/config                        parsed tour.json with defaults
GET  /api/refs                          HEAD, default base, branches, tags, recent commits
GET  /api/reviews                       [{id, title, author, created, stops}]
GET  /api/review/<id>                   full review
PUT  /api/review/<id>                   save (validates stops, atomic write)
POST /api/review/<id>/flag              {stop_id, text, by?} appends a flag
POST /api/draft                         {base, head, title} -> new review, one stop per hunk
GET  /api/diff?base=..&head=..          [{file, old_file, new_file, status, binary, hunks}]
GET  /api/file?path=..&rev=..           {lines}
GET  /api/state                         presenter position; ?follow=1 marks a projector poll
PUT  /api/state                         {review_id, stop_index, mode, peek?}
GET  /api/symbol?name=..&target=..&file=..   {definitions, preferred, target}
GET  /api/expand?file=..&start=..&end=..&target=..   preprocessed lines for a range
GET  /api/index                         index status, counts per target, warnings
POST /api/index/rebuild                 start a rebuild in the background
```

Paths are normalized and rejected if they could escape the repo; revisions
that look like options are rejected; subprocesses always get argv lists.

## Testing

```
make test          # or: python3 -m unittest discover -s tests
```

Only `unittest`. The fixtures are real git repos made with `git init` in a
temp dir. `tests/helpers.py` also builds a polyglot project: two C binaries
compiled with gcc that share `src/common` and each own an exclusive file, a
function defined differently in each, `static` helpers with the same name,
macro-generated handlers, a compile database for one target and `cflags` for
the other, plus Python tools and Sencha views. The index tests are skipped if
ctags or gcc is missing.

## Limitations

- The index is built from the working tree while reviews are pinned to
  commits. Keep the checkout on the head you are presenting, or expect
  definition lines to drift.
- One presenter at a time: presenter state is a single in-memory slot.
- Syntax highlighting is regex-based and line-oriented. It is meant to be good
  enough on a projector, not a parser.
- Sencha detection only looks at top-level properties of the `Ext.define`
  config object. Aliases built dynamically are not seen.
- Macro expansion preprocesses a whole translation unit per request (under a
  second on a kernel-module file that includes the world); the preprocessed
  text is cached for the eight most recent files.
- Symbols from headers outside the repository (system or kernel headers) are
  not indexed. Clicking `kfree` in a module finds nothing.
- Indexing a large C project means preprocessing every compiled unit; the
  first build of a 500k-line driver takes a couple of minutes and runs in the
  background at server start.
