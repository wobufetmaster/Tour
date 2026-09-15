# Tour

Author a code review from a git diff, save it as JSON in the repo, and later
walk a room through it stop by stop.

Python 3 standard library only. Two source files: `tour.py` (server) and
`index.html` (UI). No build step, no dependencies, no network.

## Run

```
python3 tour.py --repo /path/to/repo      # defaults: --repo . --port 8765
```

Open <http://localhost:8765/>. Add `--host 0.0.0.0` to reach it from the
projector machine.

Optional `tour.json` in the repo root sets the project name and where reviews
are stored (default `.tours/`). See `CLAUDE.md` for the full schema.

## Workflow

1. **New review.** Pick base and head (defaults: merge-base with `main`, and
   `HEAD`), give it a title. Every diff hunk becomes a draft stop.
2. **Author** (`#author/<id>`). Click-drag line numbers, press `n`, write a
   note. Drag stops to reorder, or `Ctrl+↑/↓`. Everything autosaves to
   `.tours/<id>.json`; commit that file.
3. **Present** (`#present/<id>`). Open this on your laptop: stop list, the
   public note, your private presenter note, and what's up next.
4. **Walk** (`#walk/<id>`). Put this on the projector. While a presenter tab
   is open it follows it (stop and view mode, twice a second). On its own it
   works standalone with the same keys: `Space`/`→`/`j` next, `←`/`k`
   previous, `f` full file, `d` diff for this stop, `g` + number to jump,
   `x` flag this stop for follow-up, `t` theme, `?` for all keys.

Flags are appended to the stop in the review file with a timestamp, so the
follow-ups from a walkthrough end up in git with the review.

## Symbols

Click any name in a walkthrough, presenter tab or diff to jump to its
definition (Esc returns). When a name means different things in different
binaries you get a chooser; otherwise it jumps straight there. `m` in a
walkthrough shows a C stop with its macros expanded.

The index lives in `.tours/index.db` (gitignored automatically), is built from
the working tree at server start when stale, and can be rebuilt from the home
page. It needs universal-ctags on the path for C and JavaScript; Python is
indexed with the standard library alone.

- **Python:** classes, functions, methods, top-level assignments (stdlib `ast`).
- **C:** ctags per target, tagged with the target name; `static` symbols
  resolve to the file you clicked from; `nm` on each target's binary breaks
  ties; every translation unit is preprocessed (`compile_commands.json` or
  `cflags`) so macro-generated definitions map back to the macro invocation.
- **JavaScript / Sencha:** ctags for functions, plus `Ext.define` classes with
  their `extend`, `alias`, `xtype` and `requires`, so an xtype resolves to
  its class.

Targets come from `tour.json` (see `CLAUDE.md`); a repo without targets is one
implicit target covering everything.

## Test

```
make test          # or: python3 -m unittest discover -s tests
```

## Status

v1 (diff → stops → walkthrough), v2 (presenter tab, live sync, flags) and
v3 (symbol index, click-to-definition, macro expansion) are done. Next per
`CLAUDE.md`: v4 suggested context stops.
