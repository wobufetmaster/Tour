#!/usr/bin/env python3
"""Tour: author code reviews from a git diff and walk a room through them.

Python 3 standard library only.  This file is the whole server; the UI lives
in index.html next to it.

    python3 tour.py [--repo PATH] [--port 8765]
"""

import argparse
import ast
import concurrent.futures
import datetime
import json
import os
import posixpath
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SCHEMA = 1
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")
MAX_BODY = 64 * 1024 * 1024

DEFAULT_CONFIG = {
    "name": "",
    "languages": ["c", "python", "js"],
    "tours_dir": ".tours",
    "targets": {},
}

REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STOP_KINDS = ("change", "context", "question")
VIEW_MODES = ("stop", "full", "diff", "peek", "expand")


class TourError(Exception):
    """An error that is reported to the client as an HTTP error."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Subprocesses and git
# ---------------------------------------------------------------------------


def run(argv, cwd=None, status=500, input_text=None, timeout=None):
    """Run argv (never a shell) and return stdout as text."""
    try:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              input=input_text.encode("utf-8") if input_text is not None else None,
                              timeout=timeout)
    except OSError as e:
        raise TourError("cannot run %s: %s" % (argv[0], e), 500)
    except subprocess.TimeoutExpired:
        raise TourError("%s timed out after %ss" % (argv[0], timeout), 500)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise TourError("%s failed: %s" % (" ".join(argv[:2]), err or "exit %d" % proc.returncode), status)
    return proc.stdout.decode("utf-8", "replace")


def git(repo, *args):
    return run(["git", "-C", repo] + list(args), status=400)


def check_rev(rev):
    """A revision must be a plain name or hash, never something git could read as an option."""
    if not isinstance(rev, str) or not rev.strip():
        raise TourError("missing revision")
    rev = rev.strip()
    if rev.startswith("-") or any(c.isspace() or ord(c) < 32 for c in rev) or ":" in rev:
        raise TourError("bad revision: %r" % rev)
    return rev


def rev_parse(repo, rev):
    """Resolve any revision to a full commit hash."""
    rev = check_rev(rev)
    out = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}").strip()
    if not out:
        raise TourError("unknown revision: %s" % rev)
    return out


def find_repo_root(path):
    if not os.path.isdir(path):
        raise TourError("no such directory: %s" % path)
    return git(path, "rev-parse", "--show-toplevel").strip()


def default_author(repo):
    try:
        name = git(repo, "config", "user.name").strip()
        if name:
            return name
    except TourError:
        pass
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


# ---------------------------------------------------------------------------
# Config and paths
# ---------------------------------------------------------------------------


def load_config(repo):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["name"] = os.path.basename(repo.rstrip(os.sep))
    path = os.path.join(repo, "tour.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                user = json.load(f)
            except ValueError as e:
                raise TourError("tour.json is not valid JSON: %s" % e, 500)
        if not isinstance(user, dict):
            raise TourError("tour.json must contain a JSON object", 500)
        cfg.update(user)
    if not isinstance(cfg.get("tours_dir"), str) or not cfg["tours_dir"]:
        raise TourError("tour.json: tours_dir must be a non-empty string", 500)
    return cfg


def safe_rel_path(rel):
    """Normalize a repo-relative POSIX path.  Rejects anything that could escape the repo."""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise TourError("bad path")
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise TourError("absolute paths are not allowed")
    norm = posixpath.normpath(rel.replace("\\", "/"))
    if norm in (".", "..") or norm.startswith("../") or "/../" in norm or norm.endswith("/.."):
        raise TourError("path escapes the repository")
    return norm


def repo_path(repo, rel):
    """Absolute filesystem path for a repo-relative path, verified to be inside the repo."""
    norm = safe_rel_path(rel)
    root = os.path.realpath(repo)
    abs_path = os.path.realpath(os.path.join(root, norm))
    if abs_path != root and not abs_path.startswith(root + os.sep):
        raise TourError("path escapes the repository")
    return abs_path


# ---------------------------------------------------------------------------
# Review files
# ---------------------------------------------------------------------------


def tours_dir(repo, cfg):
    d = repo_path(repo, cfg["tours_dir"])
    os.makedirs(d, exist_ok=True)
    return d


def check_review_id(rid):
    if not isinstance(rid, str) or not REVIEW_ID_RE.match(rid):
        raise TourError("bad review id: %r" % (rid,))
    return rid


def review_path(repo, cfg, rid):
    return os.path.join(tours_dir(repo, cfg), check_review_id(rid) + ".json")


def write_json_atomic(path, data):
    """Write JSON to a temp file in the same directory, then rename over the target."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_reviews(repo, cfg):
    out = []
    d = tours_dir(repo, cfg)
    for name in sorted(os.listdir(d)):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict):
            continue
        out.append({
            "id": r.get("id") or name[:-5],
            "title": r.get("title", ""),
            "author": r.get("author", ""),
            "created": r.get("created", ""),
            "stops": len(r.get("stops") or []),
        })
    out.sort(key=lambda r: str(r["created"]), reverse=True)
    return out


def load_review(repo, cfg, rid):
    path = review_path(repo, cfg, rid)
    if not os.path.exists(path):
        raise TourError("no such review: %s" % rid, 404)
    with open(path, encoding="utf-8") as f:
        try:
            review = json.load(f)
        except ValueError as e:
            raise TourError("%s.json is not valid JSON: %s" % (rid, e), 500)
    if not isinstance(review, dict):
        raise TourError("%s.json does not contain an object" % rid, 500)
    return review


def save_review(repo, cfg, rid, review):
    """Save a whole review.  Unknown keys are kept as they are."""
    if not isinstance(review, dict):
        raise TourError("review must be a JSON object")
    review["id"] = check_review_id(rid)
    review.setdefault("schema", SCHEMA)
    stops = review.get("stops")
    if not isinstance(stops, list):
        raise TourError("review.stops must be a list")
    seen = set()
    for stop in stops:
        if not isinstance(stop, dict):
            raise TourError("every stop must be an object")
        sid = stop.get("id")
        if not isinstance(sid, str) or not sid or sid in seen:
            raise TourError("every stop needs a unique string id")
        seen.add(sid)
        safe_rel_path(stop.get("file"))
        for key in ("start", "end"):
            if not isinstance(stop.get(key), int) or stop[key] < 1:
                raise TourError("stop %s: %s must be a positive integer" % (sid, key))
        if stop["end"] < stop["start"]:
            raise TourError("stop %s: end is before start" % sid)
        if stop.get("side", "head") not in ("head", "base"):
            raise TourError("stop %s: side must be head or base" % sid)
        if stop.get("kind", "change") not in STOP_KINDS:
            raise TourError("stop %s: kind must be one of %s" % (sid, ", ".join(STOP_KINDS)))
    write_json_atomic(review_path(repo, cfg, rid), review)
    return review


def add_flag(repo, cfg, rid, stop_id, text, by):
    if not isinstance(text, str) or not text.strip():
        raise TourError("flag text is required")
    review = load_review(repo, cfg, rid)
    for stop in review.get("stops") or []:
        if isinstance(stop, dict) and stop.get("id") == stop_id:
            flags = stop.get("flags")
            if not isinstance(flags, list):
                flags = stop["flags"] = []
            flags.append({"at": now_iso(), "by": by, "text": text.strip()})
            break
    else:
        raise TourError("no such stop: %s" % stop_id, 404)
    write_json_atomic(review_path(repo, cfg, rid), review)
    return review


# ---------------------------------------------------------------------------
# Diffs and drafts
# ---------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_name(field):
    """'--- a/foo.c' -> 'foo.c'; '/dev/null' -> None."""
    name = field.split("\t", 1)[0].strip()
    if name == "/dev/null":
        return None
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        try:
            name = json.loads(name)
        except ValueError:
            name = name[1:-1]
    if name[:2] in ("a/", "b/"):
        name = name[2:]
    return name


def parse_unified_diff(text):
    """Parse `git diff` output into a list of files with hunks.

    Each hunk's `lines` keep their leading ' ', '+' or '-'.  Lines are counted
    against the hunk header so that '---'/'+++' headers of the next file are
    never mistaken for removed lines.
    """
    files = []
    cur = None
    hunk = None
    old_left = new_left = 0
    for line in text.split("\n"):
        if hunk is not None and (old_left > 0 or new_left > 0):
            tag = line[:1]
            if tag == "\\":
                continue  # "\ No newline at end of file"
            if tag == " " or line == "":
                old_left -= 1
                new_left -= 1
                hunk["lines"].append(line if line else " ")
                continue
            if tag == "-":
                old_left -= 1
                hunk["lines"].append(line)
                continue
            if tag == "+":
                new_left -= 1
                hunk["lines"].append(line)
                continue
            hunk = None  # malformed; fall through and treat as a header line
        if line.startswith("diff --git "):
            cur = {"file": None, "old_file": None, "new_file": None,
                   "status": "modified", "binary": False, "hunks": []}
            m = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old_file"], cur["new_file"] = m.group(1), m.group(2)
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m:
                continue
            hunk = {
                "old_start": int(m.group(1)), "old_len": int(m.group(2) or 1),
                "new_start": int(m.group(3)), "new_len": int(m.group(4) or 1),
                "lines": [],
            }
            old_left, new_left = hunk["old_len"], hunk["new_len"]
            cur["hunks"].append(hunk)
        elif line.startswith("--- "):
            cur["old_file"] = _diff_name(line[4:])
        elif line.startswith("+++ "):
            cur["new_file"] = _diff_name(line[4:])
        elif line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["status"] = "renamed"
            cur["old_file"] = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur["new_file"] = line[len("rename to "):]
        elif line.startswith("Binary files"):
            cur["binary"] = True
    for f in files:
        f["file"] = f["new_file"] if f["status"] != "deleted" and f["new_file"] else f["old_file"]
    return files


def git_diff(repo, base, head):
    base, head = check_rev(base), check_rev(head)
    text = git(repo, "diff", "--no-color", "--no-ext-diff", "-M", "-U3", base, head, "--")
    return parse_unified_diff(text)


def hunk_change_range(hunk):
    """(side, start, end) covering just the changed lines of a hunk.

    Head side, trimmed to the first and last changed line.  A removed line is
    represented on the head side by the position it used to occupy.  A hunk
    that only removes lines refers to the base side instead.
    """
    old, new = hunk["old_start"], hunk["new_start"]
    head_nos, base_nos, has_add = [], [], False
    for line in hunk["lines"]:
        tag = line[:1]
        if tag == "+":
            has_add = True
            head_nos.append(new)
            new += 1
        elif tag == "-":
            base_nos.append(old)
            head_nos.append(new)
            old += 1
        else:
            old += 1
            new += 1
    if not has_add and base_nos:
        return "base", min(base_nos), max(base_nos)
    lo = hunk["new_start"]
    hi = max(lo, hunk["new_start"] + hunk["new_len"] - 1)
    if not head_nos:
        return "head", lo, hi
    start = min(hi, max(lo, min(head_nos)))
    end = min(hi, max(lo, max(head_nos)))
    return "head", start, max(start, end)


def slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:48].strip("-") or "review"


def unique_review_id(repo, cfg, title):
    base = "%s-%s" % (datetime.date.today().isoformat(), slugify(title))
    rid, n = base, 2
    while os.path.exists(review_path(repo, cfg, rid)):
        rid = "%s-%d" % (base, n)
        n += 1
    return rid


def make_draft(repo, cfg, base, head, title, author=None):
    """Create and save a new review with one `change` stop per diff hunk."""
    if not isinstance(title, str) or not title.strip():
        raise TourError("title is required")
    title = title.strip()
    base_sha = rev_parse(repo, base)
    head_sha = rev_parse(repo, head)
    if base_sha == head_sha:
        raise TourError("base and head are the same commit")
    stops = []
    for f in git_diff(repo, base_sha, head_sha):
        if f["binary"]:
            continue
        for hunk in f["hunks"]:
            side, start, end = hunk_change_range(hunk)
            path = f["old_file"] if side == "base" else f["new_file"]
            if not path:
                continue
            stops.append({
                "id": "s%d" % (len(stops) + 1),
                "file": path,
                "start": start,
                "end": end,
                "side": side,
                "kind": "change",
                "note": "",
                "presenter_note": "",
                "flags": [],
            })
    review = {
        "schema": SCHEMA,
        "id": unique_review_id(repo, cfg, title),
        "title": title,
        "author": author or default_author(repo),
        "created": now_iso(),
        "base": base_sha,
        "head": head_sha,
        "stops": stops,
    }
    write_json_atomic(review_path(repo, cfg, review["id"]), review)
    return review


def git_show_file(repo, rev, path):
    """Lines of a file at a commit."""
    rev = check_rev(rev)
    rel = safe_rel_path(path)
    text = git(repo, "show", "--end-of-options", "%s:%s" % (rev, rel))
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def git_refs(repo):
    """Recent commits, branches and tags for the pickers, plus sensible defaults."""
    head = rev_parse(repo, "HEAD")
    commits = []
    for line in git(repo, "log", "-n", "60", "--format=%H%x00%h%x00%s%x00%an%x00%cI", "HEAD", "--").splitlines():
        parts = line.split("\x00")
        if len(parts) == 5:
            commits.append({"sha": parts[0], "short": parts[1], "subject": parts[2],
                            "author": parts[3], "date": parts[4]})

    def refs(prefix):
        out = []
        text = git(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)%00%(objectname)", prefix)
        for line in text.splitlines():
            name, _, sha = line.partition("\x00")
            if name:
                out.append({"name": name, "sha": sha})
        return out

    branches, tags = refs("refs/heads"), refs("refs/tags")
    return {
        "head": head,
        "default_head": head,
        "default_base": default_base(repo, head, [b["name"] for b in branches]),
        "commits": commits,
        "branches": branches,
        "tags": tags,
    }


def default_base(repo, head, branch_names):
    """Merge-base with the main branch; the parent commit when head is the main branch."""
    for name in ("main", "master", "origin/main", "origin/master", "develop"):
        if name not in branch_names and not name.startswith("origin/"):
            continue
        try:
            mb = git(repo, "merge-base", name, head).strip()
        except TourError:
            continue
        if mb and mb != head:
            return mb
        break
    try:
        return rev_parse(repo, "HEAD~1")
    except TourError:
        return head


# ---------------------------------------------------------------------------
# Symbol index (v3): sqlite at <tours_dir>/index.db, built from the working tree
# ---------------------------------------------------------------------------

CTAGS = "ctags"
C_EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".inl")
C_TU_EXTS = (".c", ".cc", ".cpp", ".cxx")
JS_EXTS = (".js", ".mjs", ".cjs")
PREPROCESS_TIMEOUT = 60
MARKER_RE = re.compile(r'^# (\d+) "([^"]*)"((?: \d+)*)')

INDEX_SCHEMA_VERSION = "2"  # bump when the tables change: older index files then rebuild themselves
INDEX_SCHEMA = """
CREATE TABLE symbols (
    name TEXT NOT NULL, kind TEXT NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL,
    target TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', static INTEGER NOT NULL DEFAULT 0,
    signature TEXT NOT NULL DEFAULT ''
);
CREATE INDEX symbols_name ON symbols(name);
CREATE TABLE binary_symbols (target TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL);
CREATE INDEX binary_symbols_name ON binary_symbols(name);
CREATE TABLE compiled_files (target TEXT NOT NULL, file TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def index_path(repo, cfg):
    return os.path.join(tours_dir(repo, cfg), "index.db")


def ensure_tours_gitignore(repo, cfg):
    """The index is derived data; keep it out of git without touching the repo's own .gitignore."""
    path = os.path.join(tours_dir(repo, cfg), ".gitignore")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("index.db\nindex.db.tmp\n.tmp-*\n")


def repo_files(repo):
    """Tracked files, repo-relative, as git sees them."""
    out = git(repo, "ls-files", "-z", "--cached", "--exclude-standard")
    return [f for f in out.split("\0") if f]


def glob_to_re(pattern):
    """'src/ctl/**' style globs: ** crosses directories, * and ? do not."""
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                out += "/?"
                i += 1
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile("^" + out + "$")


def glob_match(path, pattern):
    return bool(glob_to_re(pattern).match(path))


def targets_of(cfg):
    """Configured targets, or one implicit target covering the whole repo."""
    targets = cfg.get("targets")
    if not isinstance(targets, dict) or not targets:
        return {cfg["name"] or "default": {"sources": ["**"]}}
    out = {}
    for name, t in targets.items():
        if not isinstance(t, dict):
            raise TourError("tour.json: target %r must be an object" % name, 500)
        t = dict(t)
        t.setdefault("sources", ["**"])
        out[name] = t
    return out


def file_targets(cfg, path):
    """Names of the targets whose sources include this path."""
    return [name for name, t in targets_of(cfg).items() if any(glob_match(path, p) for p in t["sources"])]


def index_python(repo, rel, target):
    """Classes, functions (sync and async), methods and top-level assignments, via the stdlib ast."""
    with open(os.path.join(repo, rel), "rb") as f:
        source = f.read()
    tree = ast.parse(source, rel)
    rows = []

    def walk(body, scope):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if scope and scope_kinds.get(scope) == "class" else "function"
                rows.append((node.name, kind, rel, node.lineno, target, scope, 0, "(%s)" % ", ".join(a.arg for a in node.args.args)))
                inner = (scope + "." if scope else "") + node.name
                scope_kinds[inner] = "function"
                walk(node.body, inner)
            elif isinstance(node, ast.ClassDef):
                rows.append((node.name, "class", rel, node.lineno, target, scope, 0, ""))
                inner = (scope + "." if scope else "") + node.name
                scope_kinds[inner] = "class"
                walk(node.body, inner)
            elif not scope and isinstance(node, (ast.Assign, ast.AnnAssign)):
                names = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in names:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            rows.append((n.id, "variable", rel, node.lineno, target, "", 0, ""))

    scope_kinds = {}
    walk(tree.body, "")
    return rows


EXT_DEFINE_RE = re.compile(r"""Ext\.define\s*\(\s*['"]([\w.]+)['"]\s*,\s*\{""")
EXT_PROP_RE = re.compile(r"""\b(extend|alias|xtype|requires)\s*:\s*(\[[^\]]*\]|['"][^'"]*['"])""")


def _strings_in(expr):
    return re.findall(r"""['"]([^'"]+)['"]""", expr)


def index_sencha(repo, rel, target):
    """Ext.define classes with their extend parent, aliases / xtypes and requires."""
    with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as f:
        text = f.read()
    rows = []
    defines = list(EXT_DEFINE_RE.finditer(text))
    for i, m in enumerate(defines):
        cls = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        end = defines[i + 1].start() if i + 1 < len(defines) else len(text)
        body = text[m.end():end]
        rows.append((cls, "class", rel, line, target, "Ext.define", 0, ""))
        # Only properties at the top level of the config object define the class;
        # an `xtype:` nested inside `items: [...]` is a use, not a definition.
        depth = 1
        pos = 0
        for pm in EXT_PROP_RE.finditer(body):
            depth += body.count("{", pos, pm.start()) - body.count("}", pos, pm.start())
            pos = pm.start()
            if depth != 1:
                continue
            prop, value = pm.group(1), pm.group(2)
            values = _strings_in(value)
            if prop == "extend" and values:
                rows.append((cls, "extends", rel, line, target, values[0], 0, ""))
            elif prop == "alias":
                for a in values:
                    rows.append((a, "alias", rel, line, target, cls, 0, ""))
                    if a.startswith("widget."):
                        rows.append((a[len("widget."):], "xtype", rel, line, target, cls, 0, ""))
            elif prop == "xtype":
                for x in values:
                    rows.append((x, "xtype", rel, line, target, cls, 0, ""))
                    rows.append(("widget." + x, "alias", rel, line, target, cls, 0, ""))
            elif prop == "requires":
                for r in values:
                    rows.append((r, "requires", rel, line, target, cls, 0, ""))
    return rows


def ctags_version():
    out = run([CTAGS, "--version"], status=500)
    first = out.splitlines()[0] if out else ""
    if "Universal" not in first:
        raise TourError("need Universal Ctags for JSON output, found: %s" % (first or "unknown"), 500)
    return first


def run_ctags(repo, files, target, languages, extra=()):
    """Tag a list of repo-relative files.  Returns symbol rows."""
    if not files:
        return []
    argv = [CTAGS, "--output-format=json", "--fields=+nSKf", "--kinds-c=+p", "--languages=" + languages,
            "-L", "-"] + list(extra)
    out = run(argv, cwd=repo, input_text="\n".join(files) + "\n", status=500, timeout=600)
    return parse_ctags_json(out, target)


def parse_ctags_json(text, target):
    rows = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            tag = json.loads(line)
        except ValueError:
            continue
        if tag.get("_type") != "tag" or not tag.get("name") or not tag.get("line"):
            continue
        scope = ("%s:%s" % (tag["scopeKind"], tag["scope"])) if tag.get("scope") else ""
        rows.append((tag["name"], tag.get("kind", ""), tag["path"], int(tag["line"]), target, scope,
                     1 if tag.get("file") else 0, tag.get("signature", "") or ""))
    return rows


def read_nm(repo, binary_rel):
    """Defined symbols of a binary: (name, type letter)."""
    out = run(["nm", "--defined-only", repo_path(repo, binary_rel)], status=500, timeout=120)
    syms = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            syms.append((parts[-1], parts[-2]))
        elif len(parts) == 2:
            syms.append((parts[1], parts[0]))
    return syms


def _strip_compile_args(argv, tu):
    """Keep the flags that affect preprocessing; drop output/dependency options and the source file."""
    keep = []
    skip_next = False
    tu_base = os.path.basename(tu)
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in ("-c", "-S", "-E", "-MD", "-MMD", "-MP", "-M", "-MM", "-pipe"):
            continue
        if a in ("-o", "-MF", "-MT", "-MQ"):
            skip_next = True
            continue
        if a.startswith("-o") and len(a) > 2 and not a.startswith("-O"):
            continue
        if os.path.basename(a) == tu_base and a.endswith(C_TU_EXTS):
            continue
        keep.append(a)
    return keep


def compiled_files(repo, target):
    """Repo-relative translation units listed in the target's compile database, if it has one."""
    cc_rel = target.get("compile_commands")
    if not cc_rel:
        return None
    try:
        with open(repo_path(repo, cc_rel), encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError, TourError):
        return None
    out = set()
    root = os.path.realpath(repo)
    for e in entries:
        if not isinstance(e, dict) or not e.get("file"):
            continue
        directory = e.get("directory") or repo
        if not os.path.isdir(directory):
            directory = repo
        rel = rel_in_repo(e["file"], directory, root)
        if rel:
            out.add(rel)
    return out


def compile_flags(repo, target, tu, cc_cache=None):
    """(compiler argv0, flags, cwd) for one translation unit.

    compile_commands.json is preferred; cflags from tour.json is the fallback.
    """
    cc_rel = target.get("compile_commands")
    if cc_rel:
        cc_abs = repo_path(repo, cc_rel)
        entries = cc_cache.get(cc_abs) if cc_cache is not None else None
        if entries is None:
            try:
                with open(cc_abs, encoding="utf-8") as f:
                    entries = json.load(f)
            except (OSError, ValueError) as e:
                raise TourError("%s: %s" % (cc_rel, e), 500)
            if cc_cache is not None:
                cc_cache[cc_abs] = entries  # a plain dict assignment is atomic enough for a worker pool
        want = os.path.realpath(os.path.join(repo, tu))
        for e in entries:
            if not isinstance(e, dict) or not e.get("file"):
                continue
            directory = e.get("directory") or repo
            if not os.path.isdir(directory):
                directory = repo  # database generated elsewhere: relative flags resolve from the repo root
            if os.path.realpath(os.path.join(directory, e["file"])) != want:
                continue
            argv = e.get("arguments") or shlex.split(e.get("command", ""))
            if not argv:
                continue
            return argv[0], _strip_compile_args(argv[1:], tu), directory
    return target.get("compiler", "gcc"), list(target.get("cflags") or []), repo


def preprocess(repo, target, tu, cc_cache=None):
    """Run the compiler's preprocessor on one translation unit with the target's flags."""
    compiler, flags, cwd = compile_flags(repo, target, tu, cc_cache)
    src = os.path.join(repo, tu) if os.path.realpath(cwd) != os.path.realpath(repo) else tu
    argv = [compiler, "-E", "-dD"] + flags + [src]
    try:
        text = run(argv, cwd=cwd, status=500, timeout=PREPROCESS_TIMEOUT)
    except TourError as e:
        if compiler not in ("gcc", "clang") and "cannot run" in str(e):
            text = run(["gcc", "-E", "-dD"] + flags + [src], cwd=cwd, status=500, timeout=PREPROCESS_TIMEOUT)
        else:
            raise
    return text, cwd


def parse_line_markers(text):
    """Map each line of preprocessed output back to its origin.

    Returns one entry per output line: (file, line, is_system) for source lines,
    None for the `# <line> "<file>" <flags>` marker lines themselves.
    """
    mapping = []
    cur_file, cur_line, cur_sys = None, 0, False
    for raw in text.split("\n"):
        m = MARKER_RE.match(raw)
        if m:
            cur_line = int(m.group(1))
            cur_file = m.group(2)
            flags = set(m.group(3).split())
            cur_sys = "3" in flags or cur_file.startswith("<")
            mapping.append(None)
            continue
        mapping.append((cur_file, cur_line, cur_sys))
        cur_line += 1
    return mapping


def rel_in_repo(path, cwd, repo):
    """Repo-relative form of a path the preprocessor printed, or None if it is outside the repo."""
    if not path:
        return None
    root = os.path.realpath(repo)
    abs_path = os.path.realpath(os.path.join(cwd, path))
    if abs_path == root or not abs_path.startswith(root + os.sep):
        return None
    return os.path.relpath(abs_path, root).replace(os.sep, "/")


def index_macros(repo, target, target_name, tu, basic_keys, cc_cache=None):
    """Symbols that only exist after preprocessing (e.g. DEFINE_HANDLER(foo) -> foo_handler).

    Returns (rows, seen): `seen` maps each (file, line) where macro-generated
    symbols were found to the set of names the preprocessed pass saw there.
    A basic-pass tag at such a line whose name is not in that set was a macro
    invocation misread by ctags as a definition (module_param(...) becomes a
    "prototype" named module_param, DEFINE_HANDLER(x) a "function").
    """
    text, cwd = preprocess(repo, target, tu, cc_cache)
    mapping = parse_line_markers(text)
    fd, tmp = tempfile.mkstemp(prefix="tour-pp-", suffix=".c")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        out = run([CTAGS, "--output-format=json", "--fields=+nSKf", "--kinds-c=+p-d", "--language-force=C", tmp],
                  status=500, timeout=120)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    rows = []
    seen_at = {}      # (file, line) -> names the preprocessed pass saw there
    generated_at = set()
    source_lines = {}
    rel_cache = {}    # a kernel unit yields tens of thousands of header tags; resolve each file once
    for name, kind, _path, line, _t, scope, static, sig in parse_ctags_json(out, target_name):
        idx = line - 1
        if idx < 0 or idx >= len(mapping) or mapping[idx] is None:
            continue
        src_file, src_line, is_sys = mapping[idx]
        if is_sys:
            continue
        if src_file not in rel_cache:
            rel_cache[src_file] = rel_in_repo(src_file, cwd, repo)
        rel = rel_cache[src_file]
        if not rel:
            continue
        seen_at.setdefault((rel, src_line), set()).add(name)
        if (rel, src_line, name) in basic_keys:
            continue  # ctags already saw this one in the source itself
        if _line_has_word(repo, rel, src_line, name, source_lines):
            # Spelled out in the source, so not macro-generated: ctags just missed it
            # (typically because a macro invocation above confused its parser).
            rows.append((name, kind, rel, src_line, target_name, scope, static, sig))
            continue
        rows.append((name, "macro-generated", rel, src_line, target_name, kind, static, sig))
        generated_at.add((rel, src_line))
    return rows, {key: seen_at[key] for key in generated_at}


def _line_has_word(repo, rel, line, word, cache):
    lines = cache.get(rel)
    if lines is None:
        try:
            with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            lines = []
        cache[rel] = lines
    if line < 1 or line > len(lines):
        return False
    return re.search(r"\b%s\b" % re.escape(word), lines[line - 1]) is not None


def index_stale(repo, cfg):
    """True when the index is missing or any tracked file (or tour.json) is newer than it."""
    path = index_path(repo, cfg)
    if not os.path.exists(path):
        return True
    try:
        db = sqlite3.connect(path)
        schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        row = db.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        count = db.execute("SELECT value FROM meta WHERE key='file_count'").fetchone()
        db.close()
    except sqlite3.Error:
        return True
    if not row or not schema or schema[0] != INDEX_SCHEMA_VERSION:
        return True  # missing, or built by an older Tour
    built_at = float(row[0])
    files = repo_files(repo)
    if count and int(count[0]) != len(files):
        return True
    for rel in files + ["tour.json"]:
        try:
            if os.stat(os.path.join(repo, rel)).st_mtime > built_at:
                return True
        except OSError:
            continue
    return False


def build_index(repo, cfg, progress=None):
    """Rebuild the whole index into a temp file, then swap it in atomically."""
    started = time.time()
    ensure_tours_gitignore(repo, cfg)
    final = index_path(repo, cfg)
    tmp = final + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    files = repo_files(repo)
    targets = targets_of(cfg)
    errors = []
    rows = []

    def note(msg):
        if progress:
            progress(msg)

    def first_target(path):
        names = file_targets(cfg, path)
        return names[0] if names else ""

    # Python: stdlib ast, never ctags.
    for rel in files:
        if rel.endswith((".py", ".pyi")):
            try:
                rows += index_python(repo, rel, first_target(rel))
            except (SyntaxError, ValueError, OSError) as e:
                errors.append("%s: %s" % (rel, e))

    # Sencha: regex pass over every .js file.
    js_files = [f for f in files if f.endswith(JS_EXTS)]
    for rel in js_files:
        try:
            rows += index_sencha(repo, rel, first_target(rel))
        except OSError as e:
            errors.append("%s: %s" % (rel, e))

    have_ctags = True
    try:
        ctags_version()
    except TourError as e:
        have_ctags = False
        errors.append("ctags unavailable, C and JavaScript functions are not indexed: %s" % e)

    c_tus = {}
    if have_ctags:
        for name, t in targets.items():
            note("ctags: " + name)
            c_files = [f for f in files if f.endswith(C_EXTS) and any(glob_match(f, p) for p in t["sources"])]
            c_tus[name] = [f for f in c_files if f.endswith(C_TU_EXTS)]
            try:
                rows += run_ctags(repo, c_files, name, "C,C++")
            except TourError as e:
                errors.append("ctags %s: %s" % (name, e))
        by_target = {}
        for rel in js_files:
            by_target.setdefault(first_target(rel), []).append(rel)
        for name, group in by_target.items():
            try:
                rows += run_ctags(repo, group, name, "JavaScript")
            except TourError as e:
                errors.append("ctags javascript: %s" % e)

    db = sqlite3.connect(tmp)
    try:
        db.executescript(INDEX_SCHEMA)
        # nm: which names each binary actually contains.
        for name, t in targets.items():
            binary = t.get("binary")
            if not binary:
                continue
            try:
                if not os.path.exists(repo_path(repo, binary)):
                    errors.append("%s: binary %s not built, nm disambiguation off" % (name, binary))
                    continue
                db.executemany("INSERT INTO binary_symbols VALUES (?, ?, ?)",
                               [(name, s, ty) for s, ty in read_nm(repo, binary)])
            except TourError as e:
                errors.append("nm %s: %s" % (name, e))

        # Which translation units each target's compile database really builds
        # (a driver with twelve platform/*.c files compiles one of them).
        built_by_target = {}
        for name, t in targets.items():
            built = compiled_files(repo, t)
            if built:
                built_by_target[name] = built
                db.executemany("INSERT INTO compiled_files VALUES (?, ?)", [(name, f) for f in sorted(built)])

        # Macro-generated definitions: preprocess each translation unit.  This is the
        # slow part on real projects (kernel headers take seconds per unit), so units
        # run in parallel, and a target with a compile database only preprocesses the
        # units that database builds.
        if have_ctags:
            basic_keys = set((r[2], r[3], r[0]) for r in rows)
            cc_cache = {}
            seen = set()
            seen_at = {}
            jobs = []
            for name, t in targets.items():
                built = built_by_target.get(name)
                for tu in c_tus.get(name, []):
                    if built is not None and tu not in built:
                        continue
                    jobs.append((name, t, tu))
            done = [0]

            def work(job):
                name, t, tu = job
                try:
                    return job, index_macros(repo, t, name, tu, basic_keys, cc_cache), None
                except TourError as e:
                    return job, None, str(e).splitlines()[0][:300]

            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, os.cpu_count() or 1))) as pool:
                for (name, t, tu), result, err in pool.map(work, jobs):
                    done[0] += 1
                    note("preprocess: %s (%s) %d/%d" % (tu, name, done[0], len(jobs)))
                    if err:
                        errors.append("%s (%s): %s" % (tu, name, err))
                        continue
                    extra, at = result
                    for r in extra:
                        key = (r[0], r[2], r[3], r[4])
                        if key not in seen:
                            seen.add(key)
                            rows.append(r)
                    for key, names in at.items():
                        seen_at.setdefault(key, set()).update(names)
            if seen_at:
                # Macro invocations that ctags took for definitions.
                misparse_kinds = ("function", "prototype", "variable")
                rows = [r for r in rows if not (r[1] in misparse_kinds and (r[2], r[3]) in seen_at and r[0] not in seen_at[(r[2], r[3])])]

        db.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        meta = {
            "schema": INDEX_SCHEMA_VERSION,
            "built_at": repr(time.time()),
            "file_count": str(len(files)),
            "duration": "%.2f" % (time.time() - started),
            "errors": json.dumps(errors),
            "targets": json.dumps(sorted(targets)),
        }
        db.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
        db.commit()
    finally:
        db.close()
    os.replace(tmp, final)
    return index_status(repo, cfg)


def index_status(repo, cfg):
    path = index_path(repo, cfg)
    if not os.path.exists(path):
        return {"exists": False, "symbols": 0, "by_target": {}, "by_kind": {}, "errors": [], "built_at": None}
    db = sqlite3.connect(path)
    try:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        if meta.get("schema") != INDEX_SCHEMA_VERSION:
            raise sqlite3.OperationalError("schema")
        total = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        by_target = dict(db.execute("SELECT target, COUNT(*) FROM symbols GROUP BY target").fetchall())
        by_kind = dict(db.execute("SELECT kind, COUNT(*) FROM symbols GROUP BY kind").fetchall())
        binaries = dict(db.execute("SELECT target, COUNT(*) FROM binary_symbols GROUP BY target").fetchall())
        compiled = dict(db.execute("SELECT target, COUNT(*) FROM compiled_files GROUP BY target").fetchall())
    except sqlite3.Error:
        return {"exists": False, "symbols": 0, "by_target": {}, "by_kind": {}, "built_at": None,
                "errors": ["the symbol index was built by an older version of Tour: rebuild it"]}
    finally:
        db.close()
    built = float(meta.get("built_at", 0) or 0)
    return {
        "exists": True,
        "symbols": total,
        "by_target": by_target,
        "by_kind": by_kind,
        "binaries": binaries,
        "compiled": compiled,
        "errors": json.loads(meta.get("errors", "[]")),
        "built_at": datetime.datetime.fromtimestamp(built).replace(microsecond=0).isoformat() if built else None,
        "duration": float(meta.get("duration", 0) or 0),
        "file_count": int(meta.get("file_count", 0) or 0),
    }


def lookup_symbol(repo, cfg, name, target=None, file=None):
    """Definitions of a name, best first.

    Ranking: a static symbol in the file you clicked from wins; then the wanted
    target; then names the target's binary actually contains (nm); prototypes and
    `requires` entries sink.  `preferred` is set when the top hit clearly wins.
    """
    if not isinstance(name, str) or not name.strip():
        raise TourError("name is required")
    name = name.strip()
    path = index_path(repo, cfg)
    if not os.path.exists(path):
        raise TourError("symbol index not built yet", 404)
    if file:
        file = safe_rel_path(file)
    if not target and file:
        names = file_targets(cfg, file)
        target = names[0] if len(names) == 1 else None
    db = sqlite3.connect(path)
    try:
        rows = db.execute("SELECT name, kind, file, line, target, scope, static, signature FROM symbols WHERE name=?", (name,)).fetchall()
        in_bin = set(r[0] for r in db.execute("SELECT target FROM binary_symbols WHERE name=?", (name,)).fetchall())
        built = {}
        for t, f in db.execute("SELECT target, file FROM compiled_files").fetchall():
            built.setdefault(t, set()).add(f)
    except sqlite3.Error:
        raise TourError("symbol index needs rebuilding (built by an older version of Tour)", 404)
    finally:
        db.close()
    extends = {(f, line): scope for n, kind, f, line, t, scope, static, sig in rows if kind == "extends"}
    merged = {}
    for n, kind, f, line, t, scope, static, sig in rows:
        if kind == "extends":
            continue  # metadata about the class entry at the same spot
        score = 0
        if static and file:
            score += 100 if f == file else -50
        if target and t == target:
            score += 40
        if t in in_bin and (not target or t == target):
            score += 20
        if kind in ("prototype", "requires"):
            score -= 30
        if f == file:
            score += 5
        if t in built and f.endswith(C_TU_EXTS):
            # A .c file the target's compile database never builds is probably dead for it
            # (drivers with one platform/*.c per board).  The bonus for a built file only
            # counts for the target being asked about, so a target without a database
            # is not out-ranked merely for lacking one.
            if f not in built[t]:
                score -= 15
            elif t == target:
                score += 15
        key = (kind, f, line, scope)
        d = merged.get(key)
        if d is None:
            d = merged[key] = {"name": n, "kind": kind, "file": f, "line": line, "target": t, "targets": [],
                               "scope": scope, "static": bool(static), "signature": sig, "in_binary": False, "score": score}
            if kind == "class" and (f, line) in extends:
                d["extends"] = extends[(f, line)]
        # The same definition shared by several targets (common sources) is one entry.
        if t not in d["targets"]:
            d["targets"].append(t)
        if score > d["score"] or (score == d["score"] and t == target):
            d["score"], d["target"] = score, t
        d["in_binary"] = d["in_binary"] or t in in_bin
    defs = sorted(merged.values(), key=lambda d: (-d["score"], d["target"], d["file"], d["line"]))
    preferred = None
    if len(defs) == 1 or (len(defs) > 1 and defs[0]["score"] > defs[1]["score"]):
        preferred = 0
    return {"name": name, "target": target, "definitions": defs, "preferred": preferred}


EXPAND_CACHE = {}
EXPAND_CACHE_MAX = 8
EXPAND_LOCK = threading.Lock()


def preprocess_cached(repo, target, target_name, tu):
    """preprocess() with a small in-memory cache: a second `m` on the same file is instant."""
    try:
        mtime = os.stat(os.path.join(repo, tu)).st_mtime
    except OSError:
        mtime = 0
    key = (repo, target_name, tu, mtime)
    with EXPAND_LOCK:
        hit = EXPAND_CACHE.get(key)
    if hit is not None:
        return hit
    result = preprocess(repo, target, tu)
    with EXPAND_LOCK:
        if len(EXPAND_CACHE) >= EXPAND_CACHE_MAX:
            EXPAND_CACHE.pop(next(iter(EXPAND_CACHE)))
        EXPAND_CACHE[key] = result
    return result


def expand_range(repo, cfg, file, start, end, target=None):
    """Preprocessed text for a line range of a C file, for the "expand macros" toggle."""
    rel = safe_rel_path(file)
    if not rel.endswith(C_EXTS):
        raise TourError("only C files can be expanded")
    targets = targets_of(cfg)
    if not target:
        names = file_targets(cfg, rel)
        if not names:
            raise TourError("%s is not in any target's sources" % rel)
        target = names[0]
    if target not in targets:
        raise TourError("unknown target: %s" % target, 404)
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError):
        raise TourError("start and end must be integers")
    tu = rel
    if not rel.endswith(C_TU_EXTS):
        # A header has no flags of its own: preprocess the first translation unit that includes it.
        for cand in repo_files(repo):
            if cand.endswith(C_TU_EXTS) and any(glob_match(cand, p) for p in targets[target]["sources"]):
                try:
                    with open(os.path.join(repo, cand), encoding="utf-8", errors="replace") as f:
                        if os.path.basename(rel) in f.read():
                            tu = cand
                            break
                except OSError:
                    continue
    text, cwd = preprocess_cached(repo, targets[target], target, tu)
    mapping = parse_line_markers(text)
    lines = text.split("\n")
    out = []
    rel_cache = {}
    for i, m in enumerate(mapping):
        if m is None or m[2] or not (start <= m[1] <= end):
            continue
        if m[0] not in rel_cache:
            rel_cache[m[0]] = rel_in_repo(m[0], cwd, repo)
        if rel_cache[m[0]] == rel:
            out.append({"line": m[1], "text": lines[i]})
    return {"file": rel, "target": target, "start": start, "end": end, "lines": out}


class IndexWorker:
    """Builds the index in a background thread; at most one build at a time."""

    def __init__(self, repo):
        self.repo = repo
        self.lock = threading.Lock()
        self.thread = None
        self.progress = ""
        self.error = None

    def building(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg, force=True):
        with self.lock:
            if self.building():
                return False
            if not force and not index_stale(self.repo, cfg):
                return False
            self.error = None
            self.progress = "starting"
            self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self.thread.start()
            return True

    def _run(self, cfg):
        def progress(msg):
            self.progress = msg
        try:
            build_index(self.repo, cfg, progress)
        except Exception as e:  # noqa: BLE001 - surfaced through /api/index
            self.error = str(e)
        self.progress = ""

    def status(self, cfg):
        st = index_status(self.repo, cfg)
        st["building"] = self.building()
        st["progress"] = self.progress if self.building() else ""
        if self.error:
            st["errors"] = [self.error] + st.get("errors", [])
        st["targets"] = sorted(targets_of(cfg))
        return st


# ---------------------------------------------------------------------------
# Presenter state (v2).  Lives in memory: it only matters while a talk is on.
# ---------------------------------------------------------------------------


class PresenterState:
    """Where the presenter is.  The projector tab polls this and follows."""

    def __init__(self):
        self.lock = threading.Lock()
        self.review_id = None
        self.stop_index = 0
        self.mode = "stop"
        self.peek = None
        self.updated = 0.0      # last presenter PUT
        self.last_follow = 0.0  # last projector poll
        self.review_version = 0  # bumped on every review save or flag, so open tabs refetch

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        return {
            "review_id": self.review_id,
            "stop_index": self.stop_index,
            "mode": self.mode,
            "peek": self.peek,
            "age": round(now - self.updated, 3) if self.updated else None,
            "projector_age": round(now - self.last_follow, 3) if self.last_follow else None,
            "review_version": self.review_version,
        }

    def bump(self):
        with self.lock:
            self.review_version += 1

    def get(self, follow=False):
        with self.lock:
            now = time.time()
            if follow:
                self.last_follow = now
            return self.snapshot(now)

    def put(self, body):
        if not isinstance(body, dict):
            raise TourError("state must be an object")
        review_id = check_review_id(body.get("review_id"))
        stop_index = body.get("stop_index", 0)
        if not isinstance(stop_index, int) or stop_index < 0:
            raise TourError("stop_index must be a non-negative integer")
        mode = body.get("mode", "stop")
        if mode not in VIEW_MODES:
            raise TourError("mode must be one of %s" % ", ".join(VIEW_MODES))
        peek = body.get("peek")
        if peek is not None:
            if not isinstance(peek, dict) or not isinstance(peek.get("line"), int):
                raise TourError("peek must be {file, line}")
            peek = {"file": safe_rel_path(peek.get("file")), "line": peek["line"], "name": str(peek.get("name") or "")}
        with self.lock:
            self.review_id, self.stop_index, self.mode, self.peek = review_id, stop_index, mode, peek
            self.updated = time.time()
            return self.snapshot(self.updated)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

ROUTES = [
    ("GET", re.compile(r"^/api/config$"), "api_config"),
    ("GET", re.compile(r"^/api/reviews$"), "api_reviews"),
    ("GET", re.compile(r"^/api/review/([^/]+)$"), "api_review_get"),
    ("PUT", re.compile(r"^/api/review/([^/]+)$"), "api_review_put"),
    ("POST", re.compile(r"^/api/review/([^/]+)/flag$"), "api_review_flag"),
    ("POST", re.compile(r"^/api/draft$"), "api_draft"),
    ("GET", re.compile(r"^/api/diff$"), "api_diff"),
    ("GET", re.compile(r"^/api/file$"), "api_file"),
    ("GET", re.compile(r"^/api/refs$"), "api_refs"),
    ("GET", re.compile(r"^/api/state$"), "api_state_get"),
    ("PUT", re.compile(r"^/api/state$"), "api_state_put"),
    ("GET", re.compile(r"^/api/symbol$"), "api_symbol"),
    ("GET", re.compile(r"^/api/expand$"), "api_expand"),
    ("GET", re.compile(r"^/api/index$"), "api_index"),
    ("POST", re.compile(r"^/api/index/rebuild$"), "api_index_rebuild"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Tour/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    @property
    def repo(self):
        return self.server.repo

    @property
    def cfg(self):
        return load_config(self.server.repo)

    def log_message(self, fmt, *args):
        if self.server.verbose:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def send_bytes(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise TourError("bad Content-Length")
        if n > MAX_BODY:
            raise TourError("request body too large", 413)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise TourError("invalid JSON body: %s" % e)

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        method = "GET" if self.command == "HEAD" else self.command
        try:
            if url.path.startswith("/api/"):
                for m, rx, name in ROUTES:
                    match = rx.match(url.path)
                    if match and m == method:
                        self.send_json(getattr(self, name)(match, query))
                        return
                raise TourError("not found: %s %s" % (method, url.path), 404)
            if method != "GET":
                raise TourError("not found", 404)
            self.send_index()
        except TourError as e:
            self.send_json({"error": str(e)}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 - report anything else as a 500
            traceback.print_exc()
            self.send_json({"error": "internal error: %s" % e}, 500)

    def send_index(self):
        try:
            with open(INDEX_HTML, "rb") as f:
                body = f.read()
        except OSError:
            raise TourError("index.html is missing next to tour.py", 500)
        self.send_bytes(body, "text/html; charset=utf-8")

    # -- API ---------------------------------------------------------------

    def api_config(self, m, q):
        return self.cfg

    def api_reviews(self, m, q):
        return list_reviews(self.repo, self.cfg)

    def api_review_get(self, m, q):
        return load_review(self.repo, self.cfg, unquote(m.group(1)))

    def api_review_put(self, m, q):
        review = save_review(self.repo, self.cfg, unquote(m.group(1)), self.read_json())
        self.server.state.bump()
        return review

    def api_review_flag(self, m, q):
        body = self.read_json()
        by = body.get("by") or default_author(self.repo)
        review = add_flag(self.repo, self.cfg, unquote(m.group(1)), body.get("stop_id"), body.get("text"), by)
        self.server.state.bump()
        return review

    def api_draft(self, m, q):
        body = self.read_json()
        return make_draft(self.repo, self.cfg, body.get("base"), body.get("head"),
                          body.get("title"), body.get("author"))

    def api_diff(self, m, q):
        return git_diff(self.repo, q.get("base"), q.get("head"))

    def api_file(self, m, q):
        return {"path": q.get("path"), "rev": q.get("rev"),
                "lines": git_show_file(self.repo, q.get("rev"), q.get("path"))}

    def api_refs(self, m, q):
        return git_refs(self.repo)

    def api_state_get(self, m, q):
        return self.server.state.get(follow=q.get("follow") in ("1", "true"))

    def api_state_put(self, m, q):
        return self.server.state.put(self.read_json())

    def api_symbol(self, m, q):
        return lookup_symbol(self.repo, self.cfg, q.get("name"), q.get("target") or None, q.get("file") or None)

    def api_expand(self, m, q):
        return expand_range(self.repo, self.cfg, q.get("file"), q.get("start"), q.get("end"), q.get("target") or None)

    def api_index(self, m, q):
        return self.server.index.status(self.cfg)

    def api_index_rebuild(self, m, q):
        self.read_json()
        self.server.index.start(self.cfg, force=True)
        return self.server.index.status(self.cfg)


class TourServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, repo, verbose=False):
        ThreadingHTTPServer.__init__(self, address, Handler)
        self.repo = repo
        self.verbose = verbose
        self.state = PresenterState()
        self.index = IndexWorker(repo)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tour: code review walkthroughs from a git diff.")
    ap.add_argument("--repo", default=os.getcwd(), help="path inside the git repo to review (default: cwd)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 to reach it from another machine)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    ap.add_argument("--no-index", action="store_true", help="do not check or rebuild the symbol index at start")
    args = ap.parse_args(argv)
    try:
        repo = find_repo_root(os.path.abspath(args.repo))
        cfg = load_config(repo)
        server = TourServer((args.host, args.port), repo, args.verbose)
    except (TourError, OSError) as e:
        sys.exit("tour: %s" % e)
    host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "") else args.host
    print("tour: %s  (%s)" % (cfg["name"], repo))
    print("tour: http://%s:%d/" % (host, server.server_address[1]))
    if not args.no_index:
        if server.index.start(cfg, force=False):
            print("tour: symbol index is stale, rebuilding in the background")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
