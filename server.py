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
    ('adsb.lol',
     'https://api.adsb.lol/v2/hex/{}',
     'https://api.adsb.lol/v2/reg/{}'),
    ('adsb.fi',
     'https://opendata.adsb.fi/api/v2/hex/{}',
     'https://opendata.adsb.fi/api/v2/reg/{}'),
]
TRAIL_MAX = 400

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

def fetch(hexes, regs):
    errs = []
    for name, hex_tpl, reg_tpl in SOURCES:
        try:
            out = fetch_list(hex_tpl, hexes)
            try:
                out += fetch_list(reg_tpl, regs)
            except Exception as e:
                errs.append(f'{name} reg: {e}')
            return name, out, ('; '.join(errs) or None)
        except Exception as e:
            errs.append(f'{name}: {e}')
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
                                    'source': state['lastSource'], 'error': state['lastError'], 'planes': planes})
        return super().do_GET()

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', '8135'))
    load_fleet()
    threading.Thread(target=poller, daemon=True).start()
    print(f'HA Tracker on http://0.0.0.0:{port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
