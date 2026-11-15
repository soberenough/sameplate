"""Static local-events snapshot: places from Overpass (bbox, weekly), city calendars (30 days), Ticketmaster only with
a key, a failed source keeps the previous section, files written only when they change. stdlib unittest (runs in
GitHub Actions without installs):  python -m unittest discover -s tools/local-snapshot -p 'test_*.py'"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot as S  # noqa: E402

TODAY = date(2026, 11, 20)
OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": 36.8890, "lon": -76.2600, "tags": {"name": "Ghent Playground", "leisure": "playground"}},
    {"type": "way", "id": 2, "center": {"lat": 36.8860, "lon": -76.2900}, "tags": {"name": "Chrysler Museum of Art", "tourism": "museum", "fee": "no"}},
    {"type": "node", "id": 3, "lat": 36.8900, "lon": -76.2700, "tags": {"name": "Doumar's", "amenity": "ice_cream"}},
    {"type": "node", "id": 4, "lat": 40.0000, "lon": -80.0000, "tags": {"name": "Far Away Park", "leisure": "park"}},
]}
ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nDESCRIPTION:Story time for toddlers. Free and open to the public.\r\n"
    "DTSTART;TZID=America/New_York:20261121T103000\r\nSUMMARY:Saturday Story Time\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDESCRIPTION:Craft beer tasting\\, 21+ only\r\nDTSTART:20261122T230000Z\r\nSUMMARY:Brews on the Bay\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=America/New_York:20261210T190000\r\nSUMMARY:Holiday Concert in the Park\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=America/New_York:20270115T190000\r\nSUMMARY:Too far ahead\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART;TZID=America/New_York:20261101T190000\r\nSUMMARY:Already happened\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n")
TM = {"_embedded": {"events": [
    {"id": "E1", "name": "Disney on Ice", "url": "https://www.ticketmaster.com/event/E1",
     "dates": {"start": {"localDate": "2026-11-28", "localTime": "19:00:00"}},
     "classifications": [{"segment": {"name": "Family"}, "genre": {"name": "Ice Shows"}, "family": True}],
     "priceRanges": [{"min": 20, "max": 60, "currency": "USD"}], "_embedded": {"venues": [{"name": "Scope", "city": {"name": "Norfolk"}}]}},
]}}


class Net:
    def __init__(self, overpass=(200, OVERPASS), ics=(200, ICS), tm=(200, TM)):
        self.overpass, self.ics, self.tm = overpass, ics, tm
        self.calls = []

    def http(self, method, url, headers, data):
        self.calls.append(url)
        if "overpass" in url:
            return self.overpass
        if "ticketmaster" in url:
            return self.tm
        return 404, None

    def text(self, url):
        self.calls.append(url)
        return self.ics


def builder(net, key=""):
    return S.Builder(TODAY, key, http=net.http, http_text=net.text, sleep=lambda s: None, log=lambda *a: None)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_builds_events_places_and_index_for_norfolk(self):
        net = Net()
        written = S.run(self.out, ["235"], TODAY, builder(net))
        self.assertEqual(sorted(written), ["235.json", "235.places.json", "index.json"])
        d, pl = self.read("235.json"), self.read("235.places.json")
        self.assertEqual(self.read("index.json")["areas"], ["235"])
        self.assertIn("23517", d["centroids"])
        self.assertTrue(all(z.startswith("235") for z in d["centroids"]))
        names = [p["name"] for p in pl["places"]]
        self.assertIn("Ghent Playground", names)
        self.assertNotIn("Far Away Park", names)          # not near any 235 ZIP
        self.assertEqual(pl["placesAsOf"], TODAY.isoformat())
        evs = {e["name"]: e for e in d["events"]}
        self.assertEqual(set(evs), {"Saturday Story Time", "Brews on the Bay", "Holiday Concert in the Park"})  # 30 days, nothing past
        self.assertIn("free", evs["Saturday Story Time"]["tags"])
        self.assertIn("kids", evs["Saturday Story Time"]["tags"])
        self.assertIn("adults", evs["Brews on the Bay"]["tags"])
        self.assertNotIn("kids", evs["Brews on the Bay"]["tags"])
        self.assertEqual(d["ticketmaster"], "off")
        self.assertFalse(any("ticketmaster" in c for c in net.calls))   # no key → no call
        self.assertEqual(len([c for c in net.calls if "overpass" in c]), len(S.clusters(S.area_centroids("235"))))  # one per cluster
        self.assertEqual(d["sourcesFailed"], [])
        self.assertEqual(d["attribution"]["osm"], "© OpenStreetMap contributors")
        self.assertGreater(len(d["calendars"]), 0)

    def test_big_prefixes_are_split_into_small_query_boxes(self):
        for z in S.AREAS:
            for g in S.clusters(S.area_centroids(z)):
                s, w, n, e = S.bbox_of(g)
                self.assertLess(n - s, S.CLUSTER_DEG + 2 * S.BBOX_MARGIN + 0.01)
                self.assertLess(e - w, S.CLUSTER_DEG + 2 * S.BBOX_MARGIN + 0.01)
        self.assertGreater(len(S.clusters(S.area_centroids("233"))), 1)   # Norfolk to the Eastern Shore

    def test_overpass_query_is_a_bbox(self):
        cents = S.area_centroids("235")
        s, w, n, e = S.bbox_of(cents)
        self.assertTrue(s < 36.89 < n and w < -76.26 < e)
        q = S.bbox_query((s, w, n, e))
        self.assertIn('leisure="playground"', q)
        self.assertNotIn("around:", q)

    def test_ticketmaster_only_with_a_key(self):
        net = Net()
        S.run(self.out, ["235"], TODAY, builder(net, key="k"))
        d = self.read("235.json")
        self.assertEqual(d["ticketmaster"], "ok")
        tm = [e for e in d["events"] if e["source"] == "ticketmaster"]
        self.assertEqual([e["name"] for e in tm], ["Disney on Ice"])
        self.assertIn("kids", tm[0]["tags"])
        self.assertEqual(len([c for c in net.calls if "ticketmaster" in c]), 2)   # family + general
        self.assertTrue(any("classificationName=Family" in c for c in net.calls))

    def test_failed_sources_keep_previous_sections_but_never_old_ticketmaster(self):
        S.run(self.out, ["235"], TODAY, builder(Net(), key="k"))
        # a day later every source fails
        nxt = date(2026, 11, 21)
        bad = Net(overpass=(504, None), ics=(503, None), tm=(500, None))
        b = S.Builder(nxt, "k", http=bad.http, http_text=bad.text, sleep=lambda s: None, log=lambda *a: None)
        # force a places refresh by making the old places look 8 days old
        pl = self.read("235.places.json")
        pl["placesAsOf"] = "2026-11-12"
        (self.out / "235.places.json").write_text(json.dumps(pl))
        S.run(self.out, ["235"], nxt, b)
        d, pl2 = self.read("235.json"), self.read("235.places.json")
        self.assertIn("openstreetmap", d["sourcesFailed"])
        self.assertTrue(any(f.startswith("calendar:") for f in d["sourcesFailed"]))
        self.assertIn("ticketmaster", d["sourcesFailed"])
        self.assertEqual(d["ticketmaster"], "unavailable")
        self.assertGreater(len(pl2["places"]), 0)                       # kept
        names = [e["name"] for e in d["events"]]
        self.assertIn("Holiday Concert in the Park", names)              # calendar events kept
        self.assertNotIn("Disney on Ice", names)                         # Ticketmaster never kept
        self.assertTrue(all(e["date"] >= "2026-11-21" for e in d["events"]))

    def test_places_refreshed_weekly_not_daily(self):
        S.run(self.out, ["235"], TODAY, builder(Net()))
        net = Net()
        written = S.run(self.out, ["235"], date(2026, 11, 22), builder(net))
        self.assertFalse(any("overpass" in c for c in net.calls))
        self.assertNotIn("235.places.json", written)

    def test_unchanged_data_writes_nothing(self):
        S.run(self.out, ["235"], TODAY, builder(Net()))
        self.assertEqual(S.run(self.out, ["235"], TODAY, builder(Net())), [])

    def test_hampton_roads_areas_all_have_zips(self):
        for z in S.AREAS:
            self.assertTrue(S.area_centroids(z), z)


if __name__ == "__main__":
    unittest.main()
