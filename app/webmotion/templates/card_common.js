// Shared by the «card» inserts: the starry background, number formatting, easing and the soft blur-in.
(function () {
  const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
  const ease = t => { t = clamp(t, 0, 1); return t * t * (3 - 2 * t); };
  const out3 = t => { t = clamp(t, 0, 1); return 1 - Math.pow(1 - t, 3); };
  const seg = (f, a, b) => (f - a) / Math.max(b - a, 1e-6);
  const lerp = (a, b, t) => a + (b - a) * t;
  let seed = 11; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const stars = Array.from({ length: 180 }, () => ({ x: rnd(), y: rnd(), r: rnd() < .12 ? 1.6 : 1, a: .15 + rnd() * .5, p: rnd() * 6.28, s: .02 + rnd() * .05 }));
  const css = n => getComputedStyle(document.body).getPropertyValue(n).trim();
  if (window.SPEC && window.SPEC.theme) document.body.classList.add(`theme-${window.SPEC.theme}`);
  function background(cv, f) {
    const W = cv.width, H = cv.height, x = cv.getContext("2d");
    const g = x.createRadialGradient(W * .5, H * .45, 40, W * .5, H * .5, W * .75);
    g.addColorStop(0, css("--bg0")); g.addColorStop(.55, css("--bg1")); g.addColorStop(1, css("--bg2"));
    x.fillStyle = g; x.fillRect(0, 0, W, H);
    const star = css("--star");
    for (const s of stars) {  // a slow drift and a twinkle, both from the frame number
      const a = s.a * (.65 + .35 * Math.sin(s.p + f * s.s));
      x.fillStyle = `rgba(${star},${a})`;
      x.beginPath(); x.arc(((s.x + f * 0.00004) % 1) * W, s.y * H, s.r, 0, 6.283); x.fill();
    }
  }
  const LOCALES = { ru: "ru-RU", es: "es-ES", de: "de-DE", en: "en-US" };
  function fmt(v, decimals, lang) {
    const s = new Intl.NumberFormat(LOCALES[lang] || "en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
      .format(Math.abs(v)).replace(/ | /g, " ");
    return (v < 0 && Math.abs(v) >= Math.pow(10, -decimals) / 2 ? "−" : "") + s;
  }
  function blurIn(el, t, dy = 14) {
    t = clamp(t, 0, 1);
    el.style.opacity = t;
    el.style.filter = t >= 1 ? "none" : `blur(${(1 - t) * 14}px)`;
    el.style.transform = `translateY(${dy * (1 - t)}px)`;
  }
  function tag(S, f, N) {
    const el = document.getElementById("tag");
    if (S.tag && !el.textContent) el.textContent = S.tag;
    const a = ease(seg(f, 0, 0.06 * N + 6));
    el.style.opacity = S.tag ? a : 0; el.style.transform = `translateX(${-14 * (1 - a)}px)`;
  }
  async function ready() {
    await Promise.all(["700 100px Oswald", "600 30px Montserrat", "500 22px Montserrat"].map(f => document.fonts.load(f, "AБя0")));
    await document.fonts.ready;
  }
  // a colour of the theme as "r,g,b" (for canvas drawing with alpha)
  function rgb(name) {
    const c = document.createElement("canvas").getContext("2d"); c.fillStyle = css(name) || "#fff";
    const h = c.fillStyle; if (h.startsWith("#")) return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)).join(",");
    return h.replace(/rgba?\(|\)|\s/g, "").split(",").slice(0, 3).join(",");
  }
  // the scene's own picture under the text: a slow push-in (Ken Burns), the same on every machine
  function photo(img, f, N, dir = 1) {
    const t = clamp(f / Math.max(N - 1, 1), 0, 1), s = 1.04 + 0.08 * t;
    img.style.transform = `scale(${s}) translate(${dir * (t - .5) * 1.2}%, ${(t - .5) * -0.6}%)`;
  }
  async function loadPhoto(img, src) {
    if (!src) return false;
    img.src = src;
    try { await img.decode(); return true; } catch (e) { return false; }
  }
  window.CARD = { clamp, ease, out3, seg, lerp, background, fmt, blurIn, tag, ready, css, rgb, photo, loadPhoto };
})();
