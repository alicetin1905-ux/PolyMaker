import json
import os
import tempfile
import unittest

from mm.events import EventLog
from mm.publish import build_site, pages_url


class PublishTests(unittest.TestCase):
    def test_build_site_empty_and_with_events(self):
        with tempfile.TemporaryDirectory() as logs, tempfile.TemporaryDirectory() as out:
            build_site(logs, out, 600)  # no bot data yet: still a valid site
            with open(os.path.join(out, "index.html")) as f:
                self.assertIn("window.POLYMAKER_STATIC = true", f.read())
            self.assertTrue(os.path.exists(os.path.join(out, ".nojekyll")))
            ev = EventLog(logs)
            ev.emit("scan", m="a", mid=0.5)
            ev.emit("fill", m="a", side="bid", price=0.4, size=10)
            snap = build_site(logs, out, 600)
            self.assertEqual(snap["publish_every"], 600)
            with open(os.path.join(out, "data", "events.json")) as f:
                types = [e["type"] for e in json.load(f)["events"]]
            self.assertEqual(types, ["fill"])  # stale scan/tick events are not published

    def test_pages_url(self):
        self.assertEqual(pages_url("https://github.com/Foo/Bar.git"), "https://foo.github.io/Bar/")
        self.assertIsNone(pages_url("/some/path"))


if __name__ == "__main__":
    unittest.main()
