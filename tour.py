#!/usr/bin/env python3
"""Tour: author code reviews from a git diff and walk a room through them.

Python 3 standard library only.  This file is the whole server; the UI lives
in index.html next to it.

    python3 tour.py [--repo PATH] [--port 8765]
"""

import argparse
import datetime
import json
import os
import posixpath
import re
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
VIEW_MODES = ("stop", "full", "diff")


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


def run(argv, cwd=None, status=500):
    """Run argv (never a shell) and return stdout as text."""
    try:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise TourError("cannot run %s: %s" % (argv[0], e), 500)
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
# Presenter state (v2).  Lives in memory: it only matters while a talk is on.
# ---------------------------------------------------------------------------


class PresenterState:
    """Where the presenter is.  The projector tab polls this and follows."""

    def __init__(self):
        self.lock = threading.Lock()
        self.review_id = None
        self.stop_index = 0
        self.mode = "stop"
        self.updated = 0.0      # last presenter PUT
        self.last_follow = 0.0  # last projector poll
        self.review_version = 0  # bumped on every review save or flag, so open tabs refetch

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        return {
            "review_id": self.review_id,
            "stop_index": self.stop_index,
            "mode": self.mode,
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
        with self.lock:
            self.review_id, self.stop_index, self.mode = review_id, stop_index, mode
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


class TourServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, repo, verbose=False):
        ThreadingHTTPServer.__init__(self, address, Handler)
        self.repo = repo
        self.verbose = verbose
        self.state = PresenterState()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tour: code review walkthroughs from a git diff.")
    ap.add_argument("--repo", default=os.getcwd(), help="path inside the git repo to review (default: cwd)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 to reach it from another machine)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
