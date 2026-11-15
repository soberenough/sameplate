"""Static "Events near you" files for the GitHub Pages site (no SamePlate server needed).

Runs daily in GitHub Actions in the soberenough/sameplate repo (.github/workflows/local-snapshot.yml) and writes

  local/<zip3>.json         events for the next 30 days (public city calendars; Ticketmaster only when
                            TICKETMASTER_API_KEY is set), the ZIP centroids of that prefix, sources and attribution (daily)
  local/<zip3>.places.json  places from OpenStreetMap (refreshed weekly, so the daily commit stays small)
  local/index.json    which ZIP prefixes have a file

The app downloads the file for the family's ZIP prefix after they tap "Show events near me" and works out distances
and dates on the phone. Nothing about the family is sent anywhere.

Same rules as the server (backend/server/local.py + feeds.py, copied next to this file as localsrc/ by
sync_to_pages.sh): free only when the source says so, kid-friendly hides 21+, polite to Overpass (one query per area,
refreshed weekly), Ticketmaster refreshed every run (their terms: data no older than 24 h).

A source that fails keeps the previous file's section (calendars, places) and is named in "sourcesFailed".
Ticketmaster events are never kept from an earlier run.

  python snapshot.py --out ../../local [--areas 233,234] [--today 2026-11-15]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:  # the copy in the Pages repo
    sys.path.insert(0, str(HERE))
    from localsrc import feeds, local  # type: ignore
except ImportError:  # the source tree (money-meals/local-snapshot next to money-meals/backend)
    sys.path.insert(0, str(HERE.parent / "backend"))
    from server import feeds, local  # type: ignore

AREAS = ["233", "234", "235", "236", "237"]   # Hampton Roads
EVENT_DAYS = 30
PLACES_MAX_AGE_DAYS = 7
BBOX_MARGIN = 0.12                             # degrees (~8 miles) around a cluster's ZIPs
CLUSTER_DEG = 0.3                              # ZIPs are grouped into ~20-mile squares: one Overpass query each
PER_KIND_PER_CLUSTER = 60
MAX_PLACES = 2500
TM_SIZE = 100
FORMAT = 1


def bbox_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    parts = []
    for filters, _, _ in local.KINDS.values():
        for f in filters:
            parts.append(f'nwr[{f}]["name"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});')
    return "[out:json][timeout:90];(" + "".join(parts) + f");out center tags {MAX_PLACES * 2};"


def area_centroids(zip3: str) -> dict[str, list[float]]:
    return {z: [round(c[0], 4), round(c[1], 4)] for z, c in sorted(local.centroids().items()) if z.startswith(zip3)}


def bbox_of(cents: dict[str, list[float]]) -> tuple[float, float, float, float]:
    la = [c[0] for c in cents.values()]
    lo = [c[1] for c in cents.values()]
    return (min(la) - BBOX_MARGIN, min(lo) - BBOX_MARGIN, max(la) + BBOX_MARGIN, max(lo) + BBOX_MARGIN)


def clusters(cents: dict[str, list[float]]) -> list[dict[str, list[float]]]:
    """ZIP centroids grouped into CLUSTER_DEG squares (a prefix like 233 spans Norfolk to the Eastern Shore)."""
    groups: dict[tuple[int, int], dict[str, list[float]]] = {}
    for z, c in cents.items():
        groups.setdefault((int(c[0] // CLUSTER_DEG), int(c[1] // CLUSTER_DEG)), {})[z] = c
    return [groups[k] for k in sorted(groups)]


def strip_volatile(d: dict | None) -> dict | None:
    if not d:
        return d
    return {k: v for k, v in d.items() if k not in ("asOf",)}


class Builder:
    def __init__(self, today: date, tm_key: str = "", http=None, http_text=None, sleep=time.sleep, log=print):
        self.today, self.tm_key = today, tm_key
        self.http = http or local.http
        self.http_text = http_text or local.http_text
        self.sleep, self.log = sleep, log
        self._feed_cache: dict[str, list[dict] | None] = {}

    # -------------------------------------------------------------- sources
    def places(self, cents: dict[str, list[float]], prev: dict | None, failed: list[str]) -> tuple[list[dict], str]:
        """prev: the previous <zip3>.places.json."""
        prev_places, prev_at = (prev or {}).get("places"), (prev or {}).get("placesAsOf")
        if prev_places is not None and prev_at:
            age = (self.today - date.fromisoformat(prev_at[:10])).days
            if 0 <= age < PLACES_MAX_AGE_DAYS:
                return prev_places, prev_at
        out, seen = [], set()
        for group in clusters(cents):
            q = bbox_query(bbox_of(group))
            st, body = self.http("POST", local.OVERPASS, {"Content-Type": "application/x-www-form-urlencoded"},
                                 urllib.parse.urlencode({"data": q}).encode())
            self.sleep(5)  # be polite: one query at a time, a pause between
            if st != 200 or not isinstance(body, dict):
                failed.append("openstreetmap")
                return (prev_places or []), (prev_at or "")
            pts = list(group.values())
            mid = (sum(c[0] for c in pts) / len(pts), sum(c[1] for c in pts) / len(pts))
            near = []
            for p in local.parse_overpass(body):
                d = min(local.miles((p["lat"], p["lon"]), (c[0], c[1])) for c in pts)
                if d <= local.MAX_MILES:
                    near.append((local.miles((p["lat"], p["lon"]), mid), p))
            near.sort(key=lambda x: x[0])
            per: dict[str, int] = {}
            for _, p in near:
                key = (p["name"].lower(), p["kind"], round(p["lat"], 3), round(p["lon"], 3))
                if key in seen or per.get(p["kind"], 0) >= PER_KIND_PER_CLUSTER:
                    continue
                seen.add(key)
                per[p["kind"]] = per.get(p["kind"], 0) + 1
                out.append(p)
        out.sort(key=lambda p: (p["kind"], p["name"].lower()))
        return out[:MAX_PLACES], self.today.isoformat()

    def _feed(self, fid: str, feed: dict) -> list[dict] | None:
        if fid not in self._feed_cache:
            st, body = self.http_text(feed["url"])
            ok = st == 200 and body and "BEGIN:VCALENDAR" in body
            self._feed_cache[fid] = feeds.parse_ics(body, feed.get("tz", "America/New_York")) if ok else None
        return self._feed_cache[fid]

    def calendars(self, zip3: str, prev: dict | None, failed: list[str]) -> tuple[list[dict], list[dict]]:
        evs, used = [], []
        prev_evs = [e for e in (prev or {}).get("events", []) if e.get("source") == "feed" and (e.get("date") or "") >= self.today.isoformat()]
        for fid, feed in feeds.feeds_for(zip3):
            used.append({"name": feed["name"], "url": feed.get("page") or feed["url"]})
            parsed = self._feed(fid, feed)
            if parsed is None:
                failed.append(f"calendar:{fid}")
                evs.extend(e for e in prev_evs if e.get("sourceName") == feed["name"])
                continue
            evs.extend(feeds.feed_events(parsed, feed, self.today, days=EVENT_DAYS))
        return evs, used

    def ticketmaster(self, cents: dict[str, list[float]], failed: list[str]) -> tuple[list[dict], str]:
        if not self.tm_key:
            return [], "off"
        la = sum(c[0] for c in cents.values()) / len(cents)
        lo = sum(c[1] for c in cents.values()) / len(cents)
        end = self.today + timedelta(days=EVENT_DAYS)
        base = {"apikey": self.tm_key, "latlong": f"{la:.4f},{lo:.4f}", "radius": "30", "unit": "miles", "countryCode": "US",
                "startDateTime": f"{self.today.isoformat()}T00:00:00Z", "endDateTime": f"{end.isoformat()}T23:59:59Z",
                "sort": "date,asc", "size": str(TM_SIZE), "locale": "*"}
        old_max, local.MAX_EVENTS = local.MAX_EVENTS, TM_SIZE
        try:
            out, seen = [], set()
            for extra in ({"classificationName": "Family"}, {}):
                st, body = self.http("GET", f"{local.TM_EVENTS}?{urllib.parse.urlencode({**base, **extra})}", {"Accept": "application/json"}, None)
                self.sleep(0.5)
                if st != 200:
                    failed.append("ticketmaster")
                    return [], "unavailable"
                for e in local.parse_events(body):
                    k = e.get("id") or e["url"]
                    if k not in seen:
                        seen.add(k)
                        out.append(e)
            return out, "ok"
        finally:
            local.MAX_EVENTS = old_max

    # -------------------------------------------------------------- one area
    def area(self, zip3: str, prev: dict | None, prev_places: dict | None = None) -> tuple[dict, dict] | None:
        """→ (events file, places file) for one ZIP prefix."""
        cents = area_centroids(zip3)
        if not cents:
            self.log(f"{zip3}: no ZIPs, skipped")
            return None
        failed: list[str] = []
        places, places_at = self.places(cents, prev_places, failed)
        cal, used = self.calendars(zip3, prev, failed)
        tm, tm_note = self.ticketmaster(cents, failed)
        evs = tm + cal
        evs = [e for e in evs if e.get("date") and self.today.isoformat() <= e["date"] <= (self.today + timedelta(days=EVENT_DAYS)).isoformat()]
        evs.sort(key=lambda e: (e.get("date") or "", e.get("time") or "99:99", e.get("name") or ""))
        attribution = {"osm": "© OpenStreetMap contributors", "osmUrl": "https://www.openstreetmap.org/copyright",
                       "ticketmaster": "Event data and tickets from Ticketmaster"}
        out = {"v": FORMAT, "zip3": zip3, "asOf": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
               "eventsFrom": self.today.isoformat(), "eventDays": EVENT_DAYS, "placesFile": f"{zip3}.places.json",
               "placesAsOf": places_at, "centroids": cents, "events": evs, "calendars": used, "ticketmaster": tm_note,
               "sourcesFailed": sorted(set(failed)), "attribution": attribution}
        pl = {"v": FORMAT, "zip3": zip3, "placesAsOf": places_at, "places": places, "attribution": attribution}
        validate(out, pl, self.today)
        self.log(f"{zip3}: {len(places)} places, {len(evs)} events ({len(tm)} Ticketmaster), failed: {out['sourcesFailed'] or 'none'}")
        return out, pl


def validate(d: dict, pl: dict, today: date) -> None:
    assert d["v"] == FORMAT and len(d["zip3"]) == 3 and d["centroids"], "bad header"
    for p in pl["places"]:
        assert p.get("name") and p.get("kind") in local.KINDS and isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float)), f"bad place {p}"
        assert str(p.get("url", "")).startswith("https://www.openstreetmap.org/"), f"bad place url {p}"
    for e in d["events"]:
        assert e.get("name") and e.get("date") and e["date"] >= today.isoformat(), f"bad event {e}"
        assert e.get("url") is None or str(e["url"]).startswith(("https://", "http://")), f"bad event url {e}"
        if "kids" in e.get("tags", []):
            assert "adults" not in e["tags"], f"kids + adults {e}"


def run(out_dir: Path, areas: list[str], today: date, builder: Builder) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written, have = [], []
    def load(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except ValueError:
            return None

    for z in areas:
        path, ppath = out_dir / f"{z}.json", out_dir / f"{z}.places.json"
        prev, prev_places = load(path), load(ppath)
        res = builder.area(z, prev, prev_places)
        if res is None:
            continue
        data, pl = res
        have.append(z)
        if pl != prev_places:
            ppath.write_text(json.dumps(pl, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            written.append(ppath.name)
        if strip_volatile(data) != strip_volatile(prev):
            path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            written.append(path.name)
    idx = out_dir / "index.json"
    new_idx = {"v": FORMAT, "areas": sorted(have)}
    old_idx = json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else None
    if new_idx != old_idx:
        idx.write_text(json.dumps(new_idx), encoding="utf-8")
        written.append("index.json")
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--areas", default=",".join(AREAS))
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: today in America/New_York)")
    a = ap.parse_args(argv)
    if a.today:
        today = date.fromisoformat(a.today)
    else:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).date()
    b = Builder(today, os.environ.get("TICKETMASTER_API_KEY", "").strip())
    written = run(Path(a.out), [z.strip() for z in a.areas.split(",") if z.strip()], today, b)
    print(f"wrote: {', '.join(written) if written else 'nothing changed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
