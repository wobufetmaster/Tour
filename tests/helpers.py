"""Shared test helpers: import tour.py and build throwaway git repos."""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tour  # noqa: E402

GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), env=GIT_ENV, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode()


def write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


class FixtureRepo:
    """A temp git repo with two commits: `base` and `head`.

    base:  src/a.py (5 lines), src/old.c, README
    head:  a.py line 3 changed + 2 lines appended, old.c deleted, src/new.js added,
           README unchanged.
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="tour-test-")
        self.repo = os.path.realpath(self.dir)
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo, "src/a.py", "import os\n\ndef f():\n    return 1\n\nprint(f())\n")
        write(self.repo, "src/old.c", "int main(void) {\n    return 0;\n}\n")
        write(self.repo, "README", "hello\n")
        self.base = commit_all(self.repo, "base")
        write(self.repo, "src/a.py", "import os\n\ndef f():\n    return 2\n\nprint(f())\n\n\ndef g():\n    pass\n")
        os.unlink(os.path.join(self.repo, "src/old.c"))
        write(self.repo, "src/new.js", "Ext.define('App.view.Main', {\n  extend: 'Ext.panel.Panel'\n});\n")
        self.head = commit_all(self.repo, "head")
        self.cfg = tour.load_config(self.repo)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
