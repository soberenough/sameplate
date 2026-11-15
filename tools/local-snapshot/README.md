# Local events snapshot

Builds the static files behind "Events near you" so it works on the GitHub Pages site without SamePlate's server.

- `local/<zip3>.json` (daily): events for the next 30 days from the public city calendars in `feeds.json`
  (Norfolk for ZIP prefixes 233-235) and, if the `TICKETMASTER_API_KEY` repo secret is set, Ticketmaster;
  the ZIP centroids of that prefix; sources; attribution.
- `local/<zip3>.places.json` (weekly): OpenStreetMap places (parks, playgrounds, libraries, museums, date spots),
  one Overpass query per ~20-mile square, a 5-second pause between queries.
- `local/index.json`: the prefixes that have files.

Areas: Hampton Roads, ZIP prefixes 233-237. Add one: add the prefix to `AREAS` in `snapshot.py` (and its city
calendars to `backend/server/data/feeds.json`), then run `sync_to_pages.sh`.

The phone downloads its prefix's files only after the family taps "Show events near me", and works out distance and
dates itself; nothing about the family is sent.

Run locally (needs internet): `python snapshot.py --out /tmp/local --areas 235`
Tests: `python -m unittest discover -s . -p 'test_*.py'`
