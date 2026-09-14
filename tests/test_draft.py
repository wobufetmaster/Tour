import json
import os
import unittest

from helpers import FixtureRepo, tour


class DraftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = FixtureRepo()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()

    def test_git_diff_of_fixture(self):
        files = tour.git_diff(self.fx.repo, self.fx.base, self.fx.head)
        self.assertEqual(sorted(f["file"] for f in files), ["src/a.py", "src/new.js", "src/old.c"])
        by = {f["file"]: f for f in files}
        self.assertEqual(by["src/old.c"]["status"], "deleted")
        self.assertEqual(by["src/new.js"]["status"], "added")
        self.assertEqual(by["src/a.py"]["status"], "modified")

    def test_draft_has_one_stop_per_hunk(self):
        review = tour.make_draft(self.fx.repo, self.fx.cfg, self.fx.base, self.fx.head, "My Review", author="me")
        self.assertEqual(review["schema"], 1)
        self.assertEqual(review["base"], self.fx.base)
        self.assertEqual(review["head"], self.fx.head)
        self.assertEqual(review["author"], "me")
        self.assertTrue(review["id"].endswith("-my-review"))
        hunks = sum(len(f["hunks"]) for f in tour.git_diff(self.fx.repo, self.fx.base, self.fx.head))
        self.assertEqual(len(review["stops"]), hunks)
        self.assertEqual([s["id"] for s in review["stops"]], ["s%d" % (i + 1) for i in range(hunks)])
        for s in review["stops"]:
            self.assertEqual(s["kind"], "change")
            self.assertEqual(s["note"], "")
            self.assertLessEqual(s["start"], s["end"])
        by_file = {}
        for s in review["stops"]:
            by_file.setdefault(s["file"], []).append(s)
        self.assertEqual(by_file["src/old.c"][0]["side"], "base")
        self.assertEqual((by_file["src/old.c"][0]["start"], by_file["src/old.c"][0]["end"]), (1, 3))
        self.assertEqual(by_file["src/new.js"][0]["side"], "head")
        self.assertEqual((by_file["src/new.js"][0]["start"], by_file["src/new.js"][0]["end"]), (1, 3))
        a = by_file["src/a.py"][0]
        self.assertEqual(a["side"], "head")
        self.assertEqual(a["start"], 4)  # `return 2`
        self.assertEqual(a["end"], 10)   # `pass`
        # Saved to disk, readable, sorted keys.
        path = os.path.join(self.fx.repo, ".tours", review["id"] + ".json")
        with open(path) as f:
            text = f.read()
        self.assertEqual(json.loads(text), review)
        self.assertLess(text.index('"author"'), text.index('"stops"'))

    def test_draft_ids_are_unique(self):
        r1 = tour.make_draft(self.fx.repo, self.fx.cfg, self.fx.base, "HEAD", "Dup", author="me")
        r2 = tour.make_draft(self.fx.repo, self.fx.cfg, self.fx.base, "HEAD", "Dup", author="me")
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertTrue(r2["id"].endswith("-dup-2"))

    def test_draft_rejects_bad_input(self):
        with self.assertRaises(tour.TourError):
            tour.make_draft(self.fx.repo, self.fx.cfg, self.fx.base, self.fx.head, "")
        with self.assertRaises(tour.TourError):
            tour.make_draft(self.fx.repo, self.fx.cfg, "nope-not-a-rev", self.fx.head, "x")
        with self.assertRaises(tour.TourError):
            tour.make_draft(self.fx.repo, self.fx.cfg, self.fx.head, self.fx.head, "same")
        with self.assertRaises(tour.TourError):
            tour.make_draft(self.fx.repo, self.fx.cfg, "--output=/tmp/x", self.fx.head, "opt")

    def test_show_file_and_refs(self):
        lines = tour.git_show_file(self.fx.repo, self.fx.base, "src/a.py")
        self.assertEqual(lines[3], "    return 1")
        self.assertEqual(len(lines), 6)
        refs = tour.git_refs(self.fx.repo)
        self.assertEqual(refs["head"], self.fx.head)
        self.assertEqual(refs["default_base"], self.fx.base)  # head is main, so the parent
        self.assertEqual([b["name"] for b in refs["branches"]], ["main"])
        self.assertEqual([c["sha"] for c in refs["commits"]], [self.fx.head, self.fx.base])
        with self.assertRaises(tour.TourError):
            tour.git_show_file(self.fx.repo, self.fx.base, "src/missing.py")


if __name__ == "__main__":
    unittest.main()
