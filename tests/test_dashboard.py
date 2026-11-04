import json
import os
import tempfile
import unittest

from mm.dashboard import CsvTail, Hub
from mm.events import EventLog
from mm.scanner import Filters
from mm.strategy import StrategyConfig


class CsvTailTests(unittest.TestCase):
    def test_incremental_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.csv")
            with open(p, "w") as f:
                f.write("a,b\n1,2\n")
            t = CsvTail(p, keep=10)
            self.assertEqual(t.read(), [["1", "2"]])
            with open(p, "a") as f:
                f.write("3,4\n5,")  # incomplete last line is not consumed yet
            self.assertEqual(t.read(), [["1", "2"], ["3", "4"]])
            with open(p, "a") as f:
                f.write("6\n")
            self.assertEqual(t.read()[-1], ["5", "6"])
            os.remove(p)
            self.assertEqual(t.read(), [])
            with open(p, "w") as f:
                f.write("a,b\n9,9\n")
            self.assertEqual(t.read(), [["9", "9"]])


class HubTests(unittest.TestCase):
    def test_ingest_mid_history_and_moves(self):
        with tempfile.TemporaryDirectory() as d:
            hub = Hub(d, StrategyConfig(), Filters(), scan_every=0)
            hub._ingest({"ts": 100.0, "type": "scan", "m": "a", "mid": 0.5}, live=True)
            hub._ingest({"ts": 110.0, "type": "scan", "m": "a", "mid": 0.5}, live=True)
            hub._ingest({"ts": 120.0, "type": "scan", "m": "a", "mid": 0.52}, live=True)
            evs = hub.events_after(0)["events"]
            self.assertEqual([e["moved"] for e in evs], [False, False, True])
            # unchanged mid within a minute is not stored twice
            self.assertEqual([m for _, m in hub.mids["a"]], [0.5, 0.52])
            self.assertTrue(hub.events_after(999)["reset"])

    def test_event_log_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            EventLog(d).emit("fill", m="a", side="bid", price=0.4, size=10)
            with open(os.path.join(d, "events.jsonl")) as f:
                ev = json.loads(f.readline())
            self.assertEqual((ev["type"], ev["m"], ev["price"]), ("fill", "a", 0.4))


if __name__ == "__main__":
    unittest.main()
