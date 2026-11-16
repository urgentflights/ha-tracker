/* HA Tracker – Hatzolah Air. Live positions from /api/fleet and /api/state. */
(() => {
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LS = k => 'hat.' + k;
const Countries = (() => { try { return new Intl.DisplayNames(['en'], {type: 'region'}); } catch { return null; } })();

const PHASES = {
  parked:  {label: 'Parked',        kind: 'ground'},
  taxi:    {label: 'Taxiing',       kind: 'move'},
  roll:    {label: 'Taking off',    kind: 'move'},
  landed:  {label: 'Just landed',   kind: 'move'},
  takeoff: {label: 'Just took off', kind: 'air'},
  climb:   {label: 'Climbing',      kind: 'air'},
  cruise:  {label: 'Cruising',      kind: 'air'},
  descent: {label: 'Descending',    kind: 'air'},
  landing: {label: 'Landing',       kind: 'air'},
  hover:   {label: 'Hovering',      kind: 'air'},
  range:   {label: 'Out of range',  kind: 'air'},
  probable:{label: 'Landed',        kind: 'ground'},
  lost:    {label: 'No signal',     kind: 'lost'},
  unknown: {label: 'Live',          kind: 'lost'},
  none:    {label: 'No data',       kind: 'lost'}
};

function cityLine(p) {
  if (!p) return null;
  if (!p.country && p.city) return p.city;
  if (!p.country) return null;
  const country = {US: 'USA', GB: 'UK', AE: 'UAE'}[p.country] || Countries?.of(p.country) || p.country;
  const state = ['US', 'CA', 'AU'].includes(p.country) ? p.region : null;
  return [p.city, state, country].filter(Boolean).join(', ');
}
function airportCode(p) {
  if (!p) return '';
  const c = p.iata || p.code || '';
  return c && !/-\d/.test(c) ? c : '';
}
function placeShort(p) {
  if (!p) return null;
  const name = p.city || p.name;
  const code = airportCode(p);
  return name ? name + (code ? ' (' + code + ')' : '') : (code || null);
}
function asPlace(p) {
  if (!p || p.lat == null && p.code == null && !p.name) return null;
  return {
    name: p.name, city: p.city, region: p.region, country: p.country,
    code: airportCode(p), iata: p.iata, distNm: p.distNm,
    lat: p.lat, lon: p.lon
  };
}

function phaseOf(o, heli) {
  const alt = o.altFt, vs = o.vsFpm, gs = o.gsKt, has = Number.isFinite;
  if (o.onGround === true) {
    if (!(gs >= 3)) return 'parked';
    if (gs < 40) return 'taxi';
    return 'roll';
  }
  if (o.onGround !== false) return 'unknown';
  if (heli && has(gs) && gs < 25 && has(alt) && alt < 3000) return 'hover';
  if (has(vs) && vs >= 300 && has(alt) && alt < 5000) return 'takeoff';
  if (has(vs) && vs <= -300 && has(alt) && alt < 5000) return 'landing';
  if (has(vs) && vs >= 500) return 'climb';
  if (has(vs) && vs <= -500) return 'descent';
  return 'cruise';
}

const num = n => Math.round(n).toLocaleString('en-US');
function ago(t) {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + ' hr ago' : Math.round(s / 86400) + (Math.round(s / 86400) === 1 ? ' day ago' : ' days ago');
}

let fleet = {tails: []}, st = {planes: {}}, liveState = 'off', liveFailed = false, liveBusy = false;
const FLEET_ORDER = ['N18H','N248HA','N512HA','N613HA','4X-BHS','4X-BEX','4X-BOB','4X-BHT'];
const fleetIdx = tail => { const i = FLEET_ORDER.indexOf(tail); return i < 0 ? 1000 + String(tail).charCodeAt(0) : i; };
let openId = null, query = '', sortBy = 'fleet';
const offRegions = new Set(JSON.parse(localStorage.getItem(LS('off')) || '[]'));
try { const v = localStorage.getItem(LS('sort')); if (v) sortBy = v; } catch {}

function isHeli(t) { return t.category === 'helicopter' || /helicopter|rotorcraft/i.test(t.model || ''); }

function enrich(t) {
  const p = st.planes[t.tail], now = Date.now();
  const heli = isHeli(t);
  const base = {
    id: t.tail, tail: t.tail, name: t.tail, short: t.tail, model: t.model, year: t.year,
    owner: t.owner, region: t.region || '', faaModel: t.faaModel, source: t.source,
    note: t.note, color: t.color, category: t.category, heli, confidence: t.confidence
  };
  if (!p || !Number.isFinite(p.lat) || !Number.isFinite(p.lon) || !Number.isFinite(p.ts)) {
    return {...base, phase: 'none', phaseLabel: PHASES.none.label, kind: 'lost', live: false, lastAt: 0};
  }
  const observedAt = p.ts * 1000;
  const age = now - observedAt;
  const live = age <= 150000 && (p.onGround === true ? age <= 600000 : true) || (p.onGround === false && age <= 150000);
  const remembered = !live && age <= 14 * 86400e3;
  const ground = asPlace(p.groundAirport);
  const lastAp = asPlace(p.lastAirport);
  const near = asPlace(p.near);
  const from = asPlace(p.from);
  const trail = Array.isArray(p.trail) ? p.trail.filter(x => x && x.length >= 2).map(x => [x[1], x[0]]) : [];
  const obs = {onGround: p.onGround, altFt: p.altFt, vsFpm: p.vsFpm, gsKt: p.gsKt};

  let phase, place, ahead = null, trailOpen = false;
  if (live) {
    phase = phaseOf(obs, heli);
    if (p.onGround === true) place = (ground && Math.abs((ground.ts || p.ts) - p.ts) < 3600) ? ground : near;
    else {
      place = near;
    }
    trailOpen = p.onGround === false && trail.length > 1;
  } else if (remembered) {
    if (p.onGround === true) {
      phase = age < 600000 ? (phaseOf(obs, heli) === 'parked' ? 'parked' : phaseOf(obs, heli)) : 'parked';
      place = ground || near;
    } else {
      const high = (Number.isFinite(p.altFt) ? p.altFt >= 8000 : (p.gsKt || 0) >= 250);
      const probable = (!!lastAp && Number.isFinite(p.altFt) && p.altFt < 1000) || (!high && lastAp);
      phase = high ? 'range' : probable ? 'probable' : 'lost';
      place = lastAp || near;
    }
  } else {
    return {...base, phase: 'none', phaseLabel: PHASES.none.label, kind: 'lost', live: false, lastAt: observedAt};
  }

  const ph = PHASES[phase] || PHASES.none;
  const city = cityLine(place || near);
  return {
    ...base, live, remembered, phase, phaseLabel: ph.label, kind: ph.kind,
    latitude: p.lat, longitude: p.lon, observedAt, lastAt: observedAt,
    track: p.track ?? null, speedKts: p.gsKt ?? null, altitudeFt: p.altFt ?? null, verticalFpm: p.vsFpm ?? null,
    callsign: p.callsign || null, place, near, from, cityLine: city,
    trail, trailOpen, ahead
  };
}

function headline(a) {
  const p = a.place, near = a.near;
  switch (a.phase) {
    case 'parked': case 'taxi': case 'roll': case 'landed':
      return {where: p ? p.name : a.cityLine || 'On the ground', sub: p ? [airportCode(p), cityLine(p)].filter(Boolean).join(' · ') : ''};
    case 'probable':
      if (p) return {where: p.name, sub: [airportCode(p), cityLine(p)].filter(Boolean).join(' · ')};
      return {where: 'Near ' + (a.cityLine || 'its last position'), sub: ''};
    case 'range':
      return {where: near ? 'Last seen near ' + cityLine(near) : 'Over open water', sub: 'Out of radar range — normal over oceans or sparse coverage'};
    case 'lost':
      return {where: near ? 'Signal lost near ' + cityLine(near) : 'Signal lost', sub: ''};
    case 'none':
      return {where: liveState === 'error' ? 'Live data unavailable' : 'Not seen yet', sub: liveState === 'error' ? 'Retrying automatically' : 'We’ll show it the moment it flies'};
    default:
      return {where: near ? ((near.distNm > 25 ? 'Over ' : 'Near ') + cityLine(near)) : 'Over open water', sub: ''};
  }
}

function flyingMeta(a) {
  const bits = [];
  if (Number.isFinite(a.altitudeFt)) bits.push(num(a.altitudeFt) + ' ft');
  if (Number.isFinite(a.speedKts)) bits.push(num(a.speedKts) + ' kts');
  if (Number.isFinite(a.verticalFpm) && Math.abs(a.verticalFpm) >= 300) bits.push((a.verticalFpm > 0 ? '↑ ' : '↓ ') + num(Math.abs(a.verticalFpm)) + ' ft/min');
  return bits.join(' · ');
}

function detailLine(a) {
  if (a.kind === 'air' && a.live) return 'Live · updated ' + ago(a.observedAt);
  if (a.phase === 'range') return `Last seen ${ago(a.observedAt)}${Number.isFinite(a.altitudeFt) ? ' at ' + num(a.altitudeFt) + ' ft' : ''}`;
  if (a.phase === 'taxi' && Number.isFinite(a.speedKts)) return `Taxiing at ${num(a.speedKts)} kts`;
  if (a.phase === 'roll' && Number.isFinite(a.speedKts)) return `Speeding down the runway · ${num(a.speedKts)} kts`;
  if (a.remembered) return 'Seen ' + ago(a.observedAt);
  return '';
}

function routeBar(a) {
  if (a.kind !== 'air' && a.phase !== 'range') return '';
  const from = a.from, to = a.ahead, start = a.trail?.[0];
  let pct = 60;
  if (to && start && Number.isFinite(to.distNm)) {
    const d1 = Math.hypot(a.latitude - start[1], (a.longitude - start[0]) * Math.cos(a.latitude * Math.PI / 180)) * 60;
    pct = Math.max(8, Math.min(92, 100 * d1 / (d1 + to.distNm)));
  }
  const code = w => w ? esc(airportCode(w) || w.city || w.name || '?') : '?';
  if (!from && !to) return '';
  const plane = window.HAMap?.icons?.[a.heli ? 'heli' : 'plane'] || '✈';
  return `<span class="route" aria-label="Route"><b>${from ? code(from) : '?'}</b><span class="rt"><span class="rt-done" style="width:${pct}%"></span><span class="rt-plane" style="left:${pct}%">${plane}</span></span><b class="${to ? '' : 'q'}">${to ? code(to) : '?'}</b></span>`;
}

function details(a) {
  const model = a.model || a.faaModel || 'Aircraft';
  return `<div class="more">
    <h3 class="sec">Aircraft</h3>
    <div class="facts">
      <div><small>Type</small><b>${esc(model)}${a.heli ? ' · Helicopter' : ''}</b></div>
      <div><small>Year</small><b>${esc(a.year || '—')}</b></div>
      <div><small>Owner</small><b>${esc(a.owner || '—')}</b></div>
      <div><small>Based</small><b>${esc(a.region || '—')}</b></div>
    </div>
    ${a.note ? `<p class="note">${esc(a.note)}</p>` : ''}
    ${a.source ? `<p class="fine">${esc(a.source)}</p>` : ''}
    <div class="links"><button class="share-btn" data-share="${esc(a.id)}" type="button">Share ↗</button></div>
  </div>`;
}

function summary(list) {
  const air = list.filter(a => a.kind === 'air').length, move = list.filter(a => a.kind === 'move').length;
  const chips = [];
  if (air) chips.push(`<span class="chip air"><i></i>${air} in the air</span>`);
  if (move) chips.push(`<span class="chip move"><i></i>${move} on the runway</span>`);
  const heli = list.filter(a => a.heli).length;
  if (heli) chips.push(`<span class="chip heli">${heli} helicopter${heli === 1 ? '' : 's'}</span>`);
  if (!air && !move) chips.push(`<span class="chip">${liveState === 'ok' || liveState === 'cached' ? 'All quiet' : 'Connecting…'}</span>`);
  chips.push(`<span class="chip">${list.length} tails</span>`);
  return chips.join('');
}

function regions() { return [...new Set(fleet.tails.map(t => t.region).filter(Boolean))]; }

function renderGroups() {
  const regs = regions();
  const box = $('#groups');
  if (regs.length < 2) { box.innerHTML = ''; box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = regs.map(r => {
    const n = fleet.tails.filter(t => t.region === r).length;
    const on = !offRegions.has(r);
    return `<label class="tg"><span><b>Show ${esc(r)}</b><small>${n} aircraft</small></span><input type="checkbox" role="switch" data-region="${esc(r)}" ${on ? 'checked' : ''}><i class="sw"></i></label>`;
  }).join('');
}

function visible() {
  const q = query.trim().toLowerCase();
  return fleet.tails.map(enrich)
    .filter(a => !offRegions.has(a.region || '') && (!q || [a.tail, a.model, a.faaModel, a.owner, a.region, a.cityLine, a.phaseLabel].join(' ').toLowerCase().includes(q)));
}

function render() {
  renderGroups();
  const list = visible();
  $('#summary').innerHTML = summary(list);
  const icons = window.HAMap?.icons || {};
  const shown = list.slice().sort((a, b) => {
    if (sortBy === 'fleet') return fleetIdx(a.tail) - fleetIdx(b.tail);
    if (sortBy === 'tail') return a.tail.localeCompare(b.tail);
    if (sortBy === 'seen') return (b.observedAt || 0) - (a.observedAt || 0) || fleetIdx(a.tail) - fleetIdx(b.tail);
    const rank = {air: 0, move: 1, ground: 2, lost: 3};
    return (rank[a.kind] ?? 4) - (rank[b.kind] ?? 4) || (b.observedAt || 0) - (a.observedAt || 0) || fleetIdx(a.tail) - fleetIdx(b.tail);
  });
  $('#sort').value = sortBy;
  $('#list').innerHTML = shown.length ? shown.map(a => {
    const h = headline(a);
    const model = (a.model || '') + (a.year ? ' · ' + a.year : '') + (a.heli ? '' : '');
    const modelLabel = a.heli ? (a.model ? a.model.replace(/ · Helicopter$/i, '') + ' helicopter' : 'Helicopter') : (a.model || 'Aircraft');
    const meta = a.kind === 'air' && a.live ? flyingMeta(a) : '';
    return `<li class="card k-${a.kind} ph-${a.phase}${a.id === openId ? ' open' : ''}">
    <button class="card-main" data-id="${esc(a.id)}" aria-expanded="${a.id === openId}">
      <span class="row1">
        <span class="ava${a.heli ? ' heli' : ''}" style="--trk:${Number.isFinite(a.track) && a.kind !== 'ground' ? a.track : 0}deg">${a.heli ? icons.heli || '' : icons.plane || ''}</span>
        <span class="who"><span class="nm">${esc(a.tail)}${a.heli ? '<span class="kind-tag">HELI</span>' : ''}</span><span class="sub">${esc(modelLabel)}${a.year ? ' · ' + esc(a.year) : ''}</span></span>
      </span>
      <span class="pillrow"><span class="pill"><i></i>${esc(a.phaseLabel)}</span></span>
      <span class="where">${esc(h.where)}</span>
      ${h.sub ? `<span class="meta">${esc(h.sub)}</span>` : ''}
      ${routeBar(a)}
      ${meta ? `<span class="meta stats">${esc(meta)}</span>` : ''}
      ${detailLine(a) ? `<span class="meta">${esc(detailLine(a))}</span>` : ''}
    </button>${a.id === openId ? details(a) : ''}</li>`;
  }).join('') : `<li class="empty">${query ? 'No match.' : 'No aircraft to show.'}</li>`;
  $('#status').textContent = liveFailed && liveState === 'ok' ? 'Reconnecting… showing the last known positions.' : '';
  window.HAMap?.render(list, openId);
}

function toast(text) {
  const t = $('#toast'); t.textContent = text; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.hidden = true, 4000);
}
function dialog(title, html, cls = '') {
  const d = $('#dialog'); d.className = cls;
  $('#dialog-title').textContent = title; $('#dialog-body').innerHTML = html; d.showModal();
}

function open(id) {
  openId = openId === id ? null : id; render();
  if (!openId) return window.HAMap?.fit();
  window.HAMap?.select(openId);
  document.querySelector(`[data-id="${CSS.escape(openId)}"]`)?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

async function sharePlane(id) {
  const a = enrich(fleet.tails.find(t => t.tail === id) || {tail: id});
  const h = headline(a);
  const text = `${a.tail} — ${a.phaseLabel}: ${h.where}`;
  const url = location.origin + location.pathname;
  if (navigator.share) { try { await navigator.share({title: a.tail + ' · HA Tracker', text, url}); return; } catch { return; } }
  try { await navigator.clipboard.writeText(text + ' · ' + url); toast('Copied link.'); } catch { toast(text); }
}

function about() {
  const src = st.source ? ` Live data is currently coming from ${st.source}.` : '';
  const poll = st.pollSeconds ? ` The server checks the feed every ${st.pollSeconds} seconds.` : '';
  dialog('About the data', `<p>HA Tracker follows Hatzolah Air aircraft on a live map. Positions come from public, unfiltered ADS-B receiver networks.${poll} This page refreshes every 20 seconds.${src}</p>
    <p>US type, year and owner are from the FAA aircraft registry. Israeli helicopters are identified from the civil register and photographs in Hatzolah Air titles.</p>
    <p>A position is only a transponder report — it does not say who is on board or why the aircraft is flying. Planes parked or flying outside receiver coverage show their last known spot, or “Not seen yet” if we do not yet have a position for them.</p>
    <p class="fine">Times are shown in your time zone (${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}).</p>`);
}

// ---------- Map visibility + install ----------
let mapHidden = false;
try { mapHidden = localStorage.getItem(LS('map')) === 'hidden'; } catch {}
function setMap(hidden) {
  mapHidden = hidden; $('.map-box').hidden = hidden;
  $('#map-toggle').textContent = hidden ? '🗺️ Show map' : '🗺️ Hide map';
  $('#map-toggle').setAttribute('aria-expanded', String(!hidden));
  $('#map-btn').setAttribute('aria-pressed', String(!hidden));
  try { localStorage.setItem(LS('map'), hidden ? 'hidden' : 'shown'); } catch {}
  if (!hidden) requestAnimationFrame(() => window.HAMap && document.querySelector('#map') && window.dispatchEvent(new Event('resize')));
}
$('#map-toggle').addEventListener('click', () => setMap(!mapHidden));
$('#map-btn').addEventListener('click', () => { setMap(!mapHidden); if (!mapHidden) window.scrollTo({top: 0, behavior: 'smooth'}); toast(mapHidden ? 'Map hidden' : 'Map shown'); });
setMap(mapHidden);

const standalone = () => window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
const iOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
let installed = standalone();
function cta() {
  let hidden = false; try { hidden = (+localStorage.getItem(LS('cta-hide')) || 0) > Date.now(); } catch {}
  const box = $('#cta'), app = standalone();
  const inst = $('#menu [data-action="install"]'); if (inst) inst.hidden = app;
  const offerInstall = !app && !installed;
  if (hidden || !offerInstall) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<span class="cta-ic">📲</span><span class="cta-tx"><b>Get the app</b><small>Add HA Tracker to your home screen</small></span><button class="cta-go" data-cta="install" type="button">Install</button><button class="cta-x" data-cta="hide" type="button" aria-label="Not now">×</button>`;
}
function iphoneSteps() {
  dialog('Add to Home Screen', `<ol class="big-steps">
    <li><span>Tap the <b class="kbd">Share</b> button in Safari</span></li>
    <li><span>Choose <b>Add to Home Screen</b></span></li>
    <li><span>Open <b>HA Tracker</b> from your Home Screen</span></li>
  </ol>`);
}
const waitForInstall = ms => window.haInstall ? Promise.resolve(window.haInstall)
  : new Promise(r => { const t = setTimeout(() => r(window.haInstall || null), ms); addEventListener('ha-install', () => { clearTimeout(t); r(window.haInstall); }, {once: true}); });
async function ctaAction(what) {
  if (what === 'hide') { try { localStorage.setItem(LS('cta-hide'), String(Date.now() + 7 * 86400e3)); } catch {} return cta(); }
  if (iOS) return iphoneSteps();
  const go = $('#cta .cta-go'); if (go) go.textContent = 'One sec…';
  const evt = await waitForInstall(2500);
  if (evt) {
    window.haInstall = null;
    evt.prompt();
    const {outcome} = await evt.userChoice.catch(() => ({}));
    if (outcome === 'accepted') { installed = true; toast('Installing — HA Tracker will be on your home screen.'); }
    return cta();
  }
  cta();
  if (installed) return toast('Already installed — open HA Tracker from your home screen.');
  dialog('Install the app', `<div class="menu-hint"><span>Tap</span><b class="kbd big">⋮</b><span>at the top-right of Chrome:</span></div>
    <ul class="steps"><li>See <b>📲 Install app</b>? Tap it — done.</li>
    <li>See <b>Open app</b> instead? It’s already installed — look for <b>HA Tracker</b> on your home screen or app drawer.</li></ul>`);
}
addEventListener('ha-install', cta);
addEventListener('appinstalled', () => { installed = true; window.haInstall = null; cta(); });

document.addEventListener('click', e => {
  const t = e.target;
  const ctaBtn = t.closest('[data-cta]'); if (ctaBtn) return ctaAction(ctaBtn.dataset.cta);
  const share = t.closest('[data-share]'); if (share) return sharePlane(share.dataset.share);
  const card = t.closest('[data-id]'); if (card) return open(card.dataset.id);
  if (t.closest('#menu-btn')) { const m = $('#menu'); m.hidden = !m.hidden; $('#menu-btn').setAttribute('aria-expanded', String(!m.hidden)); return; }
  const act = t.closest('[data-action]');
  if (!t.closest('#menu')) $('#menu').hidden = true;
  if (act) { $('#menu').hidden = true; ({install: () => ctaAction('install'), about})[act.dataset.action]?.(); }
});
$('#groups').addEventListener('change', e => {
  const inp = e.target.closest('[data-region]'); if (!inp) return;
  if (inp.checked) offRegions.delete(inp.dataset.region); else offRegions.add(inp.dataset.region);
  try { localStorage.setItem(LS('off'), JSON.stringify([...offRegions])); } catch {}
  render();
});
$('#sort').addEventListener('change', e => { sortBy = e.target.value; try { localStorage.setItem(LS('sort'), sortBy); } catch {} render(); });
$('#search').addEventListener('input', e => { query = e.target.value; render(); });

async function refreshLive() {
  if (document.hidden || liveBusy) return;
  liveBusy = true;
  try {
    const r = await fetch('api/state', {cache: 'no-store', signal: AbortSignal.timeout(15000)});
    if (!r.ok) throw new Error();
    st = await r.json();
    liveState = 'ok'; liveFailed = false;
  } catch { liveFailed = true; if (liveState === 'off') liveState = 'error'; }
  finally { liveBusy = false; render(); }
}

async function boot() {
  for (let i = 0; ; i++) {
    try { fleet = await (await fetch('api/fleet', {cache: 'no-store'})).json(); break; }
    catch (e) { if (i > 30) throw e; await new Promise(r => setTimeout(r, 3000)); }
  }
  window.HAMap?.init(id => open(id));
  cta(); render(); await refreshLive();
  setInterval(refreshLive, 20000);
  setInterval(() => { if (!document.hidden) render(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLive(); });
  setInterval(async () => { try { fleet = await (await fetch('api/fleet', {cache: 'no-store'})).json(); } catch {} }, 300000);
}
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
boot();
})();
