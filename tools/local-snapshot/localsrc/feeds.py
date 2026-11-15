"""Public event calendars (iCalendar .ics), configured per 3-digit ZIP prefix in data/feeds.json.

parse_ics() reads the subset of RFC 5545 that city and library calendars use: folded lines, escaped text, DTSTART/DTEND
with TZID, UTC (…Z) or all-day (VALUE=DATE), SUMMARY, DESCRIPTION, LOCATION, URL. A repeating event (RRULE) shows at
its first date only. HTML that some calendars put in LOCATION/DESCRIPTION is stripped.

Tags are read only from what the event itself says; nothing is guessed:
  free    the text says the event is free (e.g. "free admission", "free and open to the public", "no cost"); a
          "gluten-free" or "free parking" does not count. Ticketmaster: also a price range of exactly 0 to 0.
  kids    the text names children, families, teens, toddlers, story time, all ages … (or the feed's hint is "kids").
  adults  21+ / 18+, beer, wine, brewery, cocktails, bar crawls, happy hour: hidden by the Kids & family filter.
  date    arts, music, theatre, comedy, dance (or the feed's hint is "date").
"""
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
FEEDS_FILE = HERE / "data" / "feeds.json"

FREE_RE = re.compile(
    r"(?<![-\w])(free admission|admission is free|admission: free|free event|free to attend|free and open to the public|"
    r"free of charge|free entry|entry is free|free for all ages|free for everyone|free family|free community|free concert|"
    r"free outdoor|free movie|free fun|free storytime|free story time|no cost|no charge|cost: free|price: free|is free)(?![-\w])"
    r"|^\s*free\b(?![-\w])(?!\s+(parking|wi-?fi|shuttle|gifts?|refills?|delivery|shipping|samples?|consultation|estimate))", re.I)
KIDS_RE = re.compile(r"\b(kids?|children|child|famil(y|ies)|toddlers?|teens?|tweens?|youth|story ?time|all ages|"
                     r"preschool|babies|baby|elementary|homeschool|lego|juniors?|niños|familia)\b", re.I)
ADULTS_RE = re.compile(r"(\b(21|18)\s?\+|\b(21|18) and (over|up|older)\b|\bbeer\b|\bwine\b|\bbrew(ery|eries|ing|s)?\b|"
                       r"\bcocktails?\b|\bbar crawl\b|\bhappy hour\b|\bbourbon\b|\bwhiskey\b|\bburlesque\b|\bnightclub\b)", re.I)
DATE_RE = re.compile(r"\b(concert|jazz|symphony|orchestra|opera|ballet|theat(er|re)|play|musical|comedy|improv|dance|"
                     r"gallery|exhibit(ion)?|art walk|film|cinema|live music|wine|dinner)\b", re.I)


def tags_for(text: str, hint: str | None = None, explicit_free: bool = False) -> list[str]:
    t = text or ""
    out = []
    if explicit_free or FREE_RE.search(t):
        out.append("free")
    adults = bool(ADULTS_RE.search(t))
    if (hint == "kids" or KIDS_RE.search(t)) and not adults:
        out.append("kids")
    if adults:
        out.append("adults")
    if hint == "date" or DATE_RE.search(t):
        out.append("date")
    return out


def _unescape(v: str) -> str:
    v = v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    v = re.sub(r"<[^>]+>", " ", v)
    v = html.unescape(v.replace("&nbsp", " "))
    return re.sub(r"[ \t]+", " ", v).strip()


def _when(params: dict, value: str, tz: ZoneInfo) -> tuple[date, str | None] | None:
    """(local date, 'HH:MM' or None for all-day)."""
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d").date(), None
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(tz)
        else:
            src = ZoneInfo(params["TZID"]) if params.get("TZID") else tz
            dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=src).astimezone(tz)
        return dt.date(), dt.strftime("%H:%M")
    except (ValueError, KeyError):
        return None


def parse_ics(text: str, tz_name: str = "America/New_York") -> list[dict]:
    """→ [{name, date, time, end, venue, url, description}] in the feed's local time, sorted by date."""
    tz = ZoneInfo(tz_name)
    lines = re.sub(r"\r?\n[ \t]", "", (text or "").replace("\r\n", "\n")).split("\n")
    out, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None and cur.get("SUMMARY") and cur.get("DTSTART"):
                start = _when(*cur["DTSTART"], tz)
                if start:
                    end = _when(*cur["DTEND"], tz) if cur.get("DTEND") else None
                    url = cur.get("URL", ("", ""))[1].strip()
                    out.append({"name": _unescape(cur["SUMMARY"][1])[:160], "date": start[0].isoformat(), "time": start[1],
                                "end": end[1] if end and end[0] == start[0] else None,
                                "venue": (_unescape(cur["LOCATION"][1])[:160] or None) if cur.get("LOCATION") else None,
                                "url": url if url.startswith("https://") or url.startswith("http://") else None,
                                "description": _unescape(cur.get("DESCRIPTION", ({}, ""))[1])[:600]})
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *ps = head.split(";")
        params = dict(p.split("=", 1) for p in ps if "=" in p)
        if name.upper() in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION", "URL"):
            cur[name.upper()] = (params, value)
    out.sort(key=lambda e: (e["date"], e["time"] or ""))
    return out


@lru_cache(maxsize=1)
def config() -> dict:
    return json.loads(FEEDS_FILE.read_text(encoding="utf-8"))


def feeds_for(zip3: str) -> list[tuple[str, dict]]:
    c = config()
    return [(fid, c["feeds"][fid]) for fid in c.get("areas", {}).get(zip3, []) if fid in c["feeds"]]


def feed_events(parsed: list[dict], feed: dict, day: date, days: int = 7) -> list[dict]:
    """Events in [day, day+days) with tags, shaped like the Ticketmaster ones."""
    end = day + timedelta(days=days)
    out = []
    for e in parsed:
        d = date.fromisoformat(e["date"])
        if not (day <= d < end):
            continue
        text = f"{e['name']}\n{e['description']}"
        out.append({"name": e["name"], "url": e["url"] or feed.get("page"), "date": e["date"], "time": e["time"],
                    "venue": e["venue"], "city": None, "category": None, "price": "free" if FREE_RE.search(text) else None,
                    "priceMin": None, "priceMax": None, "currency": None, "image": None,
                    "tags": tags_for(text, feed.get("hint")), "source": "feed", "sourceName": feed["name"]})
    return out
