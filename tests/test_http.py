"""End-to-end tests against a real server on a random port."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import FixtureRepo, tour


class ServerCase(unittest.TestCase):
    """Starts one server per test class on a random port."""

    @classmethod
    def setUpClass(cls):
        cls.fx = FixtureRepo()
        cls.server = tour.TourServer(("127.0.0.1", 0), cls.fx.repo)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.fx.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode()) if resp.headers.get("Content-Type", "").startswith("application/json") else resp.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


class HttpTest(ServerCase):
    def test_index_is_served_for_unknown_paths(self):
        req = urllib.request.Request("http://127.0.0.1:%d/anything/here" % self.port)
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn(b"<title>Tour</title>", resp.read())

    def test_config_and_refs(self):
        status, cfg = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["tours_dir"], ".tours")
        status, refs = self.call("GET", "/api/refs")
        self.assertEqual(status, 200)
        self.assertEqual(refs["head"], self.fx.head)

    def test_path_escape_is_rejected(self):
        for path in ("../etc/passwd", "/etc/passwd", "src/../../x"):
            status, body = self.call("GET", "/api/file?path=%s&rev=HEAD" % urllib.request.quote(path, safe=""))
            self.assertEqual(status, 400, path)
            self.assertIn("error", body)
        status, body = self.call("GET", "/api/file?path=src/a.py&rev=%s" % self.fx.base)
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"][3], "    return 1")
        status, body = self.call("GET", "/api/file?path=src/a.py&rev=--output=x")
        self.assertEqual(status, 400)

    def test_review_id_escape_is_rejected(self):
        status, body = self.call("PUT", "/api/review/..%2F..%2Fpwned", {"stops": []})
        self.assertEqual(status, 400)
        status, body = self.call("PUT", "/api/review/.hidden", {"stops": []})
        self.assertEqual(status, 400)
        status, body = self.call("GET", "/api/review/does-not-exist")
        self.assertEqual(status, 404)

    def test_draft_save_flag_round_trip(self):
        status, review = self.call("POST", "/api/draft", {"base": self.fx.base, "head": self.fx.head, "title": "HTTP draft"})
        self.assertEqual(status, 200, review)
        rid = review["id"]
        self.assertGreater(len(review["stops"]), 0)
        status, lst = self.call("GET", "/api/reviews")
        self.assertIn(rid, [r["id"] for r in lst])

        review["stops"][0]["note"] = "hello **world**"
        review["stops"][0]["kind"] = "question"
        review["x_unknown"] = {"round": "trip"}
        status, saved = self.call("PUT", "/api/review/" + rid, review)
        self.assertEqual(status, 200, saved)
        status, again = self.call("GET", "/api/review/" + rid)
        self.assertEqual(again["stops"][0]["note"], "hello **world**")
        self.assertEqual(again["x_unknown"], {"round": "trip"})

        status, flagged = self.call("POST", "/api/review/%s/flag" % rid, {"stop_id": "s1", "text": "look again", "by": "room"})
        self.assertEqual(status, 200, flagged)
        self.assertEqual(flagged["stops"][0]["flags"][-1]["text"], "look again")

        status, diff = self.call("GET", "/api/diff?base=%s&head=%s" % (self.fx.base, self.fx.head))
        self.assertEqual(status, 200)
        self.assertEqual(sorted(f["file"] for f in diff), ["src/a.py", "src/new.js", "src/old.c"])

    def test_bad_json_and_unknown_api(self):
        req = urllib.request.Request("http://127.0.0.1:%d/api/draft" % self.port, data=b"{not json", method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        status, body = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/api/config")
        self.assertEqual(status, 404)


class StateTest(ServerCase):
    """Presenter state (v2) lives in memory on the server."""

    def test_state_round_trip_and_ages(self):
        status, st = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(st["review_id"])
        self.assertIsNone(st["age"])
        status, st = self.call("PUT", "/api/state", {"review_id": "r1", "stop_index": 4, "mode": "full"})
        self.assertEqual(status, 200, st)
        self.assertEqual((st["review_id"], st["stop_index"], st["mode"]), ("r1", 4, "full"))
        self.assertIsNone(st["projector_age"])
        status, st = self.call("GET", "/api/state?follow=1")
        self.assertEqual(st["stop_index"], 4)
        self.assertGreaterEqual(st["age"], 0)
        status, st = self.call("PUT", "/api/state", {"review_id": "r1", "stop_index": 5})
        self.assertEqual(st["mode"], "stop")
        self.assertIsNotNone(st["projector_age"])
        self.assertLess(st["projector_age"], 5)

    def test_review_version_bumps_on_save_and_flag(self):
        status, st = self.call("GET", "/api/state")
        v0 = st["review_version"]
        status, review = self.call("POST", "/api/draft", {"base": self.fx.base, "head": self.fx.head, "title": "Version"})
        self.assertEqual(status, 200, review)
        status, _ = self.call("PUT", "/api/review/" + review["id"], review)
        self.assertEqual(status, 200)
        status, st = self.call("GET", "/api/state")
        self.assertEqual(st["review_version"], v0 + 1)
        status, _ = self.call("POST", "/api/review/%s/flag" % review["id"], {"stop_id": "s1", "text": "hm"})
        self.assertEqual(status, 200)
        status, st = self.call("GET", "/api/state")
        self.assertEqual(st["review_version"], v0 + 2)

    def test_state_validation(self):
        for body in ({"review_id": "../x", "stop_index": 0}, {"review_id": "r", "stop_index": -1},
                     {"review_id": "r", "stop_index": "2"}, {"review_id": "r", "stop_index": 0, "mode": "zoom"}, []):
            status, st = self.call("PUT", "/api/state", body)
            self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
