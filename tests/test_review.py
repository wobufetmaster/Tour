import json
import os
import shutil
import tempfile
import unittest

from helpers import tour

SAMPLE = {
    "schema": 1,
    "id": "2026-09-14-ipc-refactor",
    "title": "IPC refactor",
    "author": "sean",
    "created": "2026-09-14T10:32:00",
    "base": "a1b2c3d",
    "head": "d4e5f6a",
    "custom_top_level": {"kept": True},
    "stops": [
        {
            "id": "s1", "file": "src/common/ipc.c", "start": 120, "end": 148, "side": "head",
            "kind": "change", "note": "New ring buffer.", "presenter_note": "benchmarks",
            "flags": [{"at": "2026-09-16T14:02:00", "by": "sean", "text": "bounds check"}],
            "future_field": [1, 2, 3],
        }
    ],
}


class ReviewFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tour-review-")
        self.repo = os.path.realpath(self.dir)
        self.cfg = dict(tour.DEFAULT_CONFIG, tours_dir=".tours")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip_keeps_unknown_keys(self):
        saved = tour.save_review(self.repo, self.cfg, "2026-09-14-ipc-refactor", json.loads(json.dumps(SAMPLE)))
        loaded = tour.load_review(self.repo, self.cfg, "2026-09-14-ipc-refactor")
        self.assertEqual(loaded, SAMPLE)
        self.assertEqual(saved, SAMPLE)

    def test_file_format_is_indented_and_sorted(self):
        tour.save_review(self.repo, self.cfg, "r1", json.loads(json.dumps(SAMPLE)))
        with open(os.path.join(self.repo, ".tours", "r1.json")) as f:
            text = f.read()
        self.assertTrue(text.startswith('{\n  "author": "sean",\n  "base":'))
        self.assertTrue(text.endswith("}\n"))
        keys = [l.strip().split('"')[1] for l in text.splitlines()[1:] if l.startswith('  "')]
        self.assertEqual(keys, sorted(keys))
        self.assertFalse([n for n in os.listdir(os.path.join(self.repo, ".tours")) if n.startswith(".tmp")])

    def test_id_comes_from_the_url(self):
        r = tour.save_review(self.repo, self.cfg, "other-id", json.loads(json.dumps(SAMPLE)))
        self.assertEqual(r["id"], "other-id")
        self.assertEqual(tour.load_review(self.repo, self.cfg, "other-id")["title"], "IPC refactor")

    def test_list(self):
        tour.save_review(self.repo, self.cfg, "a", {"title": "A", "created": "2026-01-01T00:00:00", "stops": []})
        tour.save_review(self.repo, self.cfg, "b", {"title": "B", "created": "2026-02-01T00:00:00", "stops": [{"id": "s1", "file": "x", "start": 1, "end": 1}]})
        with open(os.path.join(self.repo, ".tours", "junk.json"), "w") as f:
            f.write("not json")
        lst = tour.list_reviews(self.repo, self.cfg)
        self.assertEqual([(r["id"], r["stops"]) for r in lst], [("b", 1), ("a", 0)])

    def test_validation(self):
        bad = [
            {"stops": "nope"},
            {"stops": [{"id": "s1", "file": "../x", "start": 1, "end": 1}]},
            {"stops": [{"id": "s1", "file": "x", "start": 0, "end": 1}]},
            {"stops": [{"id": "s1", "file": "x", "start": 5, "end": 4}]},
            {"stops": [{"id": "s1", "file": "x", "start": 1, "end": 1, "kind": "rant"}]},
            {"stops": [{"id": "s1", "file": "x", "start": 1, "end": 1}, {"id": "s1", "file": "y", "start": 1, "end": 1}]},
        ]
        for review in bad:
            with self.assertRaises(tour.TourError, msg=review):
                tour.save_review(self.repo, self.cfg, "v", review)
        for rid in ("", "..", ".hidden", "a/b", "a b", "x" * 200):
            with self.assertRaises(tour.TourError, msg=rid):
                tour.save_review(self.repo, self.cfg, rid, {"stops": []})

    def test_missing_review_is_404(self):
        with self.assertRaises(tour.TourError) as cm:
            tour.load_review(self.repo, self.cfg, "nope")
        self.assertEqual(cm.exception.status, 404)

    def test_flag_is_appended(self):
        tour.save_review(self.repo, self.cfg, "f", json.loads(json.dumps(SAMPLE)))
        r = tour.add_flag(self.repo, self.cfg, "f", "s1", "  follow up  ", "me")
        self.assertEqual(len(r["stops"][0]["flags"]), 2)
        self.assertEqual(r["stops"][0]["flags"][1]["text"], "follow up")
        self.assertEqual(r["stops"][0]["flags"][1]["by"], "me")
        self.assertEqual(r["stops"][0]["future_field"], [1, 2, 3])
        with self.assertRaises(tour.TourError):
            tour.add_flag(self.repo, self.cfg, "f", "s9", "x", "me")
        with self.assertRaises(tour.TourError):
            tour.add_flag(self.repo, self.cfg, "f", "s1", "   ", "me")

    def test_config_defaults_and_override(self):
        cfg = tour.load_config(self.repo)
        self.assertEqual(cfg["tours_dir"], ".tours")
        self.assertEqual(cfg["name"], os.path.basename(self.repo))
        with open(os.path.join(self.repo, "tour.json"), "w") as f:
            json.dump({"name": "flightctl", "tours_dir": "docs/tours", "targets": {"a": {"sources": ["src/**"]}}}, f)
        cfg = tour.load_config(self.repo)
        self.assertEqual(cfg["name"], "flightctl")
        self.assertEqual(cfg["tours_dir"], "docs/tours")
        self.assertEqual(cfg["languages"], ["c", "python", "js"])
        tour.save_review(self.repo, cfg, "r", {"stops": []})
        self.assertTrue(os.path.exists(os.path.join(self.repo, "docs", "tours", "r.json")))


if __name__ == "__main__":
    unittest.main()
