// Shared by globe3d.html and globe2d.html: the timing of the shot, the camera path, labels and plates.
// SPEC (from webmotion.py): {w, h, frames, view: {lat, lon, spread}, countries: [a3], route: [place], pins: [place],
//                            tag, headline, subline}; place = {name, note, lat, lon}.
(function () {
  const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
  const ease = t => { t = clamp(t, 0, 1); return t * t * (3 - 2 * t); };
  const ease2 = t => { t = clamp(t, 0, 1); return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
  const seg = (f, a, b) => (f - a) / Math.max(b - a, 1e-6);
  const lerp = (a, b, t) => a + (b - a) * t;

  function plan(S, N) {
    const view = S.view || { lat: 20, lon: 0, spread: 40 };
    const dEnd = clamp(1.9 + (view.spread || 20) * 0.035, 2.25, 4.3);
    const d1 = dEnd * 1.04, d0 = Math.max(4.6, dEnd + 1.2);
    const lat0 = clamp(view.lat - 14, -45, 45), lon0 = view.lon + 62;
    const arrive = 0.42 * (N - 1);
    function camera(f) {
      let lat, lon, dist;
      if (f <= arrive) {
        const s = ease2(f / arrive);
        lat = lerp(lat0, view.lat, s); lon = lerp(lon0, view.lon, s); dist = lerp(d0, d1, ease(s * 1.05));
      } else {  // a slow drift after arriving: starts and ends without a jolt
        const s = ease((f - arrive) / (N - 1 - arrive));
        lat = view.lat - 1.2 * s; lon = view.lon - 3 * s; dist = lerp(d1, dEnd * 1.12, s);
      }
      return { lat, lon, dist, r: 1883 * (S.h || 1080) / 1080 / Math.sqrt(dist * dist - 1) };
    }
    // places: the stops of the route first, then the other pins; a country alone gets its own point
    const seen = new Set(), places = [];
    const add = (p, kind) => { const key = `${(+p.lat).toFixed(2)},${(+p.lon).toFixed(2)}`; if (seen.has(key)) return; seen.add(key); places.push({ ...p, kind }); };
    (S.route || []).forEach(p => add(p, "route"));
    (S.pins || []).forEach(p => add(p, "pin"));
    if (!places.length) (S.country_points || []).forEach(p => add(p, "country"));
    const route = places.filter(p => p.kind === "route");
    const legs = [];
    const drawFrom = 0.40 * N, drawTo = 0.74 * N;
    for (let i = 1; i < route.length; i++) {
      const span = (drawTo - drawFrom) / (route.length - 1);
      legs.push({ a: route[i - 1], b: route[i], from: drawFrom + span * (i - 1), to: drawFrom + span * i });
    }
    route.forEach((p, i) => { p.at = i === 0 ? 0.35 * N : legs[i - 1].to - 6; });
    let k = 0;
    places.filter(p => p.kind !== "route").forEach(p => { p.at = Math.min(0.36 * N + 0.07 * N * k++, 0.72 * N); });
    return { places, legs, camera, lightAt: 0.2 * N, scale: dEnd / 2.4, titleAt: 0.62 * N, N };
  }

  // labels: one side for each (right of its point, or left near the right edge), pushed apart so none overlap,
  // kept clear of the plate at the top and the title at the bottom. Laid out once, at the last frame.
  function labels(list, W, H) {
    const box = document.getElementById("labels");
    const out = list.map(p => {
      const el = document.createElement("div");
      el.className = "lab";
      el.innerHTML = `<div class="k"></div><div class="v"></div>`;
      el.querySelector(".k").textContent = p.name || "";
      el.querySelector(".v").textContent = p.note || "";
      box.appendChild(el);
      const w = el.offsetWidth, h = el.offsetHeight;
      // a stop of the route takes the side away from its neighbour, so the label never covers the arc
      const route = list.filter(q => q.kind === "route"), i = route.indexOf(p);
      const near = i < 0 ? null : (route[i + 1] || route[i - 1]);
      const fitsLeft = p.x - 46 - w > 40, fitsRight = p.x + 46 + w < W - 40;
      let left = !fitsRight;
      if (near && Math.abs(near.x - p.x) > 30) left = near.x > p.x ? fitsLeft : !fitsRight;
      if (left) el.classList.add("left");
      return { p, el, w, h, left, x: left ? p.x - 46 - w : p.x + 46, y: p.y - h / 2 };
    });
    const top = 150, bottom = H - 230;
    out.sort((a, b) => a.y - b.y);
    for (let i = 0; i < out.length; i++) {
      const a = out[i];
      a.y = clamp(a.y, top, bottom - a.h);
      for (let j = 0; j < i; j++) {
        const b = out[j];
        if (a.x < b.x + b.w + 16 && b.x < a.x + a.w + 16 && a.y < b.y + b.h + 12) a.y = b.y + b.h + 12;
      }
    }
    out.forEach(a => { a.dy = a.y - (a.p.y - a.h / 2); });
    return out;
  }
  function drawLabels(list, f, project) {
    for (const a of list) {
      const q = project(a.p);
      const v = q.facing ? ease(seg(f, a.p.at + 8, a.p.at + 22)) : 0;
      a.el.style.opacity = v;
      const x = a.left ? q.x - 46 - a.w + 20 * (1 - v) : q.x + 46 - 20 * (1 - v);
      a.el.style.transform = `translate(${x}px, ${q.y - a.h / 2 + a.dy}px)`;
    }
  }
  function drawPlates(S, plan, f) {
    const tag = document.getElementById("tag"), title = document.getElementById("title");
    if (S.tag && !tag.textContent) tag.textContent = S.tag;
    if (S.headline && !title.dataset.set) {
      title.textContent = S.headline;
      if (S.subline) { const s = document.createElement("small"); s.textContent = S.subline; title.appendChild(s); }
      title.dataset.set = "1";
    }
    const a = ease(seg(f, 0.06 * plan.N, 0.06 * plan.N + 18));
    tag.style.opacity = S.tag ? a : 0; tag.style.transform = `translateX(${-16 * (1 - a)}px)`;
    const t = ease(seg(f, plan.titleAt, plan.titleAt + 24));
    title.style.opacity = S.headline ? t : 0; title.style.transform = `translateY(${18 * (1 - t)}px)`;
  }
  async function ready() {
    await Promise.all(["700 42px Oswald", "600 25px Inter", "400 30px Inter", "600 21px Inter"].map(f => document.fonts.load(f, "AБя")));
    await document.fonts.ready;
  }
  window.GLOBE = { clamp, ease, ease2, seg, lerp, plan, labels, drawLabels, drawPlates, ready };
})();
