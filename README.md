# HA Tracker – Hatzolah Air

Live map + one card per tail for Hatzolah Air (also called HatzAir): the US jets and helicopter, plus the Israeli Sikorsky S-76s photographed in Hatzolah Air titles.

## Layout
- `server.py` – tiny stdlib-only Python backend. Every 30 s it makes ONE batched request for all tails
  (by ICAO hex) to `api.adsb.lol/v2/hex/…` (fallback `opendata.adsb.fi/api/v2/hex/…`), stores the latest
  and last-known position of each aircraft in `data/state.json`, works out the nearest airport
  (`data/airports.json`, from OurAirports), and serves `public/` plus `/api/fleet` and `/api/state`.
- `public/` – static front end (MapLibre + OpenFreeMap Positron tiles, light navy-header layout, PWA manifest + service worker).
- `data/fleet.json` – **the tail list. Edit this to add aircraft.**

US type, year, owner, and Mode S hex come from the FAA registry. Israeli registrations and hexes
come from the Israeli civil register / Mode S lookups, with operator identity from dated photos
in Hatzolah Air titles.

## Adding a tail
Add to `tails`: `{"tail":"N123AB","model":"Citation X","owner":"Example LLC","category":"jet","region":"United States","color":"#38bdf8"}`
Use `"category":"helicopter"` for rotorcraft (shows a HELI badge and helicopter map icon).
For unconfirmed links, set `"confidence":"low"` — they stay hidden until the "Show unconfirmed tails" box is checked.
`hex` is optional for US N-numbers – the server looks the Mode S hex up on registry.faa.gov automatically within a minute
(file is re-read whenever it changes; no restart needed). Foreign tails need a Mode S `hex`.

## Run
    ./run.sh            # http://localhost:8136   (POLL_SECONDS=30, PORT=8136 env vars)

## Hosting
A small backend is required (no pure-static hosting). Any host that can run `python3 server.py`
works (Render, Fly.io, Railway, a VPS, a home PC).
From a normal machine `./tunnel.sh` gives a free trycloudflare.com URL with no account (URL changes each restart).

## Deployed on Render (free plan)
Public repo `urgentflights/ha-tracker`, auto-deploys on every push to `main`.
To add a tail: edit `data/fleet.json` on GitHub (pencil icon) and commit – Render redeploys in ~2 min.
Free plan: sleeps after ~15 min without visitors (first load then takes ~30-60 s); disk is ephemeral, so
last-known positions reset to the committed `data/state.json` snapshot on each restart/deploy and only
accumulate while the service is awake.
