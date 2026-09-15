"""Symbol index (v3): C with several binaries, Python via ast, Sencha JS, macro mapping."""

import os
import shutil
import time
import unittest

from helpers import PolyglotRepo, tour

HAVE_CTAGS = shutil.which("ctags") is not None
HAVE_GCC = shutil.which("gcc") is not None and shutil.which("nm") is not None


class LineMarkerTest(unittest.TestCase):
    """Pure mapping of preprocessed output back to file:line."""

    def test_markers_map_following_lines(self):
        text = ('# 1 "src/a.c"\n'
                'int a;\n'
                '# 1 "include/h.h" 1\n'
                'int h1;\n'
                'int h2;\n'
                '# 2 "src/a.c" 2\n'
                'int b;\n'
                '# 1 "/usr/include/stdio.h" 1 3 4\n'
                'typedef int FILE;\n'
                '# 3 "src/a.c" 2\n'
                '\n'
                'int c;\n')
        m = tour.parse_line_markers(text)
        self.assertIsNone(m[0])
        self.assertEqual(m[1], ("src/a.c", 1, False))
        self.assertEqual(m[3], ("include/h.h", 1, False))
        self.assertEqual(m[4], ("include/h.h", 2, False))
        self.assertEqual(m[6], ("src/a.c", 2, False))
        self.assertEqual(m[8], ("/usr/include/stdio.h", 1, True))
        self.assertEqual(m[10], ("src/a.c", 3, False))
        self.assertEqual(m[11], ("src/a.c", 4, False))

    def test_builtin_pseudo_files_count_as_system(self):
        m = tour.parse_line_markers('# 0 "<built-in>"\n#define __STDC__ 1\n# 0 "<command-line>"\n')
        self.assertEqual(m[1], ("<built-in>", 0, True))

    def test_rel_in_repo(self):
        repo = os.path.realpath(os.getcwd())
        self.assertEqual(tour.rel_in_repo("src/a.c", repo, repo), "src/a.c")
        self.assertEqual(tour.rel_in_repo(os.path.join(repo, "x", "y.h"), "/", repo), "x/y.h")
        self.assertIsNone(tour.rel_in_repo("/usr/include/stdio.h", repo, repo))
        self.assertIsNone(tour.rel_in_repo("../outside.c", repo, repo))

    def test_compile_commands_with_missing_directory_falls_back_to_repo(self):
        import json
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="tour-cc-")
        try:
            os.makedirs(os.path.join(d, "src"))
            open(os.path.join(d, "src", "a.c"), "w").close()
            cc = os.path.join(d, "compile_commands.json")
            with open(cc, "w") as f:
                json.dump([{"directory": "/nonexistent/build/dir", "file": "src/a.c",
                            "command": "cc -Iinclude -DX=1 -c -o a.o src/a.c"}], f)
            compiler, flags, cwd = tour.compile_flags(d, {"compile_commands": "compile_commands.json"}, "src/a.c")
            self.assertEqual((compiler, flags, cwd), ("cc", ["-Iinclude", "-DX=1"], d))
            compiler, flags, cwd = tour.compile_flags(d, {"compile_commands": "compile_commands.json", "cflags": ["-DF"]}, "src/other.c")
            self.assertEqual((compiler, flags), ("gcc", ["-DF"]))  # not in the database: cflags fallback
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_strip_compile_args(self):
        argv = ["-Iinclude", "-DFOO=1", "-c", "-o", "build/a.o", "-MMD", "-MF", "a.d", "-O2", "-std=gnu11", "src/a.c"]
        self.assertEqual(tour._strip_compile_args(argv, "src/a.c"), ["-Iinclude", "-DFOO=1", "-O2", "-std=gnu11"])


class GlobTest(unittest.TestCase):
    def test_double_star_crosses_dirs_single_star_does_not(self):
        self.assertTrue(tour.glob_match("src/ctl/ctl.c", "src/ctl/**"))
        self.assertTrue(tour.glob_match("src/ctl/deep/er/x.c", "src/ctl/**"))
        self.assertFalse(tour.glob_match("src/daemon/d.c", "src/ctl/**"))
        self.assertTrue(tour.glob_match("src/a.c", "src/*.c"))
        self.assertFalse(tour.glob_match("src/sub/a.c", "src/*.c"))
        self.assertTrue(tour.glob_match("src/sub/a.c", "**/*.c"))
        self.assertTrue(tour.glob_match("a.c", "**/*.c"))
        self.assertTrue(tour.glob_match("anything/at/all", "**"))

    def test_targets_default_to_whole_repo(self):
        self.assertEqual(tour.targets_of({"name": "solo"}), {"solo": {"sources": ["**"]}})
        cfg = {"name": "x", "targets": {"a": {"sources": ["src/a/**"]}, "b": {"sources": ["src/b/**", "src/common/**"]}}}
        self.assertEqual(tour.file_targets(cfg, "src/common/u.c"), ["b"])
        self.assertEqual(tour.file_targets(cfg, "src/a/x.c"), ["a"])
        self.assertEqual(tour.file_targets(cfg, "tools/t.py"), [])


@unittest.skipUnless(HAVE_CTAGS and HAVE_GCC, "needs universal-ctags, gcc and nm")
class PolyglotIndexTest(unittest.TestCase):
    """One index over a C project with two binaries plus Python tools and Sencha JS."""

    @classmethod
    def setUpClass(cls):
        cls.fx = PolyglotRepo()
        cls.progress = []
        cls.status = tour.build_index(cls.fx.repo, cls.fx.cfg, cls.progress.append)

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()

    def look(self, name, **kw):
        return tour.lookup_symbol(self.fx.repo, self.fx.cfg, name, **kw)

    def test_build_status(self):
        st = self.status
        self.assertTrue(st["exists"])
        self.assertEqual(st["errors"], [])
        self.assertEqual(set(st["by_target"]), {"", "flightctl", "flightd"})
        self.assertGreater(st["binaries"]["flightctl"], 0)
        self.assertGreater(st["binaries"]["flightd"], 0)
        self.assertEqual(st["by_kind"]["macro-generated"], 3)  # status_handler, reload_handler, ctl_count
        self.assertTrue(os.path.exists(os.path.join(self.fx.repo, ".tours", ".gitignore")))
        # flightctl has a compile database: only the units it builds are preprocessed for it.
        pp = [m for m in self.progress if m.startswith("preprocess:")]
        self.assertTrue(any("src/ctl/ctl.c (flightctl)" in m for m in pp))
        self.assertFalse(any("alt_handle.c" in m for m in pp))
        # flightd has cflags only: every unit in its sources is preprocessed.
        self.assertTrue(any("src/daemon/only_daemon.c (flightd)" in m for m in pp))
        self.assertFalse(tour.index_stale(self.fx.repo, self.fx.cfg))

    def test_same_name_resolves_per_target(self):
        ctl = self.look("handle", target="flightctl")
        self.assertEqual(ctl["preferred"], 0)
        self.assertEqual((ctl["definitions"][0]["file"], ctl["definitions"][0]["kind"]), ("src/ctl/ctl.c", "function"))
        self.assertTrue(ctl["definitions"][0]["in_binary"])
        d = self.look("handle", target="flightd")
        self.assertEqual(d["definitions"][0]["file"], "src/daemon/daemon.c")
        # Target inferred from the file the click came from.
        self.assertEqual(self.look("handle", file="src/daemon/daemon.c")["definitions"][0]["file"], "src/daemon/daemon.c")
        # Without any context both real definitions tie: the UI must ask.
        both = self.look("handle")
        self.assertIsNone(both["preferred"])
        self.assertEqual([x["file"] for x in both["definitions"][:2]], ["src/ctl/ctl.c", "src/daemon/daemon.c"])
        # src/ctl/alt_handle.c matches flightctl's sources but its compile database never builds it,
        # so it sinks below the compiled definition.
        files = [x["file"] for x in both["definitions"]]
        self.assertLess(files.index("src/ctl/ctl.c"), files.index("src/ctl/alt_handle.c"))
        self.assertEqual(ctl["definitions"][0]["file"], "src/ctl/ctl.c")
        self.assertEqual(self.status["compiled"], {"flightctl": 3})  # flightd has cflags only

    def test_static_symbols_resolve_by_file(self):
        r = self.look("helper", file="src/ctl/ctl.c")
        self.assertEqual(r["preferred"], 0)
        self.assertEqual(r["definitions"][0]["file"], "src/ctl/ctl.c")
        self.assertTrue(r["definitions"][0]["static"])
        r = self.look("helper", file="src/daemon/daemon.c")
        self.assertEqual(r["definitions"][0]["file"], "src/common/util.c")
        # The common file's static helper is one entry shared by both targets.
        r = self.look("helper", file="src/common/util.c")
        self.assertEqual(r["preferred"], 0)
        self.assertEqual(sorted(r["definitions"][0]["targets"]), ["flightctl", "flightd"])

    def test_exclusive_files_belong_to_one_target(self):
        r = self.look("daemon_only")
        self.assertEqual(r["definitions"][0]["targets"], ["flightd"])
        self.assertTrue(r["definitions"][0]["in_binary"])
        r = self.look("ctl_only")
        self.assertEqual(r["definitions"][0]["targets"], ["flightctl"])
        common = self.look("common_util", file="src/ctl/ctl.c")
        self.assertEqual(common["definitions"][0]["file"], "src/common/util.c")
        self.assertEqual(sorted(common["definitions"][0]["targets"]), ["flightctl", "flightd"])

    def test_macro_generated_definitions_map_back_to_source(self):
        r = self.look("status_handler")
        self.assertEqual(r["preferred"], 0)
        d = r["definitions"][0]
        self.assertEqual((d["kind"], d["file"], d["line"], d["targets"]), ("macro-generated", "src/ctl/ctl.c", 14, ["flightctl"]))
        self.assertTrue(d["in_binary"])
        r = self.look("reload_handler")  # flightd has no compile_commands: cflags path
        d = r["definitions"][0]
        self.assertEqual((d["kind"], d["file"], d["line"], d["targets"]), ("macro-generated", "src/daemon/daemon.c", 10, ["flightd"]))
        # The macro invocation line is not also reported as a function called DEFINE_HANDLER.
        kinds = set(d["kind"] for d in self.look("DEFINE_HANDLER")["definitions"])
        self.assertEqual(kinds, {"macro"})
        # `DECLARE_STAT(ctl);` at file scope looks like a K&R prototype to ctags (as module_param does
        # in kernel modules); the preprocessed pass knows better.
        self.assertEqual(set(d["kind"] for d in self.look("DECLARE_STAT")["definitions"]), {"macro"})
        stat = self.look("ctl_count")["definitions"]
        self.assertEqual([(d["kind"], d["file"], d["line"], d["scope"]) for d in stat],
                         [("macro-generated", "src/ctl/ctl.c", 15, "variable")])
        # Nothing from system headers leaked in.
        self.assertEqual(self.look("printf")["definitions"], [])
        # ctags misses `main` after the macro invocation; the preprocessed pass recovers it as a plain function.
        mains = self.look("main")["definitions"]
        self.assertEqual(sorted((d["file"], d["kind"]) for d in mains),
                         [("src/ctl/ctl.c", "function"), ("src/daemon/daemon.c", "function")])

    def test_python_symbols_via_ast(self):
        self.assertEqual(self.look("Report")["definitions"][0]["kind"], "class")
        self.assertEqual(self.look("make_report")["definitions"][0]["line"], 12)
        self.assertEqual(self.look("fetch")["definitions"][0]["kind"], "function")  # async def
        self.assertEqual(self.look("TOP")["definitions"][0]["kind"], "variable")
        m = self.look("render")["definitions"][0]
        self.assertEqual((m["kind"], m["scope"]), ("method", "Report"))
        self.assertEqual(self.look("Report")["definitions"][0]["target"], "")  # not in any C target

    def test_sencha_classes_aliases_and_xtypes(self):
        grid = self.look("App.view.Grid")
        self.assertEqual(grid["preferred"], 0)
        self.assertEqual(grid["definitions"][0]["kind"], "class")
        self.assertEqual(grid["definitions"][0]["extends"], "Ext.grid.Panel")
        x = self.look("appgrid")["definitions"][0]
        self.assertEqual((x["kind"], x["scope"], x["file"]), ("xtype", "App.view.Grid", "web/app/view/Grid.js"))
        self.assertEqual(self.look("widget.appgrid")["definitions"][0]["kind"], "alias")
        # xtype declared directly, and the nested `items: [{ xtype: 'appgrid' }]` is a use, not a definition.
        main = self.look("appmain")["definitions"]
        self.assertEqual(len(main), 1)
        self.assertEqual(main[0]["scope"], "App.view.Main")
        self.assertEqual([d["scope"] for d in self.look("appgrid")["definitions"]], ["App.view.Grid"])
        self.assertEqual(self.look("plainHelper")["definitions"][0]["kind"], "function")
        req = self.look("App.store.Rows")["definitions"][0]
        self.assertEqual((req["kind"], req["scope"]), ("requires", "App.view.Grid"))

    def test_expand_macros(self):
        r = tour.expand_range(self.fx.repo, self.fx.cfg, "src/ctl/ctl.c", 14, 14)
        self.assertEqual(r["target"], "flightctl")
        self.assertEqual([l["text"] for l in r["lines"]], ["int status_handler(int x) { return handle(x) + 1; }"])
        r = tour.expand_range(self.fx.repo, self.fx.cfg, "src/daemon/daemon.c", 3, 10)
        texts = [l["text"] for l in r["lines"]]
        self.assertIn("int reload_handler(int x) { return handle(x) + 1; }", texts)
        self.assertIn("int handle(int x)", texts)  # -DDAEMON from cflags kept the #ifdef body
        with self.assertRaises(tour.TourError):
            tour.expand_range(self.fx.repo, self.fx.cfg, "tools/report.py", 1, 2)
        with self.assertRaises(tour.TourError):
            tour.expand_range(self.fx.repo, self.fx.cfg, "src/ctl/ctl.c", 1, 2, target="nope")

    def test_index_from_older_version_is_stale_not_fatal(self):
        import sqlite3
        path = tour.index_path(self.fx.repo, self.fx.cfg)
        backup = path + ".bak"
        os.replace(path, backup)
        try:
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE symbols (name TEXT, kind TEXT, file TEXT, line INTEGER, target TEXT, scope TEXT);"
                             "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            db.execute("INSERT INTO meta VALUES ('built_at', ?)", (repr(time.time() + 100),))
            db.execute("INSERT INTO meta VALUES ('file_count', ?)", (str(len(tour.repo_files(self.fx.repo))),))
            db.commit()
            db.close()
            self.assertTrue(tour.index_stale(self.fx.repo, self.fx.cfg))
            st = tour.index_status(self.fx.repo, self.fx.cfg)
            self.assertFalse(st["exists"])
            self.assertTrue(any("older version" in e for e in st["errors"]))
            with self.assertRaises(tour.TourError):
                self.look("handle")
        finally:
            os.replace(backup, path)

    def test_stale_after_edit_and_missing_ctags(self):
        path = os.path.join(self.fx.repo, "src/ctl/only_ctl.c")
        future = time.time() + 5
        os.utime(path, (future, future))
        try:
            self.assertTrue(tour.index_stale(self.fx.repo, self.fx.cfg))
        finally:
            os.utime(path, None)
        old = tour.CTAGS
        tour.CTAGS = "ctags-definitely-missing"
        try:
            st = tour.build_index(self.fx.repo, self.fx.cfg)
        finally:
            tour.CTAGS = old
        self.assertTrue(any("ctags" in e for e in st["errors"]))
        self.assertNotIn("function", {k for k, v in st["by_kind"].items() if k == "macro"})
        self.assertEqual(self.look("Report")["definitions"][0]["kind"], "class")  # python still indexed
        self.assertEqual(self.look("appgrid")["definitions"][0]["kind"], "xtype")  # sencha still indexed
        self.assertEqual(self.look("handle")["definitions"], [])
        tour.build_index(self.fx.repo, self.fx.cfg)  # restore for other tests


if __name__ == "__main__":
    unittest.main()
