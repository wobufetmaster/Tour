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

## Test

```
make test          # or: python3 -m unittest discover -s tests
```

## Status

v1 (diff → stops → walkthrough) and v2 (presenter tab, live sync, flags) are
done. Next per `CLAUDE.md`: v3 symbol index, v4 suggested context stops.
