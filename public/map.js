/* MapLibre + OpenFreeMap Positron. HTML markers point with heading and glide
   between updates. Current flight path is a solid line; a likely landing spot is dashed. */
(() => {
  const STYLE = 'https://tiles.openfreemap.org/styles/positron';
  const PLANE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 1.6c.9 0 1.5 1 1.5 2.2v5.3l8 4.8v2.2l-8-2.5v4.6l2.4 1.8v1.6L12 20.5l-3.9 1.1V20l2.4-1.8v-4.6l-8 2.5v-2.2l8-4.8V3.8c0-1.2.6-2.2 1.5-2.2z"/></svg>';
  const HELI = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 6.4c1.9 0 3.3 1.7 3.3 3.9 0 1.9-1 3.4-2.3 3.9v6.1h1.7v1.3H9.3v-1.3H11v-6.1c-1.3-.5-2.3-2-2.3-3.9 0-2.2 1.4-3.9 3.3-3.9z"/><g class="rotor"><rect x="1" y="9.7" width="22" height="1.3" rx=".65"/><rect x="11.35" y="-.8" width="1.3" height="22" rx=".65"/></g></svg>';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let map, ready = false, onSelect, selected = null, items = [], fittedKey = '', lastTrail = 0, loopOn = false;
  const marks = new Map();

  function init(select) {
    onSelect = select;
    const el = document.querySelector('#map');
    try {
      map = new maplibregl.Map({container: el, style: STYLE, center: [-30, 35], zoom: 1.4, minZoom: 1,
        attributionControl: {compact: true}, cooperativeGestures: matchMedia('(pointer:coarse)').matches,
        dragRotate: false, pitchWithRotate: false, touchPitch: false, fadeDuration: 150});
    } catch { el.innerHTML = '<p class="map-off">Map unavailable on this device.</p>'; return; }
    map.touchZoomRotate.disableRotation();
    map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
    map.on('load', () => {
      ready = true;
      for (const l of map.getStyle().layers) if (l.type === 'symbol' && map.getLayoutProperty(l.id, 'text-field'))
        map.setLayoutProperty(l.id, 'text-field', ['coalesce', ['get', 'name_en'], ['get', 'name:en'], ['get', 'name:latin'], ['get', 'name']]);
      map.addSource('trails', {type: 'geojson', data: empty()});
      map.addSource('ahead', {type: 'geojson', data: empty()});
      map.addLayer({id: 'trail-glow', type: 'line', source: 'trails', layout: {'line-cap': 'round', 'line-join': 'round'},
        paint: {'line-color': ['get', 'color'], 'line-width': 7, 'line-opacity': .14}});
      map.addLayer({id: 'trail', type: 'line', source: 'trails', layout: {'line-cap': 'round', 'line-join': 'round'},
        paint: {'line-color': ['get', 'color'], 'line-width': 2.6, 'line-opacity': .95}});
      map.addLayer({id: 'ahead', type: 'line', source: 'ahead', layout: {'line-cap': 'round'},
        paint: {'line-color': '#2563eb', 'line-width': 2, 'line-dasharray': [1.5, 2], 'line-opacity': .7}});
      draw(); fit();
    });
    map.on('moveend', declutter);
    new ResizeObserver(() => map.resize()).observe(el);
  }

  const RANK = {air: 0, move: 1, ground: 2, lost: 3};
  function declutter() {
    if (!ready) return;
    const boxes = [], hidden = new Set(), all = [...marks.values()];
    const list = all.filter(m => !m.k).sort((x, y) => (y.a.id === selected) - (x.a.id === selected) || (RANK[x.a.kind] ?? 3) - (RANK[y.a.kind] ?? 3));
    for (const m of list) {
      const p = map.project(m.cur), w = Math.max(...all.filter(o => o.lead === m.a.id).map(o => o.el.querySelector('.mk-label')?.offsetWidth || 90), 90) / 2 + 4;
      const box = [p.x - Math.max(w, 20), p.y - 20, p.x + Math.max(w, 20), p.y + 58 + ((m.n || 1) - 1) * 22];
      const hit = boxes.some(b => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]);
      if (hit) hidden.add(m.a.id); else boxes.push(box);
    }
    for (const m of all) m.el.classList.toggle('nolabel', hidden.has(m.lead));
  }

  const empty = () => ({type: 'FeatureCollection', features: []});
  const has = a => Number.isFinite(a.latitude) && Number.isFinite(a.longitude);
  const moving = a => (a.kind === 'air' && a.phase !== 'range') || a.kind === 'move';

  function target(a, now) {
    const p = [a.longitude, a.latitude];
    if (!a.live || !moving(a) || !Number.isFinite(a.speedKts) || !Number.isFinite(a.track)) return p;
    const s = Math.min(Math.max(0, now - a.observedAt), a.kind === 'move' ? 15e3 : 90e3) / 1000;
    const nm = a.speedKts * s / 3600, t = a.track * Math.PI / 180;
    const dLat = nm * Math.cos(t) / 60, dLon = nm * Math.sin(t) / (60 * Math.cos(a.latitude * Math.PI / 180));
    return [a.longitude + dLon, a.latitude + dLat];
  }

  function markerHtml(a, n = 1) {
    return (n > 1 ? `<span class="mk-count" aria-hidden="true">${n}</span>` : '') +
      `<div class="mk-icon" style="transform:rotate(${Number.isFinite(a.track) && a.kind !== 'ground' ? a.track : 0}deg)">${a.heli ? HELI : PLANE}</div>` +
      `<div class="mk-label">${esc(a.short || a.tail)}</div>`;
  }

  function draw() {
    if (!ready) return;
    const now = Date.now(), seen = new Set(), groups = {};
    const spotOf = a => a.latitude.toFixed(2) + ',' + a.longitude.toFixed(2);
    for (const a of items) if (has(a) && !moving(a)) (groups[spotOf(a)] ||= []).push(a);
    for (const g of Object.values(groups)) g.sort((x, y) => (y.lastAt || 0) - (x.lastAt || 0));
    for (const a of items) {
      if (!has(a)) continue;
      seen.add(a.id);
      const g = !moving(a) && groups[spotOf(a)], k = g ? g.indexOf(a) : 0, n = g ? g.length : 1;
      let m = marks.get(a.id);
      if (!m) {
        const el = document.createElement('button');
        el.type = 'button'; el.className = 'mk';
        el.addEventListener('click', e => { e.stopPropagation(); onSelect(a.id); });
        const marker = new maplibregl.Marker({element: el, anchor: 'center'}).setLngLat(target(a, now)).addTo(map);
        m = {marker, el, cur: target(a, now)}; marks.set(a.id, m);
      }
      m.a = a; m.k = k; m.n = n; m.lead = g ? g[0].id : a.id;
      m.el.className = `mk ph-${a.phase} k-${a.kind}${a.heli ? ' heli' : ''}${a.id === selected ? ' sel' : ''}${a.live ? ' live' : ''}${k > 0 ? ' grp-sub' : ''}`;
      m.el.setAttribute('aria-label', `${a.tail}: ${a.phaseLabel}`);
      m.el.style.setProperty('--stack', k);
      const html = markerHtml(a, k === 0 ? n : 1);
      if (m.html !== html) { m.el.innerHTML = html; m.html = html; }
      if (a.ahead && Number.isFinite(a.ahead.lat)) {
        if (!m.dest) { const d = document.createElement('div'); d.className = 'dest'; m.dest = new maplibregl.Marker({element: d, anchor: 'bottom'}).setLngLat([a.ahead.lon, a.ahead.lat]).addTo(map); }
        m.dest.setLngLat([a.ahead.lon, a.ahead.lat]); m.dest.getElement().textContent = a.ahead.code || a.ahead.city || a.ahead.name;
      } else if (m.dest) { m.dest.remove(); m.dest = null; }
    }
    for (const [id, m] of marks) if (!seen.has(id)) { m.marker.remove(); m.dest?.remove(); marks.delete(id); }
    trails(); declutter();
    const key = [...seen].sort().join();
    if (key !== fittedKey) { fittedKey = key; fit(); }
    if (!loopOn && [...marks.values()].some(m => moving(m.a))) { loopOn = true; requestAnimationFrame(tick); }
  }

  function tick() {
    const now = Date.now();
    let any = false;
    for (const m of marks.values()) {
      const t = target(m.a, now), dx = t[0] - m.cur[0], dy = t[1] - m.cur[1];
      if (Math.abs(dx) > 5 || Math.abs(dy) > 5) m.cur = t; else { m.cur = [m.cur[0] + dx * .12, m.cur[1] + dy * .12]; }
      m.marker.setLngLat(m.cur);
      if (moving(m.a)) any = true;
    }
    if (now - lastTrail > 1000) { lastTrail = now; trails(); }
    if (any && !document.hidden) requestAnimationFrame(tick); else loopOn = false;
  }

  function trails() {
    if (!ready) return;
    const lines = [], ahead = [];
    for (const m of marks.values()) {
      const a = m.a;
      if (a.trail?.length) {
        const coords = a.trailOpen ? [...a.trail, m.cur] : a.trail;
        if (coords.length > 1) lines.push({type: 'Feature', properties: {color: a.trailOpen ? '#2563eb' : '#64748b'}, geometry: {type: 'LineString', coordinates: coords}});
      }
      if (a.ahead && Number.isFinite(a.ahead.lat)) ahead.push({type: 'Feature', properties: {}, geometry: {type: 'LineString', coordinates: [m.cur, [a.ahead.lon, a.ahead.lat]]}});
    }
    map.getSource('trails')?.setData({type: 'FeatureCollection', features: lines});
    map.getSource('ahead')?.setData({type: 'FeatureCollection', features: ahead});
  }

  function bounds(points) {
    const b = new maplibregl.LngLatBounds();
    points.forEach(p => b.extend(p));
    return b;
  }

  function fit() {
    if (!ready) return;
    const pts = [...marks.values()].map(m => m.cur);
    if (pts.length === 1) map.easeTo({center: pts[0], zoom: 6, duration: 800});
    else if (pts.length) map.fitBounds(bounds(pts), {padding: {top: 50, bottom: 50, left: 50, right: 50}, maxZoom: 7, duration: 800});
  }

  function focus(id) {
    const m = marks.get(id);
    if (!m || !ready) return;
    const pts = m.a.trail?.length > 1 ? [...m.a.trail, m.cur] : [];
    if (m.a.ahead && Number.isFinite(m.a.ahead.lat)) pts.push(m.cur, [m.a.ahead.lon, m.a.ahead.lat]);
    if (pts.length > 1) map.fitBounds(bounds(pts), {padding: 60, maxZoom: 9, duration: 1200});
    else map.flyTo({center: m.cur, zoom: m.a.kind === 'ground' || m.a.kind === 'move' ? 11 : 7, duration: 1200});
  }

  function render(list, sel) {
    items = list; selected = sel;
    if (!map) return;
    draw();
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden && !loopOn && map) { loopOn = true; requestAnimationFrame(tick); } });
  window.HAMap = {init, render, fit, select: focus, icons: {plane: PLANE, heli: HELI}};
})();
