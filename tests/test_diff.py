import unittest

from helpers import tour

SAMPLE = """\
diff --git a/src/a.py b/src/a.py
index 1111111..2222222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,6 +1,10 @@
 import os

 def f():
-    return 1
+    return 2

 print(f())
+
+
+def g():
+    pass
diff --git a/src/old.c b/src/old.c
deleted file mode 100644
index 3333333..0000000
--- a/src/old.c
+++ /dev/null
@@ -1,3 +0,0 @@
-int main(void) {
-    return 0;
-}
diff --git a/src/new.js b/src/new.js
new file mode 100644
index 0000000..4444444
--- /dev/null
+++ b/src/new.js
@@ -0,0 +1,2 @@
+var x = 1;
+var y = 2;
\\ No newline at end of file
diff --git a/img.png b/img.png
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/img.png differ
diff --git a/old/name.c b/new/name.c
similarity index 90%
rename from old/name.c
rename to new/name.c
index 6666666..7777777 100644
--- a/old/name.c
+++ b/new/name.c
@@ -10 +10 @@ int foo(void)
-  return 1;
+  return 2;
"""


class ParseDiffTest(unittest.TestCase):
    def setUp(self):
        self.files = tour.parse_unified_diff(SAMPLE)

    def test_file_count_and_names(self):
        self.assertEqual([f["file"] for f in self.files],
                         ["src/a.py", "src/old.c", "src/new.js", "img.png", "new/name.c"])

    def test_statuses(self):
        self.assertEqual([f["status"] for f in self.files],
                         ["modified", "deleted", "added", "added", "renamed"])
        self.assertTrue(self.files[3]["binary"])
        self.assertEqual(self.files[3]["hunks"], [])
        self.assertEqual(self.files[4]["old_file"], "old/name.c")
        self.assertEqual(self.files[4]["new_file"], "new/name.c")

    def test_hunk_headers(self):
        h = self.files[0]["hunks"][0]
        self.assertEqual((h["old_start"], h["old_len"], h["new_start"], h["new_len"]), (1, 6, 1, 10))
        self.assertEqual(len(h["lines"]), 11)
        # Deleted file: '---' header of the *next* file must not leak into this hunk.
        d = self.files[1]["hunks"][0]
        self.assertEqual(d["lines"], ["-int main(void) {", "-    return 0;", "-}"])
        # Single-line hunks omit the length.
        r = self.files[4]["hunks"][0]
        self.assertEqual((r["old_start"], r["old_len"], r["new_start"], r["new_len"]), (10, 1, 10, 1))

    def test_no_newline_marker_is_dropped(self):
        h = self.files[2]["hunks"][0]
        self.assertEqual(h["lines"], ["+var x = 1;", "+var y = 2;"])

    def test_blank_context_line_keeps_leading_space(self):
        h = self.files[0]["hunks"][0]
        self.assertEqual(h["lines"][1], " ")

    def test_empty_input(self):
        self.assertEqual(tour.parse_unified_diff(""), [])


class HunkRangeTest(unittest.TestCase):
    def hunk(self, old_start, new_start, lines):
        old_len = sum(1 for l in lines if l[0] != "+")
        new_len = sum(1 for l in lines if l[0] != "-")
        return {"old_start": old_start, "old_len": old_len, "new_start": new_start, "new_len": new_len, "lines": lines}

    def test_trims_context(self):
        h = self.hunk(1, 1, [" a", " b", "-c", "+C", " d", " e"])
        self.assertEqual(tour.hunk_change_range(h), ("head", 3, 3))

    def test_addition_block(self):
        h = self.hunk(5, 5, [" a", "+x", "+y", " b"])
        self.assertEqual(tour.hunk_change_range(h), ("head", 6, 7))

    def test_pure_deletion_maps_to_base(self):
        h = self.hunk(1, 0, ["-a", "-b", "-c"])
        self.assertEqual(tour.hunk_change_range(h), ("base", 1, 3))

    def test_deletion_with_context_still_maps_to_base(self):
        h = self.hunk(10, 10, [" a", "-gone", " b"])
        self.assertEqual(tour.hunk_change_range(h), ("base", 11, 11))

    def test_mixed_hunk_deletion_points_at_following_head_line(self):
        h = self.hunk(10, 10, [" a", "+x", "-gone", " b"])
        self.assertEqual(tour.hunk_change_range(h), ("head", 11, 12))

    def test_trailing_deletion_is_clamped_to_hunk(self):
        # "gone" would sit at head line 13, past the hunk's last head line (12).
        h = self.hunk(10, 10, [" a", "+x", " b", "-gone"])
        self.assertEqual(tour.hunk_change_range(h), ("head", 11, 12))


if __name__ == "__main__":
    unittest.main()
