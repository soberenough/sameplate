"""Ideas near a family for date night and for the kids: places from OpenStreetMap (Overpass API), events from
Ticketmaster and from public city / library calendars (feeds.py, data/feeds.json).

  GET /api/local?zip=23517&date=2026-11-20  →  {places:[…], events:[…]|null, …}

Only the ZIP and the date reach us (the family opts in on the phone first). Nothing about the request is logged; the
cache keys hold a map cell or a ZIP + date, never an IP.

Places (no key needed): one Overpass query per map cell (~0.1°, about 7 miles), cached 7 days so a whole town shares
one query. Distances are worked out from the family's own ZIP centroid on each request.
Events (free Ticketmaster Discovery key in TICKETMASTER_API_KEY; 5,000 calls/day by default): one call per ZIP + date,
cached 6 hours (their terms: no more than 24 h), shown as Ticketmaster gives them with a link to the event on
Ticketmaster. No key → events are simply left out.

ZIP centroids: data/zip_centroids.csv.gz, from the BSD-licensed "zipcodes" npm package (see ZIP_CENTROIDS_LICENSE.txt).
"""
import gzip
from .feeds import dedupe_events, feed_events, feeds_for, parse_ics, tags_for
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
CENTROIDS = HERE / "data" / "zip_centroids.csv.gz"
UA = "SamePlate/0.1 (family meal planner; https://sameplate.sponsorstrike.com)"
OVERPASS = "https://overpass-api.de/api/interpreter"
TM_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
PLACES_TTL = 7 * 86400
PLACES_FAIL_TTL = 600          # after an Overpass failure, wait 10 minutes before asking again
EVENTS_TTL = 6 * 3600          # Ticketmaster: cache no longer than 24 h
EVENTS_FAIL_TTL = 600
FEED_TTL = 6 * 3600
FEED_FAIL_TTL = 1800
TM_DAILY_BUDGET = 4500         # under the free 5,000/day quota
PLACE_RADIUS_M = 16000         # ~10 miles around the map cell's centre
MAX_MILES = 12.0
PER_KIND = 8
MAX_PLACES = 60
MAX_EVENTS = 20
RE_ZIP = re.compile(r"^\d{5}$")

# kind → (Overpass filters, price label, kid-friendly)
KINDS: dict[str, tuple[list[str], str, bool]] = {
    "playground": (['leisure="playground"'], "free", True),
    "splash": (['leisure="splash_pad"', 'amenity="fountain"]["fountain"="splash_pad"', 'playground="splash_pad"'], "free", True),
    "library": (['amenity="library"'], "free", True),
    "park": (['leisure="park"'], "free", True),
    "viewpoint": (['tourism="viewpoint"'], "free", True),
    "beach": (['natural="beach"'], "free", True),
    "nature": (['leisure="nature_reserve"'], "free", True),
    "garden": (['leisure="garden"]["garden:type"="botanical"'], "$", True),
    "museum": (['tourism="museum"'], "$", True),
    "gallery": (['tourism="gallery"', 'amenity="arts_centre"'], "free", True),
    "theatre": (['amenity="theatre"'], "$$", True),
    "cinema": (['amenity="cinema"'], "$", True),
    "bowling": (['leisure="bowling_alley"'], "$", True),
    "minigolf": (['leisure="miniature_golf"'], "$", True),
    "escape": (['leisure="escape_game"'], "$$", True),
    "zoo": (['tourism="zoo"', 'tourism="aquarium"'], "$$", True),
    "dessert": (['amenity="ice_cream"', 'shop="chocolate"'], "$", True),
    "music": (['amenity="music_venue"'], "$$", False),
}
# Which kinds suit a date night (the rest are family outings).
DATE_KINDS = {"viewpoint", "beach", "nature", "garden", "museum", "gallery", "theatre", "cinema", "bowling", "minigolf",
              "escape", "zoo", "dessert", "music", "park"}


def http_text(url: str):
    """(status, text-or-None) for a calendar feed. Swappable in tests."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/calendar, */*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 599, None


def http(method: str, url: str, headers: dict, data: bytes | None):
    """(status, json-or-None). Swappable in tests."""
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 599, None


@lru_cache(maxsize=1)
def centroids() -> dict[str, tuple[float, float]]:
    out = {}
    with gzip.open(CENTROIDS, "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            z, la, lo = line.rstrip("\n").split(",")
            out[z] = (float(la), float(lo))
    return out


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def cell_of(pt: tuple[float, float]) -> tuple[float, float]:
    return (round(pt[0], 1), round(pt[1], 1))


def overpass_query(center: tuple[float, float]) -> str:
    la, lo = center
    parts = []
    for filters, _, _ in KINDS.values():
        for f in filters:
            parts.append(f'nwr[{f}]["name"](around:{PLACE_RADIUS_M},{la},{lo});')
    return "[out:json][timeout:25];(" + "".join(parts) + ");out center tags 400;"


def kind_of(tags: dict) -> str | None:
    t = tags.get
    if t("leisure") == "garden" and t("garden:type") == "botanical":
        return "garden"
    if t("leisure") == "splash_pad" or t("playground") == "splash_pad" or (t("amenity") == "fountain" and t("fountain") == "splash_pad"):
        return "splash"
    pairs = [("leisure", "playground", "playground"), ("amenity", "library", "library"), ("leisure", "park", "park"), ("tourism", "viewpoint", "viewpoint"), ("natural", "beach", "beach"),
             ("leisure", "nature_reserve", "nature"), ("tourism", "museum", "museum"), ("tourism", "gallery", "gallery"),
             ("amenity", "arts_centre", "gallery"), ("amenity", "theatre", "theatre"), ("amenity", "cinema", "cinema"),
             ("leisure", "bowling_alley", "bowling"), ("leisure", "miniature_golf", "minigolf"),
             ("leisure", "escape_game", "escape"), ("tourism", "zoo", "zoo"), ("tourism", "aquarium", "zoo"),
             ("amenity", "ice_cream", "dessert"), ("shop", "chocolate", "dessert"), ("amenity", "music_venue", "music")]
    for k, v, kind in pairs:
        if t(k) == v:
            return kind
    return None


def parse_overpass(body) -> list[dict]:
    """Overpass JSON → [{name, kind, lat, lon, price, kids, url}] (dedupes same name + kind)."""
    out, seen = [], set()
    els = body.get("elements") if isinstance(body, dict) else None
    for el in els if isinstance(els, list) else []:
        if not isinstance(el, dict):
            continue
        tags = el.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        kind = kind_of(tags)
        if not name or not kind or len(name) > 120:
            continue
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lon = el.get("lon", (el.get("center") or {}).get("lon"))
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        key = (name.lower(), kind)
        if key in seen:
            continue
        seen.add(key)
        _, price, kids = KINDS[kind]
        fee = str(tags.get("fee") or "").lower()
        if fee == "no":
            price = "free"
        elif fee == "yes" and price == "free":
            price = "$"
        etype = el.get("type") if el.get("type") in ("node", "way", "relation") else "node"
        out.append({"name": name, "kind": kind, "lat": round(float(lat), 5), "lon": round(float(lon), 5), "price": price,
                    "kids": kids, "date": kind in DATE_KINDS, "url": f"https://www.openstreetmap.org/{etype}/{int(el.get('id', 0))}"})
    return out


def _price_label(ranges) -> str | None:
    if not isinstance(ranges, list) or not ranges:
        return None
    mins = [r.get("min") for r in ranges if isinstance(r, dict) and isinstance(r.get("min"), (int, float))]
    if not mins:
        return None
    lo = min(mins)
    return "free" if lo <= 0 else "$" if lo < 30 else "$$"


def parse_events(body) -> list[dict]:
    """Ticketmaster Discovery events.json → [{name, url, date, time, venue, city, category, price, priceMin, priceMax, image}]."""
    evs = ((body or {}).get("_embedded") or {}).get("events") if isinstance(body, dict) else None
    out = []
    for e in evs or []:
        if not isinstance(e, dict) or not e.get("name") or not str(e.get("url") or "").startswith("https://"):
            continue
        start = ((e.get("dates") or {}).get("start") or {})
        pr = e.get("priceRanges") or []
        mins = [r["min"] for r in pr if isinstance(r, dict) and isinstance(r.get("min"), (int, float))]
        maxs = [r["max"] for r in pr if isinstance(r, dict) and isinstance(r.get("max"), (int, float))]
        venue = (((e.get("_embedded") or {}).get("venues") or [{}])[0]) or {}
        cls = ((e.get("classifications") or [{}])[0]) or {}
        cat = " · ".join(x for x in ((cls.get("segment") or {}).get("name"), (cls.get("genre") or {}).get("name"))
                         if x and x != "Undefined")
        imgs = [i for i in e.get("images") or [] if isinstance(i, dict) and str(i.get("url", "")).startswith("https://")]
        img = next((i["url"] for i in sorted(imgs, key=lambda i: abs((i.get("width") or 0) - 640)) if i.get("ratio") == "16_9"),
                   imgs[0]["url"] if imgs else None)
        family = any(isinstance(c, dict) and (c.get("family") is True or "family" in str((c.get("segment") or {}).get("name", "")).lower()
                     or "children" in str((c.get("genre") or {}).get("name", "")).lower()) for c in e.get("classifications") or [])
        adult_flag = bool((e.get("ageRestrictions") or {}).get("legalAgeEnforced"))
        text = " ".join(str(x) for x in (e.get("name"), e.get("info"), e.get("pleaseNote"), cat) if x)
        explicit_free = bool(mins) and min(mins) == 0 and (not maxs or max(maxs) == 0)
        tags = tags_for(text, "kids" if family else ("date" if (cls.get("segment") or {}).get("name") in ("Music", "Arts & Theatre") else None),
                        explicit_free)
        if adult_flag:
            tags = [t for t in tags if t != "kids"] + ([] if "adults" in tags else ["adults"])
        out.append({"name": str(e["name"])[:160], "url": e["url"], "date": start.get("localDate"), "time": start.get("localTime"),
                    "tags": tags, "source": "ticketmaster", "sourceName": "Ticketmaster", "id": str(e.get("id") or "")[:40],
                    "venue": venue.get("name"), "city": (venue.get("city") or {}).get("name"), "category": cat or None,
                    "price": "free" if "free" in tags else _price_label(pr) if _price_label(pr) != "free" else None, "priceMin": min(mins) if mins else None, "priceMax": max(maxs) if maxs else None,
                    "currency": (pr[0].get("currency") if pr and isinstance(pr[0], dict) else None), "image": img})
        if len(out) >= MAX_EVENTS:
            break
    return out


class Local:
    def __init__(self, store, transport=None, clock=time.time, text_transport=None):
        self.store, self.http, self.clock = store, transport or http, clock
        self.http_text = text_transport or http_text
        self._lock = threading.Lock()
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS local_cache (k TEXT PRIMARY KEY, v TEXT NOT NULL, expires REAL NOT NULL)")

    # ------------------------------------------------------------------ cache (no IPs, ever)
    def _get(self, k: str):
        with self.store.lock:
            row = self.store.db.execute("SELECT v, expires FROM local_cache WHERE k=?", (k,)).fetchone()
        if row and row[1] > self.clock():
            return json.loads(row[0])
        return None

    def _put(self, k: str, v, ttl: float) -> None:
        with self.store.lock:
            self.store.db.execute("DELETE FROM local_cache WHERE expires < ?", (self.clock(),))
            self.store.db.execute("INSERT INTO local_cache(k,v,expires) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                                  "expires=excluded.expires", (k, json.dumps(v), self.clock() + ttl))

    # ------------------------------------------------------------------ places
    def places(self, zip_code: str) -> tuple[list[dict], str]:
        home = centroids().get(zip_code)
        if not home:
            return [], "unknown_zip"
        cell = cell_of(home)
        key = f"osm:{cell[0]:.1f},{cell[1]:.1f}"
        cached = self._get(key)
        if cached is None:
            st, body = self.http("POST", OVERPASS, {"Content-Type": "application/x-www-form-urlencoded"},
                                 urllib.parse.urlencode({"data": overpass_query(cell)}).encode())
            if st != 200:
                self._put(key, {"failed": True}, PLACES_FAIL_TTL)
                return [], "unavailable"
            cached = {"places": parse_overpass(body)}
            self._put(key, cached, PLACES_TTL)
        if cached.get("failed"):
            return [], "unavailable"
        near = []
        for p in cached["places"]:
            d = miles(home, (p["lat"], p["lon"]))
            if d <= MAX_MILES:
                near.append({k: v for k, v in p.items() if k not in ("lat", "lon")} | {"miles": round(d, 1)})
        near.sort(key=lambda p: p["miles"])
        per, out = {}, []
        for p in near:
            if per.get(p["kind"], 0) < PER_KIND:
                per[p["kind"]] = per.get(p["kind"], 0) + 1
                out.append(p)
        return out[:MAX_PLACES], "ok"

    # ------------------------------------------------------------------ events
    def _budget_ok(self) -> bool:
        k = f"tm_calls:{date.fromtimestamp(self.clock()).isoformat()}"
        with self._lock:
            n = self.store.get(k, 0)
            if n >= TM_DAILY_BUDGET:
                return False
            self.store.put(k, n + 1)
            return True

    def _tm_call(self, params: dict) -> list[dict] | None:
        if not self._budget_ok():
            return None
        st, body = self.http("GET", f"{TM_EVENTS}?{urllib.parse.urlencode(params)}", {"Accept": "application/json"}, None)
        return parse_events(body) if st == 200 else None

    def ticketmaster(self, zip_code: str, day: date) -> tuple[list[dict] | None, str]:
        key_env = os.environ.get("TICKETMASTER_API_KEY", "")
        if not key_env:
            return None, "off"
        home = centroids().get(zip_code)
        if not home:
            return [], "unknown_zip"
        key = f"tm:{zip_code}:{day.isoformat()}"
        cached = self._get(key)
        if cached is not None:
            return (None, "unavailable") if cached.get("failed") else (cached["events"], "ok")
        end = day + timedelta(days=6)
        base = {"apikey": key_env, "latlong": f"{home[0]},{home[1]}", "radius": "25", "unit": "miles", "countryCode": "US",
                "startDateTime": f"{day.isoformat()}T00:00:00Z", "endDateTime": f"{end.isoformat()}T23:59:59Z",
                "sort": "date,asc", "size": str(MAX_EVENTS), "locale": "*"}
        general = self._tm_call(base)
        if general is None:
            self._put(key, {"failed": True}, EVENTS_FAIL_TTL)
            return None, "unavailable"
        family = self._tm_call({**base, "classificationName": "Family"}) or []   # family shows can be crowded out
        seen, evs = set(), []
        for e in family + general:
            k = e.get("id") or e["url"]
            if k not in seen:
                seen.add(k)
                evs.append(e)
        evs.sort(key=lambda e: (e.get("date") or "", e.get("time") or ""))
        self._put(key, {"events": evs}, EVENTS_TTL)
        return evs, "ok"

    def calendars(self, zip_code: str, day: date) -> tuple[list[dict], list[dict]]:
        """Events from the public calendars configured for this ZIP prefix, and the list of those calendars."""
        out, used = [], []
        for fid, feed in feeds_for(zip_code[:3]):
            used.append({"name": feed["name"], "url": feed.get("page") or feed["url"]})
            key = f"feed:{fid}"
            cached = self._get(key)
            if cached is None:
                st, body = self.http_text(feed["url"])
                if st != 200 or not body or "BEGIN:VCALENDAR" not in body:
                    self._put(key, {"failed": True}, FEED_FAIL_TTL)
                    continue
                cached = {"events": parse_ics(body, feed.get("tz", "America/New_York"))}
                self._put(key, cached, FEED_TTL)
            if cached.get("failed"):
                continue
            out.extend(feed_events(cached["events"], feed, day))
        return out, used

    def events(self, zip_code: str, day: date) -> tuple[list[dict], dict]:
        tm, tm_note = self.ticketmaster(zip_code, day)
        cal, used = self.calendars(zip_code, day)
        evs = dedupe_events(list(tm or []) + cal)
        evs.sort(key=lambda e: ("free" not in e.get("tags", []), e.get("date") or "", e.get("time") or "99:99"))
        return evs, {"ticketmaster": tm_note, "calendars": used}

    # ------------------------------------------------------------------ the endpoint's answer
    def for_request(self, zip_code: str, day_s: str, today: date | None = None) -> dict | None:
        if not RE_ZIP.fullmatch(zip_code or ""):
            return None
        try:
            day = date.fromisoformat(day_s)
        except (TypeError, ValueError):
            return None
        today = today or date.fromtimestamp(self.clock())
        if day < today - timedelta(days=1) or day > today + timedelta(days=60):
            return None
        places, pn = self.places(zip_code)
        events, en = self.events(zip_code, day)
        return {"day": day.isoformat(), "places": places, "placesNote": pn, "events": events, "eventsNote": en["ticketmaster"],
                "calendars": en["calendars"], "attribution": {"osm": "© OpenStreetMap contributors", "osmUrl": "https://www.openstreetmap.org/copyright",
                                "ticketmaster": "Event data and tickets from Ticketmaster"}}
