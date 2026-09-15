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


POLYGLOT_CONFIG = {
    "name": "flight",
    "languages": ["c", "python", "js"],
    "targets": {
        "flightctl": {
            "binary": "build/flightctl",
            "sources": ["src/ctl/**", "src/common/**", "include/**"],
            "compile_commands": "build/ctl/compile_commands.json",
        },
        "flightd": {
            "binary": "build/flightd",
            "sources": ["src/daemon/**", "src/common/**", "include/**"],
            "cflags": ["-Iinclude", "-DDAEMON"],
        },
    },
}

POLYGLOT_FILES = {
    "include/handlers.h": """\
#ifndef HANDLERS_H
#define HANDLERS_H

#define VERSION 3
#define DEFINE_HANDLER(name) int name##_handler(int x) { return handle(x) + 1; }

int handle(int x);
int common_util(int x);

#endif
""",
    "src/common/util.c": """\
#include "handlers.h"

static int helper(int x)
{
    return x * 2;
}

int common_util(int x)
{
    return helper(x);
}
""",
    "src/ctl/ctl.c": """\
#include "handlers.h"

/* ctl's private helper; the daemon has its own. */
static int helper(int x)
{
    return x + 100;
}

int handle(int x)
{
    return helper(x);
}

DEFINE_HANDLER(status)

int main(void)
{
    return status_handler(common_util(1));
}
""",
    "src/ctl/only_ctl.c": """\
int ctl_only(void)
{
    return 8;
}
""",
    "src/daemon/daemon.c": """\
#include "handlers.h"

#ifdef DAEMON
int handle(int x)
{
    return x - 1;
}
#endif

DEFINE_HANDLER(reload)

int main(void)
{
    return reload_handler(common_util(2));
}
""",
    "src/daemon/only_daemon.c": """\
int daemon_only(void)
{
    return 7;
}
""",
    "tools/report.py": """\
import json

TOP = 1
_private = 2


class Report:
    def render(self):
        return json.dumps({"top": TOP})


def make_report(rows):
    return Report()


async def fetch(url):
    return url
""",
    "web/app/view/Grid.js": """\
Ext.define('App.view.Grid', {
    extend: 'Ext.grid.Panel',
    alias: 'widget.appgrid',
    requires: ['App.store.Rows', 'Ext.grid.column.Number'],

    initComponent: function () {
        this.callParent(arguments);
    }
});
""",
    "web/app/view/Main.js": """\
function plainHelper(n) {
    return n + 1;
}

Ext.define('App.view.Main', {
    extend: 'Ext.panel.Panel',
    xtype: 'appmain',
    items: [{ xtype: 'appgrid' }]
});
""",
    "README": "flight: two binaries, one common dir, plus python tools and a Sencha UI\n",
}


class PolyglotRepo:
    """A C project with two binaries (common + exclusive sources), Python tools and Sencha JS.

    Both binaries are compiled with gcc so `nm` disambiguation can be tested.
    flightctl gets a compile_commands.json; flightd uses cflags from tour.json.
    """

    def __init__(self):
        import json as _json
        self.dir = tempfile.mkdtemp(prefix="tour-poly-")
        self.repo = os.path.realpath(self.dir)
        git(self.repo, "init", "-q", "-b", "main")
        for rel, text in POLYGLOT_FILES.items():
            write(self.repo, rel, text)
        write(self.repo, "tour.json", _json.dumps(POLYGLOT_CONFIG, indent=2))
        write(self.repo, ".gitignore", "build/*/\nbuild/flightctl\nbuild/flightd\n")
        os.makedirs(os.path.join(self.repo, "build", "ctl"), exist_ok=True)
        ctl_sources = ["src/ctl/ctl.c", "src/ctl/only_ctl.c", "src/common/util.c"]
        d_sources = ["src/daemon/daemon.c", "src/daemon/only_daemon.c", "src/common/util.c"]
        subprocess.run(["gcc", "-Iinclude", "-o", "build/flightctl"] + ctl_sources, cwd=self.repo, check=True)
        subprocess.run(["gcc", "-Iinclude", "-DDAEMON", "-o", "build/flightd"] + d_sources, cwd=self.repo, check=True)
        cc = [{"directory": self.repo, "file": src, "arguments": ["gcc", "-Iinclude", "-DCTL", "-c", "-o", src + ".o", src]}
              for src in ctl_sources]
        write(self.repo, "build/ctl/compile_commands.json", _json.dumps(cc, indent=2))
        self.head = commit_all(self.repo, "flight")
        self.cfg = tour.load_config(self.repo)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
