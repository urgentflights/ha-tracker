#!/usr/bin/env python3
"""HA Tracker box-side sync.

Runs on the always-on box (whose IP is not rate-limited the way Render's is). Every
SYNC_SECONDS it polls adsb.lol (hex + registration) with adsb.fi as fallback, keeps
last-known positions and a short flight history, pushes the result to the live site
(POST /api/ingest, shared secret) and, when something changed, commits the snapshot
(data/state.json) to the repo at most every COMMIT_MIN_S seconds.

NOTE: adsb.lol's *trace/globe_history* hosts block this box's network (403), so history
is rebuilt from our own polling, not from trace files. Flights shorter than the poll
interval, or outside ADS-B coverage, can still be missed.
"""
import fcntl, gzip, hashlib, json, os, signal, socket, subprocess, sys, threading, time, urllib.request, datetime

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault('HA_NO_AUTOSTART', '1')
import server as S  # reuse airport lookup + helpers (does not start threads on import)

SYNC_S = int(os.environ.get('SYNC_SECONDS', '180'))
COMMIT_MIN_S = int(os.environ.get('COMMIT_MIN_S', '1800'))
SITE = os.environ.get('SITE_URL', 'https://ha-tracker.onrender.com').rstrip('/')
UA = 'HATracker-box-sync/1.0 (small private fleet tracker; 1 poll / %ds)' % SYNC_S
LOCK = os.path.join(HERE, 'sync.lock'); PIDF = os.path.join(HERE, 'sync.pid')
LOG = os.path.join(HERE, 'sync.log'); OK = os.path.join(HERE, 'last_success')
STATEF = os.path.join(HERE, 'sync_state.json'); SECRETF = os.path.join(HERE, '.secret')
SNAP = os.path.join(ROOT, 'data', 'state.json'); FLEETF = os.path.join(ROOT, 'data', 'fleet.json')
IL = datetime.timezone(datetime.timedelta(hours=2))
LOW_FT = 5000
HEARTBEAT = os.path.join(HERE, 'heartbeat')
CYCLE_DEADLINE_S = 240   # a cycle (fetch+push) taking longer than this is treated as hung -> process exits, wrapper restarts it
socket.setdefaulttimeout(30)
cycle_started = [0.0]

def log(*a):
    line = datetime.datetime.now(IL).strftime('%Y-%m-%d %H:%M:%S') + ' ' + ' '.join(str(x) for x in a)
    print(line, flush=True)

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    r = urllib.request.urlopen(req, timeout=25); b = r.read()
    if r.headers.get('Content-Encoding') == 'gzip' or b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
    return json.loads(b)

def secret():
    v = os.environ.get('INGEST_SECRET')
    if v: return v.strip()
    return open(SECRETF).read().strip()

def load_fleet():
    f = json.load(open(FLEETF))
    return [dict(t, tail=t['tail'].upper(), hex=(t.get('hex') or '').lower()) for t in f['tails']]

# ---------- fetching ----------
def fetch(tails):
    hexes = sorted({t['hex'] for t in tails if t['hex']}); regs = sorted({t['tail'] for t in tails})
    notes = []
    try:
        out = get_json('https://api.adsb.lol/v2/hex/' + ','.join(hexes)).get('ac') or []
        time.sleep(1.5)
        try: out += get_json('https://api.adsb.lol/v2/reg/' + ','.join(regs)).get('ac') or []
        except Exception as e: notes.append(f'adsb.lol reg: {e}')
        return 'adsb.lol', out, notes
    except Exception as e:
        notes.append(f'adsb.lol: {e}')
    try:
        out = get_json('https://opendata.adsb.fi/api/v2/hex/' + ','.join(hexes)).get('ac') or []
        return 'adsb.fi', out, notes
    except Exception as e:
        notes.append(f'adsb.fi: {e}')
    return None, [], notes

# ---------- state ----------
def load_state():
    st = None
    for f in (STATEF, SNAP):
        try:
            st = json.load(open(f)); break
        except Exception: pass
    st = st or {}
    st.setdefault('planes', {}); st.setdefault('lastCommit', 0); st.setdefault('commitHash', '')
    return st

def save_state(st):
    tmp = STATEF + '.tmp'; json.dump(st, open(tmp, 'w')); os.replace(tmp, STATEF)

def apt(ap):
    return ap and {k: ap[k] for k in ('code', 'iata', 'name', 'city') if k in ap}

def near_text(lat, lon):
    ap = S.nearest_airport(lat, lon, 60, 1)
    return ap and (ap.get('city') or ap.get('name'))

def open_flight(p):
    fl = p.get('flights') or []
    return fl[-1] if fl and fl[-1].get('arr') is None else None

def observe(p, a, t, src, now):
    """Fold one ADS-B observation into plane record p (mirrors server.poll_once)."""
    ts = now - (S.num(a.get('seen_pos')) or 0)
    if p.get('ts') and ts < p['ts'] - 1: return False
    onground = a.get('alt_baro') == 'ground'
    alt = None if onground else S.num(a.get('alt_baro'))
    if alt is None and not onground: alt = S.num(a.get('alt_geom'))
    prev_ts, prev_ground = p.get('ts'), p.get('onGround')
    p.update({'hex': t['hex'] or (a.get('hex') or '').lower().lstrip('~'), 'tail': t['tail'], 'lat': a['lat'], 'lon': a['lon'], 'ts': ts,
              'onGround': onground, 'altFt': alt, 'gsKt': S.num(a.get('gs')), 'track': S.num(a.get('track')) or S.num(a.get('true_heading')),
              'vsFpm': S.num(a.get('baro_rate')) or S.num(a.get('geom_rate')), 'callsign': (a.get('flight') or '').strip() or None,
              'squawk': a.get('squawk'), 'type': a.get('t'), 'source': 'box-sync:' + src})
    low = onground or (alt is not None and alt < 4000)
    ap = (S.nearest_airport(a['lat'], a['lon'], 6, 1) if low else None) or S.nearest_airport(a['lat'], a['lon'], 60, 2)
    p['near'] = ap
    if low and ap and ap['distNm'] <= 6 and (onground or (p.get('vsFpm') or 0) < 0): p['lastAirport'] = dict(ap, ts=ts)
    if onground and ap and ap['distNm'] <= 3: p['groundAirport'] = dict(ap, ts=ts)
    fl = p.setdefault('flights', []); of = open_flight(p)
    if onground:
        p['trail'] = []
        if of:
            end = S.nearest_airport(a['lat'], a['lon'], 8, 1)
            of.update({'to': apt(end), 'arr': int(ts), 'arrInferred': False}); of.pop('lost', None); of.pop('inProgress', None)
            if not end: of['toNear'] = near_text(a['lat'], a['lon'])
    else:
        if not of:
            origin = None
            known = p.get('groundAirport') if (p.get('groundAirport') or {}).get('ts', 0) >= (p.get('lastAirport') or {}).get('ts', 0) else p.get('lastAirport')
            if low and ap and ap['distNm'] <= 6 and (alt or 0) < 3000: origin = ap       # seen right at the airport
            elif known and ts - known.get('ts', 0) < 86400: origin = known              # last airport it was known at
            of = {'dep': int(ts), 'from': apt(origin), 'inProgress': True}
            if not origin: of['fromNear'] = near_text(a['lat'], a['lon'])
            if not (prev_ground or (prev_ts and ts - prev_ts < 600)): of['depApprox'] = True
            fl.append(of); p['from'] = dict(origin, ts=int(ts)) if origin else None
            p['trail'] = []
        tr = p.get('trail') or []
        if tr and ts - tr[-1][2] > 1800: tr = []
        if not tr or tr[-1][2] < ts - 20: tr.append([round(a['lat'], 4), round(a['lon'], 4), int(ts), alt])
        p['trail'] = tr[-400:]
        of.pop('lost', None); of['inProgress'] = True
    p['flights'] = fl[-8:]
    return True

def expire(p, now):
    """Close an open flight when the signal has gone quiet."""
    of = open_flight(p)
    if not of or not p.get('ts'): return
    age = now - p['ts']
    if age < 900: return
    ap = S.nearest_airport(p['lat'], p['lon'], 6, 1)
    if not p.get('onGround') and (p.get('altFt') or 0) < LOW_FT and ap:
        of.update({'to': apt(ap), 'arr': int(p['ts']), 'arrInferred': True}); of.pop('inProgress', None)
        p['lastAirport'] = dict(ap, ts=p['ts'])
    elif age > 3600:
        of.update({'to': None, 'toNear': near_text(p['lat'], p['lon']), 'arr': int(p['ts']), 'arrInferred': True, 'lost': True}); of.pop('inProgress', None)

# ---------- push / commit ----------
def push(st, src, notes, tracked=()):
    planes = {}
    for h, p in st['planes'].items():
        if p.get('lat') is None: continue
        planes[h] = dict({k: v for k, v in p.items() if not k.startswith('_')}, checkedAt=int(time.time()))
    body = json.dumps({'planes': planes, 'source': 'box-sync', 'syncAt': time.time(), 'feed': src,
                       'tracked': list(tracked), 'heard': st.get('heard', {})}).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(SITE + '/api/ingest', data=body, method='POST',
                                         headers={'Content-Type': 'application/json', 'X-Ingest-Secret': secret(), 'User-Agent': UA})
            r = urllib.request.urlopen(req, timeout=90); j = json.loads(r.read())
            if j.get('ok'): return True, f"pushed {j.get('planes')} planes"
            last = str(j)
        except Exception as e:
            last = str(e)
        time.sleep(20)  # site may be waking from sleep
    return False, last

def snap_hash(st, major=False):
    d = {}
    for h, p in sorted(st['planes'].items()):
        d[h] = [p.get('onGround'), p.get('flights'), (p.get('lastAirport') or {}).get('code'), (p.get('groundAirport') or {}).get('code')]
        if not major: d[h] += [round(p.get('lat', 0), 3), round(p.get('lon', 0), 3)]
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()

def maybe_commit(st, now):
    """Commit the snapshot: soon (>=5 min) after flights/airport state change, else >=30 min after any position change."""
    h, hm = snap_hash(st), snap_hash(st, True)
    since = now - st.get('lastCommit', 0)
    if h == st.get('commitHash'): return
    if not ((hm != st.get('commitHashMajor') and since >= 300) or since >= COMMIT_MIN_S): return
    snap = {'planes': {k: {kk: vv for kk, vv in p.items() if kk != 'trail'} for k, p in st['planes'].items()},
            'lastPoll': None, 'lastSource': None, 'lastError': None}
    json.dump(snap, open(SNAP, 'w'))
    try:
        run = lambda *c: subprocess.run(c, cwd=ROOT, capture_output=True, text=True, timeout=120)
        run('git', 'add', 'data/state.json')
        c = run('git', 'commit', '-m', 'Box sync: update last-known positions and flights')
        if c.returncode != 0 and 'nothing to commit' in (c.stdout + c.stderr):
            st.update({'commitHash': h, 'commitHashMajor': hm, 'lastCommit': now}); return
        p = run('git', 'push', 'origin', 'HEAD')
        if p.returncode == 0: log('snapshot committed and pushed'); st.update({'commitHash': h, 'commitHashMajor': hm, 'lastCommit': now})
        else: log('snapshot push failed:', (p.stderr or '')[-200:].replace('\n', ' '))
    except Exception as e:
        log('snapshot commit error', e)

# ---------- main loop ----------
def cycle(st):
    tails = load_fleet(); now = time.time()
    by_hex = {t['hex']: t for t in tails if t['hex']}; by_reg = {t['tail']: t for t in tails}
    src, acs, notes = fetch(tails)
    seen = set(); heard = st.setdefault('heard', {})
    if src:
        for a in acs:
            if a.get('lat') is None:
                # transponder heard but no position (e.g. mode_s only): remember that it is alive
                h0 = (a.get('hex') or '').lower().lstrip('~'); t0 = by_hex.get(h0) or by_reg.get((a.get('r') or '').upper())
                if t0: heard[t0['tail']] = {'ts': int(now - (S.num(a.get('seen')) or 0)), 'hex': h0, 'type': a.get('type')}
                continue
            h = (a.get('hex') or '').lower().lstrip('~'); r = (a.get('r') or '').upper().replace(' ', '')
            fl = (a.get('flight') or '').upper().replace(' ', '')
            t = by_hex.get(h) or by_reg.get(r) or by_reg.get(fl)
            if not t: continue
            key = t['hex'] or h
            p = st['planes'].setdefault(key, {})
            if observe(p, a, t, src, now): seen.add(t['tail'])
    for p in st['planes'].values(): expire(p, now)
    log(f"fetch {src or 'FAILED'} tracking={len(tails)} ac={len(acs)} matched={','.join(sorted(seen)) or 'none'}" + (f" notes={'; '.join(notes)}" if notes else ''))
    ok, msg = push(st, src, notes, [t['tail'] for t in tails])
    if ok:
        open(OK, 'w').write(f"{int(time.time())} {datetime.datetime.now(IL).isoformat(timespec='seconds')}\n")
        log('push ok:', msg)
    else:
        log('push FAILED:', msg)
    save_state(st)
    if ok: maybe_commit(st, now); save_state(st)

def heartbeat_thread():
    """Touch the heartbeat file every 10 s (wall clock). The watchdog uses it to tell 'alive' from 'hung/suspended',
    and this thread also kills the process if a cycle hangs past CYCLE_DEADLINE_S."""
    while True:
        try:
            open(HEARTBEAT, 'w').write(str(int(time.time())))
            cs = cycle_started[0]
            if cs and time.time() - cs > CYCLE_DEADLINE_S:
                log(f'cycle hung >{CYCLE_DEADLINE_S}s; exiting so the wrapper restarts us'); os._exit(3)
        except Exception: pass
        time.sleep(10)

def on_signal(sig, frm):
    log(f'received signal {signal.Signals(sig).name}; exiting' if sig != signal.SIGHUP else 'received SIGHUP (ignored)')
    if sig != signal.SIGHUP: os._exit(0)

def main():
    lf = open(LOCK, 'w')
    try: fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print('another instance holds the lock'); sys.exit(0)
    open(PIDF, 'w').write(str(os.getpid()))
    for sg in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT): signal.signal(sg, on_signal)
    log(f'sync started pid={os.getpid()} interval={SYNC_S}s site={SITE}')
    threading.Thread(target=heartbeat_thread, daemon=True).start()
    st = load_state()
    due = 0.0; prev_cycle = time.time()
    while True:
        # sleep in short wall-clock slices so a resume after the box was suspended runs the next cycle at once
        if time.time() < due: time.sleep(5); continue
        gap = time.time() - prev_cycle
        if prev_cycle and gap > SYNC_S * 2.5: log(f'gap of {int(gap)}s since last cycle (box suspended or stalled?)')
        cycle_started[0] = time.time()
        try:
            cycle(st)
        except BaseException as e:
            log('cycle error', repr(e))
        cycle_started[0] = 0.0
        prev_cycle = time.time(); due = prev_cycle + SYNC_S
        try:
            if os.path.exists(LOG) and os.path.getsize(LOG) > 3_000_000: open(LOG, 'w').close()
        except Exception: pass

if __name__ == '__main__':
    main()
