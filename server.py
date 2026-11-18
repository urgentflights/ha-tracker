#!/usr/bin/env python3
"""HA Tracker - tiny backend: polls free public ADS-B APIs for the tails in data/fleet.json,
remembers last-known positions in data/state.json, serves the static app in public/.
Stdlib only. Run: python3 server.py [port]"""
import json, math, os, re, sys, threading, time, urllib.request, gzip
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data'); PUB = os.path.join(ROOT, 'public')
FLEET = os.path.join(DATA, 'fleet.json'); STATE = os.path.join(DATA, 'state.json')
POLL_S = int(os.environ.get('POLL_SECONDS', '30'))
UA = 'HATracker/1.0 (small private fleet tracker, ~1 req/30s)'
# Query by ICAO hex and by registration so a Privacy ICAO Address (PIA)
# still matches when the transponder reports our tail in `r` or `flight`.
SOURCES = [
    # adsb.fi has no batched /v2/reg (returns 400). Registration is queried on
    # adsb.lol only, which is enough to catch a Privacy ICAO Address (PIA).
    ('adsb.lol',
     'https://api.adsb.lol/v2/hex/{}',
     'https://api.adsb.lol/v2/reg/{}'),
    ('adsb.fi',
     'https://opendata.adsb.fi/api/v2/hex/{}',
     None),
]
TRAIL_MAX = 400
INGEST_SECRET = os.environ.get('INGEST_SECRET', '')
ingest_info = {'lastAt': None, 'count': 0, 'source': None, 'syncAt': None}

lock = threading.Lock()
fleet = {'tails': []}; fleet_mtime = 0
state = {'planes': {}, 'lastPoll': None, 'lastSource': None, 'lastError': None}
if os.path.exists(STATE):
    try: state.update(json.load(open(STATE)))
    except Exception as e: print('state load failed', e, flush=True)

# ---- airports (OurAirports subset) ----
AP = json.load(open(os.path.join(DATA, 'airports.json')))
def nm(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2-lat1)*p/2)**2 + math.cos(lat1*p)*math.cos(lat2*p)*math.sin((lon2-lon1)*p/2)**2
    return 2 * 3440.065 * math.asin(math.sqrt(a))
def nearest_airport(lat, lon, maxnm=40, minsize=1):
    best = None
    for a in AP:
        if a[8] < minsize: continue
        if abs(a[6]-lat) > 0.8 or abs(a[7]-lon) > 1.0: continue
        d = nm(lat, lon, a[6], a[7])
        score = d - (1.5 if a[8] >= 2 else 0)  # prefer real airports over tiny strips when close
        if d <= maxnm and (best is None or score < best[0]): best = (score, d, a)
    if not best: return None
    a = best[2]
    return {'code': a[0], 'iata': a[1], 'name': a[2], 'city': a[3], 'region': a[4], 'country': a[5], 'distNm': round(best[1], 1)}

# ---- FAA registry hex lookup for US N-numbers added without hex ----
def is_us_n(tail):
    t = (tail or '').upper().strip()
    return bool(re.match(r'^N[0-9]{1,5}[A-Z]{0,2}$', t))

def faa_hex(tail):
    n = tail.upper().strip().lstrip('N')
    url = 'https://registry.faa.gov/AircraftInquiry/Search/NNumberResult?nNumberTxt=' + n
    html = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20).read().decode('utf-8', 'replace')
    m = re.search(r'Mode S Code \(Base 16 / Hex\)</td>\s*<td[^>]*>\s*([0-9A-Fa-f]{6})', html)
    return m.group(1).lower() if m else None

def load_fleet():
    global fleet, fleet_mtime
    mt = os.path.getmtime(FLEET)
    if mt == fleet_mtime: return
    f = json.load(open(FLEET)); changed = False
    for t in f['tails']:
        t['tail'] = t['tail'].upper().strip()
        if not t.get('hex') and is_us_n(t['tail']):
            try:
                h = faa_hex(t['tail'])
                if h: t['hex'] = h; t.setdefault('source', 'FAA registry (auto)'); changed = True; print('hex', t['tail'], h, flush=True)
            except Exception as e: print('FAA lookup failed', t['tail'], e, flush=True)
            time.sleep(1)
        if t.get('hex'): t['hex'] = t['hex'].lower()
    if changed:
        json.dump(f, open(FLEET, 'w'), indent=1)
    fleet = f; fleet_mtime = os.path.getmtime(FLEET)

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    r = urllib.request.urlopen(req, timeout=20); b = r.read()
    if r.headers.get('Content-Encoding') == 'gzip': b = gzip.decompress(b)
    return json.loads(b)

def fetch_list(tpl, keys):
    out = []
    if not keys: return out
    for i in range(0, len(keys), 100):
        j = get_json(tpl.format(','.join(keys[i:i+100])))
        out += j.get('ac') or []
        time.sleep(1.2)
    return out

BLOCKED_UNTIL = {}  # source name -> epoch; set after HTTP 429 so we stop hammering a rate-limited source

def fetch(hexes, regs):
    errs = []
    for name, hex_tpl, reg_tpl in SOURCES:
        if BLOCKED_UNTIL.get(name, 0) > time.time():
            errs.append(f'{name}: backing off after rate limit')
            continue
        try:
            out = fetch_list(hex_tpl, hexes)
            if reg_tpl:
                try:
                    out += fetch_list(reg_tpl, regs)
                except Exception as e:
                    if '429' in str(e): BLOCKED_UNTIL[name] = time.time() + 300
                    print(f'poll note {name} reg: {e}', flush=True)
            return name, out, None
        except Exception as e:
            errs.append(f'{name}: {e}')
            if '429' in str(e): BLOCKED_UNTIL[name] = time.time() + 300
            print(f'poll note {name}: {e}', flush=True)
    return None, [], '; '.join(errs)

def num(v): return v if isinstance(v, (int, float)) else None

def ident(a):
    return ((a.get('r') or '') + ' ' + (a.get('flight') or '')).upper().replace(' ', '')

def match_ac(a, by_hex, by_reg):
    h = (a.get('hex') or '').lower().lstrip('~')
    if h and h in by_hex: return by_hex[h]
    r = (a.get('r') or '').upper().replace(' ', '')
    if r and r in by_reg: return by_reg[r]
    f = (a.get('flight') or '').upper().replace(' ', '')
    if f and f in by_reg: return by_reg[f]
    return None

def save_state():
    tmp = STATE + '.tmp'
    json.dump(state, open(tmp, 'w'))
    os.replace(tmp, STATE)

def poll_once():
    load_fleet()
    by_hex = {t['hex']: t for t in fleet['tails'] if t.get('hex')}
    by_reg = {t['tail'].upper(): t for t in fleet['tails']}
    hexes = sorted(by_hex)
    regs = sorted(by_reg)
    src, acs, err = fetch(hexes, regs)
    now = time.time()
    with lock:
        state['lastPoll'] = now; state['lastSource'] = src; state['lastError'] = err
        if src is None:
            print(f'poll failed {err}', flush=True)
            save_state()
            return
        seen = set()
        for a in acs:
            if a.get('lat') is None: continue
            t = match_ac(a, by_hex, by_reg)
            if not t: continue
            h = t.get('hex') or (a.get('hex') or '').lower().lstrip('~')
            seen.add(t['tail'])
            ts = now - (num(a.get('seen_pos')) or 0)
            onground = a.get('alt_baro') == 'ground'
            alt = None if onground else num(a.get('alt_baro'))
            if alt is None and not onground: alt = num(a.get('alt_geom'))
            p = state['planes'].get(h) or {}
            heard_hex = (a.get('hex') or '').lower().lstrip('~')
            p.update({'hex': h, 'tail': t['tail'], 'lat': a['lat'], 'lon': a['lon'], 'ts': ts,
                      'onGround': onground, 'altFt': alt, 'gsKt': num(a.get('gs')), 'track': num(a.get('track')) or num(a.get('true_heading')),
                      'vsFpm': num(a.get('baro_rate')) or num(a.get('geom_rate')), 'callsign': (a.get('flight') or '').strip() or None,
                      'squawk': a.get('squawk'), 'type': a.get('t'), 'source': src,
                      'heardHex': heard_hex or None})
            low = onground or (alt is not None and alt < 4000)
            ap = (nearest_airport(a['lat'], a['lon'], 6, 1) if low else None) or nearest_airport(a['lat'], a['lon'], 60, 2)
            p['near'] = ap
            if low and ap and ap['distNm'] <= 6:
                if onground or (p.get('vsFpm') or 0) < 0: p['lastAirport'] = dict(ap, ts=ts)
            if onground and ap and ap['distNm'] <= 3: p['groundAirport'] = dict(ap, ts=ts)
            # departure tracking
            if not onground and p.get('_wasGround') and p.get('groundAirport'): p['from'] = p['groundAirport']
            p['_wasGround'] = onground
            tr = p.get('trail') or []
            if onground: tr = []  # reset trail when on ground
            else:
                if tr and ts - tr[-1][2] > 1800: tr = []  # new flight after a long gap
                if not tr or tr[-1][2] < ts - 20: tr.append([round(a['lat'], 4), round(a['lon'], 4), int(ts), alt])
                tr = tr[-TRAIL_MAX:]
            p['trail'] = tr
            state['planes'][h] = p
        print(f"poll {src} ac={len(acs)} matched={len(seen)} {','.join(sorted(seen)) or 'none'}", flush=True)
        if err: print(f'poll note {err}', flush=True)
        save_state()


# ---- history backfill from public ADS-B trace files (globe_history / live traces) ----
# The free Render plan sleeps and loses its disk, so a flight that happens while we are
# asleep is never seen by the live poller. On startup (and hourly) we pull each tail's
# recent trace file, rebuild its last flights and last-known position from that, and
# merge it into state unless the live poller already has something newer.
TRACE_HOST = os.environ.get('TRACE_HOST', 'https://globe.adsb.lol')
BACKFILL_DAYS = 4
BACKFILL_EVERY_S = 6 * 3600  # trace hosts are blocking Render/datacenter IPs (403), so keep this rare
backfill_info = {'lastRun': None, 'results': {}}

def trace_urls(hexid):
    hh = hexid[-2:]
    urls = [f'{TRACE_HOST}/data/traces/{hh}/trace_full_{hexid}.json',
            f'{TRACE_HOST}/data/traces/{hh}/trace_recent_{hexid}.json']
    today = time.gmtime()
    for d in range(1, BACKFILL_DAYS + 1):
        g = time.gmtime(time.time() - d * 86400)
        urls.append(f'{TRACE_HOST}/globe_history/{g.tm_year}/{g.tm_mon:02d}/{g.tm_mday:02d}/traces/{hh}/trace_full_{hexid}.json')
    return urls

def get_trace(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    r = urllib.request.urlopen(req, timeout=25); b = r.read()
    if r.headers.get('Content-Encoding') == 'gzip' or b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
    return json.loads(b)

def trace_points(j):
    """-> [(ts, lat, lon, alt|'ground'|None, gs, vs)] absolute epoch seconds."""
    base = j.get('timestamp') or 0
    out = []
    for p in j.get('trace') or []:
        try:
            if p[1] is None or p[2] is None: continue
            alt = p[3] if (p[3] == 'ground' or isinstance(p[3], (int, float))) else None
            out.append((base + p[0], p[1], p[2], alt, num(p[4]), num(p[7])))
        except Exception: continue
    return out

def flights_from_points(pts, final_ts=None):
    """Segment points into flights. Departure = ground->air, arrival = air->ground, or a signal
    loss while low and close to an airport (arrival 'inferred'). Returns (flights, last_point)."""
    pts = sorted(set(pts), key=lambda p: p[0])
    flights = []; cur = None; prev = None
    def ap_at(p, maxnm=8):
        return nearest_airport(p[1], p[2], maxnm, 1)
    def close(cur, endp, inferred):
        a = ap_at(endp)
        cur.update({'to': a and dict(code=a['code'], iata=a['iata'], name=a['name'], city=a['city']), 'arr': int(endp[0]), 'arrInferred': inferred})
        flights.append(cur)
    for p in pts:
        air = p[3] != 'ground' and p[3] is not None
        if prev is not None and cur is not None and p[0] - prev[0] > 1200:
            if prev[3] != 'ground' and (prev[3] or 0) < 5000 and ap_at(prev, 6): close(cur, prev, True)
            else: flights.append(dict(cur, to=None, arr=int(prev[0]), arrInferred=True, lost=True))
            cur = None
        if air and cur is None:
            a = ap_at(prev if (prev and prev[3] == 'ground') else p)
            cur = {'dep': int(p[0]), 'from': a and dict(code=a['code'], iata=a['iata'], name=a['name'], city=a['city'])}
        elif not air and cur is not None and p[3] == 'ground':
            close(cur, p, False); cur = None
        prev = p
    last = pts[-1] if pts else None
    if cur is not None and last is not None:
        age = (final_ts or time.time()) - last[0]
        if age > 600 and (last[3] or 0) < 5000 and ap_at(last, 6): close(cur, last, True)
        elif age > 600: flights.append(dict(cur, to=None, arr=int(last[0]), arrInferred=True, lost=True))
        else: flights.append(dict(cur, to=None, arr=None, inProgress=True))
    return flights, last

def apply_backfill(t, flights, last):
    h = t['hex']
    with lock:
        p = state['planes'].get(h) or {}
        p['flights'] = flights[-8:]
        if last and (not p.get('ts') or last[0] > p['ts'] + 1):
            ts, lat, lon, alt = last[0], last[1], last[2], last[3]
            onground = alt == 'ground'
            altft = None if onground or alt is None else alt
            p.update({'hex': h, 'tail': t['tail'], 'lat': lat, 'lon': lon, 'ts': ts, 'onGround': onground, 'altFt': altft,
                      'gsKt': last[4], 'track': None, 'vsFpm': last[5], 'callsign': t['tail'], 'source': 'trace-backfill',
                      'trail': []})
            ap = nearest_airport(lat, lon, 6, 1) or nearest_airport(lat, lon, 60, 2)
            p['near'] = ap
            lf = flights[-1] if flights else None
            if onground and ap and ap['distNm'] <= 3:
                p['groundAirport'] = dict(ap, ts=ts); p['lastAirport'] = dict(ap, ts=ts)
            elif lf and lf.get('arrInferred') and not lf.get('lost') and ap and ap['distNm'] <= 6:
                p['lastAirport'] = dict(ap, ts=ts)  # probable arrival: signal lost on final approach
            if lf and lf.get('from') and lf.get('dep'):
                a0 = lf['from']; p['from'] = dict(a0, distNm=0.0, ts=lf['dep'], region='', country='')
        state['planes'][h] = p
        save_state()

def backfill_once():
    load_fleet()
    res = {}
    for t in fleet['tails']:
        h = t.get('hex')
        if not h: continue
        pts = []; ok = []; errs = []
        for u in trace_urls(h):
            try:
                pts += trace_points(get_trace(u)); ok.append(u.rsplit('/', 4)[-4] if 'globe_history' in u else u.rsplit('/', 1)[-1].split('_')[1])
            except Exception as e:
                errs.append(str(e)[:60])
                if '403' in str(e): break  # host blocks this network; don't hammer it
            time.sleep(0.6)
        fl, last = flights_from_points(pts)
        res[t['tail']] = {'files': ok, 'errors': errs[:3], 'points': len(pts), 'flights': len(fl),
                          'last': last and [int(last[0]), last[1], last[2], last[3]]}
        if pts: apply_backfill(t, fl, last)
        print(f"backfill {t['tail']} points={len(pts)} flights={len(fl)} ok={len(ok)} err={errs[:2]}", flush=True)
    backfill_info['lastRun'] = time.time(); backfill_info['results'] = res

def backfiller():
    time.sleep(3)
    while True:
        try: backfill_once()
        except Exception as e: print('backfill error', e, flush=True)
        time.sleep(BACKFILL_EVERY_S)


def ingest(payload):
    """Merge plane records pushed by the box-side sync (authenticated). Newer observation wins;
    flights lists are unioned by departure time so neither side loses history."""
    planes = payload.get('planes') or {}
    n = 0
    with lock:
        for h, inc in planes.items():
            if not isinstance(inc, dict) or not isinstance(inc.get('lat'), (int, float)) or not isinstance(inc.get('ts'), (int, float)): continue
            h = str(h).lower()
            if not re.match(r'^[0-9a-f]{6}$', h): continue
            cur = state['planes'].get(h) or {}
            fl = {}
            for f in (cur.get('flights') or []) + (inc.get('flights') or []):  # incoming wins on same departure
                if isinstance(f, dict) and f.get('dep'): fl[int(f['dep'])] = f
            merged_flights = [fl[k] for k in sorted(fl)][-8:]
            if not cur.get('ts') or inc['ts'] >= cur['ts'] - 1:
                keep = {k: v for k, v in cur.items() if k.startswith('_')}
                cur = dict(inc); cur.update(keep); cur['hex'] = h
                cur['source'] = inc.get('source') or 'box-sync'
            cur['flights'] = merged_flights
            state['planes'][h] = cur; n += 1
        ingest_info.update({'lastAt': time.time(), 'count': n, 'source': payload.get('source') or 'box-sync', 'syncAt': payload.get('syncAt')})
        save_state()
    return n

def poller():
    while True:
        try: poll_once()
        except Exception as e:
            print('poll error', e, flush=True)
            with lock:
                state['lastError'] = str(e)
                state['lastPoll'] = time.time()
                save_state()
        time.sleep(POLL_S)

class H(SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory=PUB, **k)
    def log_message(s, *a): pass
    def send_json(s, obj):
        b = json.dumps(obj, separators=(',', ':')).encode()
        s.send_response(200); s.send_header('Content-Type', 'application/json'); s.send_header('Cache-Control', 'no-store')
        s.send_header('Access-Control-Allow-Origin', '*'); s.send_header('Content-Length', str(len(b))); s.end_headers(); s.wfile.write(b)
    def end_headers(s):
        if not s.path.startswith('/api/'): s.send_header('Cache-Control', 'no-cache')
        super().end_headers()
    def do_POST(s):
        path = s.path.split('?')[0]
        if path != '/api/ingest': s.send_response(404); s.end_headers(); return
        import hmac
        sent = s.headers.get('X-Ingest-Secret', '')
        if not INGEST_SECRET or not hmac.compare_digest(sent.encode(), INGEST_SECRET.encode()):
            s.send_response(401); s.send_header('Content-Length', '0'); s.end_headers(); return
        try:
            ln = int(s.headers.get('Content-Length') or 0)
            if ln <= 0 or ln > 2_000_000: raise ValueError('bad length')
            payload = json.loads(s.rfile.read(ln))
            n = ingest(payload)
        except Exception as e:
            b = json.dumps({'ok': False, 'error': str(e)[:100]}).encode()
            s.send_response(400); s.send_header('Content-Type', 'application/json'); s.send_header('Content-Length', str(len(b))); s.end_headers(); s.wfile.write(b); return
        return s.send_json({'ok': True, 'planes': n})

    def do_GET(s):
        path = s.path.split('?')[0]
        if path == '/api/fleet':
            return s.send_json({'tails': fleet['tails']})
        if path in ('/api/state', '/api/health'):
            with lock:
                planes = {}
                for h, p in state['planes'].items():
                    planes[p['tail']] = {k: v for k, v in p.items() if not k.startswith('_')}
                return s.send_json({'now': time.time(), 'pollSeconds': POLL_S, 'lastPoll': state['lastPoll'],
                                    'source': state['lastSource'], 'error': state['lastError'], 'backfill': backfill_info, 'ingest': ingest_info, 'planes': planes})
        return super().do_GET()

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', '8135'))
    load_fleet()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=backfiller, daemon=True).start()
    print(f'HA Tracker on http://0.0.0.0:{port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
