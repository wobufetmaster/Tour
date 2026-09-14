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
3. **Walk** (`#walk/<id>`). Projector view. `Space`/`→`/`j` next, `←`/`k`
   previous, `f` full file, `d` diff for this stop, `g` + number to jump,
   `?` for all keys.

## Test

```
make test          # or: python3 -m unittest discover -s tests
```

## Status

v1 (diff → stops → walkthrough) is done. Next phases per `CLAUDE.md`:
v2 presenter tab and live sync, v3 symbol index, v4 suggested context stops.
