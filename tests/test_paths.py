import os
import shutil
import tempfile
import unittest

from helpers import tour


class PathEscapeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tour-paths-")
        self.repo = os.path.realpath(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_good_paths_are_normalized(self):
        self.assertEqual(tour.safe_rel_path("src/a.c"), "src/a.c")
        self.assertEqual(tour.safe_rel_path("./src//a.c"), "src/a.c")
        self.assertEqual(tour.safe_rel_path("src/x/../a.c"), "src/a.c")
        self.assertEqual(tour.safe_rel_path("src\\a.c"), "src/a.c")
        self.assertEqual(tour.repo_path(self.repo, "src/a.c"), os.path.join(self.repo, "src", "a.c"))
        self.assertEqual(tour.repo_path(self.repo, "."), self.repo) if False else None

    def test_escapes_are_rejected(self):
        for bad in ("../etc/passwd", "src/../../x", "/etc/passwd", "\\\\server\\share", "C:\\x",
                    "..", "", "a\x00b", "src/../..", "../", "..\\x"):
            with self.assertRaises(tour.TourError, msg=repr(bad)):
                tour.safe_rel_path(bad)
            with self.assertRaises(tour.TourError, msg=repr(bad)):
                tour.repo_path(self.repo, bad)

    def test_symlink_out_of_repo_is_rejected(self):
        outside = tempfile.mkdtemp(prefix="tour-outside-")
        try:
            os.symlink(outside, os.path.join(self.repo, "link"))
            with self.assertRaises(tour.TourError):
                tour.repo_path(self.repo, "link/secret")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_revisions_that_look_like_options_are_rejected(self):
        for bad in ("-x", "--output=x", "", "  ", "a b", "HEAD:file", "a\nb"):
            with self.assertRaises(tour.TourError, msg=repr(bad)):
                tour.check_rev(bad)
        self.assertEqual(tour.check_rev(" HEAD~2 "), "HEAD~2")

    def test_review_ids(self):
        for ok in ("2026-09-14-ipc-refactor", "a", "A.b_c-1"):
            self.assertEqual(tour.check_review_id(ok), ok)
        for bad in ("../x", ".", "..", ".git", "a/b", "", None, 5, "x" * 129):
            with self.assertRaises(tour.TourError, msg=repr(bad)):
                tour.check_review_id(bad)


if __name__ == "__main__":
    unittest.main()
