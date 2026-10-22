"use strict";
const $ = (s, el = document) => el.querySelector(s);
const main = $("#main");
let META = null;
let timer = null;

const STATUS_RU = { queued: "В очереди", running: "Делается", review: "Ждёт вашей проверки", paused: "На паузе", done: "Готово", error: "Ошибка" };

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function nl(s) { return esc(s).replace(/\n/g, "<br>"); }
function toast(msg, err = false) {
  const d = document.createElement("div"); d.textContent = msg; if (err) d.className = "err";
  $("#toast").appendChild(d); setTimeout(() => d.remove(), err ? 9000 : 3500);
}
async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) { o.method = o.method || "POST"; o.headers = { "Content-Type": "application/json" }; o.body = JSON.stringify(o.json); delete o.json; }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = `Ошибка ${r.status}`;
    try { const j = await r.json(); msg = j.error || j.detail || msg; } catch (e) { }
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.blob();
}
function scoreCls(v) { return v >= 8 ? "s-good" : v >= 7 ? "s-mid" : "s-bad"; }
function score(v) { return `<span class="score ${scoreCls(v)}">${esc(v)}</span>`; }
function fmtTime(sec) { sec = Math.round(sec || 0); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`; }
function badge(st) { return `<span class="badge ${st}">${STATUS_RU[st] || st}</span>`; }
function copyBtn(text) { return `<button class="sm" data-copy="${esc(text)}">Копировать</button>`; }
document.addEventListener("click", e => {
  const b = e.target.closest("[data-copy]");
  if (b) { navigator.clipboard.writeText(b.dataset.copy); toast("Скопировано"); }
});

// ------------------------------------------------------------------ theme: auto follows the Mac (light by day, dark at night)
const darkMQ = matchMedia("(prefers-color-scheme: dark)");
function themeMode() { try { return localStorage.getItem("ay_theme") || "auto"; } catch (e) { return "auto"; } }
function applyTheme() {
  const m = themeMode();
  document.documentElement.dataset.theme = m === "dark" || (m === "auto" && darkMQ.matches) ? "dark" : "light";
  document.querySelectorAll("#themesw [data-th]").forEach(b => b.classList.toggle("on", b.dataset.th === m));
}
darkMQ.addEventListener("change", applyTheme);
document.querySelectorAll("#themesw [data-th]").forEach(b => b.onclick = () => {
  try { localStorage.setItem("ay_theme", b.dataset.th); } catch (e) { }
  applyTheme();
});
applyTheme();

// ------------------------------------------------------------------ router
async function route() {
  clearInterval(timer); timer = null;
  if (!META) { META = await api("/api/meta"); STYLE_PRESETS = META.image_styles; }
  const [, page, id] = (location.hash || "#/jobs").split("/");
  document.querySelectorAll("[data-nav]").forEach(a => a.classList.toggle("active", a.dataset.nav === page || (page === "job" && a.dataset.nav === "jobs")));
  try {
    if (page === "new") await viewNew();
    else if (page === "job") await viewJob(id);
    else if (page === "settings") await viewSettings(id);
    else if (page === "projects") await viewProjects(id);
    else if (page === "characters") await viewCharacters();
    else if (page === "items") await viewItems();
    else if (page === "prompts") await viewPrompts(id);
    else if (page === "music") await viewMusic();
    else if (page === "remake") await viewRemake(id);
    else if (page === "producer") await viewProducer(id);
    else if (page === "library") await viewLibrary();
    else if (page === "help") viewHelp();
    else if (page === "balance") await viewBalance();
    else await viewJobs();
  } catch (e) { main.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
  draftRestore();
  rememberTab();
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ unsaved input survives leaving the page
// Everything typed on a page is kept per page address until it is saved, so switching tabs loses nothing.
const DRAFT_PAGES = ["new", "projects", "settings", "characters", "items", "prompts"];
function draftKey() { return "ay_draft:" + (location.hash || "#/jobs"); }
function draftOn() { return DRAFT_PAGES.includes((location.hash || "#/jobs").split("/")[1]); }
function draftFields() {
  const out = {}, seen = {};
  main.querySelectorAll("input, textarea, select").forEach(el => {
    if (el.type === "file" || el.type === "button" || el.closest("[data-nodraft]")) return;
    let k = el.id ? "#" + el.id : el.dataset.q ? "q:" + el.dataset.q : el.dataset.p ? "p:" + el.dataset.p : el.name ? "n:" + el.name : el.tagName + "." + el.className;
    if (!el.id && !el.dataset.q && !el.name) { seen[k] = (seen[k] || 0) + 1; k += "@" + seen[k]; }
    out[k] = el;
  });
  return out;
}
const fieldVal = el => el.type === "checkbox" || el.type === "radio" ? el.checked : el.value;
function draftClear() { try { localStorage.removeItem(draftKey()); } catch (e) { } }
function draftBaseReset() {
  main.dataset.draftBase = JSON.stringify(Object.fromEntries(Object.entries(draftFields()).map(([k, el]) => [k, fieldVal(el)])));
  $("#draft-bar")?.remove();
}
let draftTimer = null;
function draftSave() {
  if (!draftOn() || main.dataset.restoring) return;
  const d = {};
  Object.entries(draftFields()).forEach(([k, el]) => d[k] = fieldVal(el));
  const base = JSON.parse(main.dataset.draftBase || "{}");
  const changed = Object.fromEntries(Object.entries(d).filter(([k, v]) => base[k] !== v));
  if (Object.keys(changed).length) lsSet(draftKey(), JSON.stringify(changed)); else draftClear();
}
main.addEventListener("input", () => { clearTimeout(draftTimer); draftTimer = setTimeout(draftSave, 300); });
main.addEventListener("change", () => { clearTimeout(draftTimer); draftTimer = setTimeout(draftSave, 300); });
function draftRestore() {
  if (!draftOn()) return;
  const base = {};
  Object.entries(draftFields()).forEach(([k, el]) => base[k] = fieldVal(el));
  main.dataset.draftBase = JSON.stringify(base);
  let d = {};
  try { d = JSON.parse(lsGet(draftKey(), "{}")) || {}; } catch (e) { }
  if (!Object.keys(d).length) return;
  const apply = () => {
    const f = draftFields(); let changed = 0;
    main.dataset.restoring = 1;
    Object.entries(d).forEach(([k, v]) => {
      const el = f[k]; if (!el || fieldVal(el) === v) return;
      if (el.type === "checkbox" || el.type === "radio") el.checked = v; else el.value = v;
      if (el.tagName === "SELECT" && el.value !== v) return;  // options not loaded yet
      el.dispatchEvent(new Event("change", { bubbles: true }));
      changed++;
    });
    delete main.dataset.restoring;
    if (changed) showBar();
  };
  const showBar = () => {
    if ($("#draft-bar")) return;
    const bar = document.createElement("div");
    bar.id = "draft-bar";
    bar.className = "banner warn";
  bar.innerHTML = `На этой странице есть несохранённые изменения, я их вернул. Не забудьте сохранить. <button class="sm" id="draft-drop">Отменить изменения</button>`;
    main.prepend(bar);
    $("#draft-drop").onclick = () => { draftClear(); route(); };
  };
  const page = location.hash;
  apply();
  setTimeout(() => { if (location.hash === page) apply(); }, 1500);  // some lists (voices) appear a moment later
}

// ------------------------------------------------------------------ recent pages as tabs
const TAB_ICONS = { new: "＋", jobs: "🎬", job: "🎞️", projects: "📁", characters: "🧑‍🎨", items: "📦", settings: "⚙️", prompts: "📝", music: "🎵", remake: "🔁", producer: "🎓", library: "📚", help: "❓", balance: "💰" };
function getTabs() { try { return JSON.parse(lsGet("ay_tabs", "[]")) || []; } catch (e) { return []; } }
function rememberTab() {
  const hash = location.hash || "#/jobs";
  const page = hash.split("/")[1] || "jobs";
  const h1 = main.querySelector("h1");
  const title = (h1 ? h1.textContent : page).trim().replace(/\s+/g, " ").slice(0, 60);
  let tabs = getTabs();
  const i = tabs.findIndex(t => t.hash === hash);
  if (i >= 0) tabs[i].title = title; else tabs.push({ hash, title, icon: TAB_ICONS[page] || "📄" });
  if (tabs.length > 8) tabs = tabs.slice(-8);
  lsSet("ay_tabs", JSON.stringify(tabs));
  renderTabs();
}
function renderTabs() {
  const cur = location.hash || "#/jobs";
  $("#tabbar").innerHTML = getTabs().map(t => `<a class="tb ${t.hash === cur ? "on" : ""}" href="${esc(t.hash)}" title="${esc(t.title)}">${t.icon} <span>${esc(t.title)}</span><b data-close="${esc(t.hash)}" title="Закрыть">×</b></a>`).join("")
    + `<a class="tnew" href="#/new">＋ Новый ролик</a>`;
  $("#tabbar").querySelectorAll("[data-close]").forEach(x => x.onclick = e => {
    e.preventDefault(); e.stopPropagation();
    const tabs = getTabs(), i = tabs.findIndex(t => t.hash === x.dataset.close);
    tabs.splice(i, 1); lsSet("ay_tabs", JSON.stringify(tabs));
    if (x.dataset.close === cur) location.hash = tabs.length ? tabs[Math.min(i, tabs.length - 1)].hash : "#/jobs";
    else renderTabs();
  });
}

// ------------------------------------------------------------------ new video
async function viewNew() {
  const s = await api("/api/settings");
  const yt = await api("/api/youtube").catch(() => ({ channels: [] }));
  const projs = await api("/api/projects").catch(() => []);
  let curProj = lsGet("ay_proj");
  if (!projs.find(p => p.id === curProj)) curProj = "";
  const missing = [];
  if (s.llm_provider === "anthropic" ? !s.anthropic_key : !s.openai_key) missing.push("нейросети для сценария");
  if (!s.elevenlabs_key) missing.push("ElevenLabs (озвучка)");
  const prov = META.providers[s.image_provider];
  if (prov && !s[prov.key_field]) missing.push(`${prov.name} (картинки)`);
  main.innerHTML = `
  <h1>Новый ролик</h1>
  ${missing.length ? `<div class="banner warn">Не хватает ключей: ${missing.join(", ")}. Вставьте их в <a href="#/settings">Настройках</a>. Пока можно нажать «Проверить установку» внизу: соберётся тестовый ролик без ключей.</div>` : ""}
  <div class="card">
    <label class="f" style="margin-top:0">Проект <span class="hint">Одной кнопкой подставляются язык, ниша, голос, стиль и остальные настройки проекта. <a href="#/projects">Создать или изменить проекты</a></span></label>
    <div class="pgrid" id="projsel">
      <button class="pcard ${curProj ? "" : "on"}" data-proj=""><div class="pic">⚙️</div><div><b>Без проекта</b><div class="muted small">общие настройки</div></div></button>
      ${projs.map(p => `<button class="pcard ${p.id === curProj ? "on" : ""}" data-proj="${p.id}"><div class="pic">${esc(p.icon)}</div><div><b>${esc(p.name)}</b><div class="muted small">${p.language === "ru" ? "русский" : "английский"}</div></div></button>`).join("")}
    </div>
    <div id="charsel"></div><div id="itemsel"></div><input type="hidden" id="fmtv" value="long"><input type="hidden" id="charsv" value=""><input type="hidden" id="itemsv" value="">
    <label class="f">Формат</label>
    <div class="pgrid" style="margin-bottom:4px">
      <button class="pcard on" data-fmt="long"><div class="pic">🎬</div><div><b>Видео + шортс</b><div class="muted small">длинное видео, шортс по желанию</div></div></button>
      <button class="pcard" data-fmt="short"><div class="pic">📱</div><div><b>Только шортс</b><div class="muted small">вертикальное видео до 60 секунд</div></div></button>
    </div>
    <label class="f">Тема ролика <span class="hint">Можно по-русски. Оставьте пустым, и программа сама придумает 5 тем и выберет самую цепляющую (ещё темы можно попросить кнопкой).</span></label>
    <textarea id="topic" placeholder="Например: загадочное исчезновение колонии Роанок"></textarea>
    <div id="series-hint" class="banner" style="display:none;margin-top:8px"></div>
    <div class="grid2">
      <div class="long-only">
        <label class="f">Длительность, минут</label>
        <input type="number" id="dur" min="1" max="30" step="1" value="${esc(s.duration_min)}">
      </div>
      <div class="short-only" style="display:none">
        <label class="f">Длина шортса, секунд <span class="hint">15–180. Лучше всего заходят 30–60 секунд</span></label>
        <input type="number" id="sec" min="15" max="180" step="5" value="50">
      </div>
      <div>
        <label class="f">Сколько роликов сделать <span class="hint">Каждый получит свою тему</span></label>
        <input type="number" id="count" min="1" max="20" value="1">
      </div>
    </div>
    ${yt.channels.length ? `<label class="f">Для какого канала <span class="hint">Берутся ниша, голос и стиль картинок этого канала</span></label>
    <select id="chan"><option value="">Без канала (общие настройки)</option>${yt.channels.map(c => `<option value="${c.id}">${esc(c.title)}</option>`).join("")}</select>` : ""}
    <div class="grid2"><div>
      <label class="f">Ниша <span class="hint">Берётся из проекта или общих настроек</span></label>
      <textarea id="niche" data-nodraft rows="3">${esc(s.niche)}</textarea>
    </div><div>
      <label class="f">Язык ролика <span class="hint">Сценарий, озвучка и субтитры</span></label>
      <select id="lang">${Object.entries(META.languages).map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join("")}</select>
    </div></div>
    <label class="check long-only"><input type="checkbox" id="short" checked> Сделать ещё и вертикальный шортс (до 60 секунд)</label>
    <div class="grid2">
      <div><label class="f">Анимация сцен <span class="hint">Оживить картинки нейросетью (fal.ai, платно за каждую сцену)</span></label>
        <select id="anim">${animModes().map(([k, n]) => `<option value="${k}" ${k === s.anim_mode ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
        <div id="anim-n-box"><label class="f">Сколько сцен оживить <span class="hint">Конкретные сцены можно выбрать потом на странице ролика</span></label><input type="number" id="anim-n" min="1" max="60" value="${s.anim_count || 3}"></div></div>
      <div><label class="f">&nbsp;</label><label class="check long-only"><input type="checkbox" id="anim-s" ${s.anim_short ? "checked" : ""}> Оживить и шортс <span class="muted small">(столько же первых сцен)</span></label>
        <label class="check"><input type="checkbox" id="anim-p"> Пауза перед анимацией: я сам выберу сцены <span class="muted small">(когда картинки готовы, ролик остановится, вы отметите любые сцены и нажмёте «Продолжить»)</span></label></div>
    </div>
    <label class="f">Музыка <span class="hint">Сочинение новой платное, около $0.30 за минуту, трек сохранится в «Музыку»</span></label>
    <select id="music" style="max-width:520px">${Object.entries(META.music_modes).map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join("")}</select>
    <label class="f">Эффекты <span class="hint">Звуки перелистывания и ударов, наезды, тряска. Нейросеть ставит их по смыслу сценария. Бесплатно.</span></label>
    <select id="fx" style="max-width:520px">${Object.entries(META.fx_modes).map(([k, n]) => `<option value="${k}" ${k === (s.fx_mode || "light") ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
    <label class="check"><input type="checkbox" id="subs-v" ${s.subtitles_long ? "checked" : ""}> <span id="subs-v-l">Субтитры на длинном видео</span></label>
    <label class="check long-only"><input type="checkbox" id="subs-s" ${s.subtitles_short ? "checked" : ""}> Субтитры на шортсе</label>
    <label class="check"><input type="checkbox" id="ideapause" ${lsGet("ay_ideapause", "1") === "1" ? "checked" : ""} onchange="lsSet('ay_ideapause', this.checked ? '1' : '0')"> Выбирать тему вручную: показать лучшие темы и подождать, пока я отмечу одну <span class="muted small">(до сценария, почти бесплатно)</span></label>
    <label class="check"><input type="checkbox" id="pause" ${lsGet("ay_pause", "1") === "1" ? "checked" : ""} onchange="lsSet('ay_pause', this.checked ? '1' : '0')"> Ручная правка текста: остановиться сразу после проверки редактором, чтобы я прочитал и при желании исправил текст <span class="muted small">(дальше ничего не делается, пока вы не нажмёте «Продолжить»)</span></label>
    <div id="est" class="muted small"></div>
    <div class="row" style="margin-top:16px">
      <button class="primary big" id="go">▶ Запустить</button>
    </div>
  </div>
  <div class="card">
    <h2>Проверить установку</h2>
    <p class="muted">Соберёт короткий тестовый ролик с цветными заставками и гудками вместо голоса. Ключи не нужны и деньги не тратятся. Так видно, что сборка видео на вашем Mac работает.</p>
    <button id="demo">Проверить установку</button>
  </div>`;
  const upd = async () => {
    try {
      const e = await api(`/api/estimate?minutes=${$("#dur").value}&short=${$("#short").checked}&anim_mode=${$("#anim").value}&anim_short=${$("#anim-s").checked}&anim_count=${$("#anim-n").value}&language=${$("#lang").value}&format=${fmt}&short_seconds=${$("#sec").value}&music_mode=${$("#music").value}`);
      $("#anim-n-box").style.display = ["hook", "key"].includes($("#anim").value) ? "" : "none";
      const n = Math.max(1, +$("#count").value || 1);
      $("#est").textContent = `Примерная стоимость одного ролика: ~$${e.total} (сценарий ~$${e.llm_cost}, ${e.images} картинок ~$${e.image_cost}${e.anim_cost ? `, анимация ~$${e.anim_cost}` : ""}${e.music_cost ? `, музыка ~$${e.music_cost}` : ""}) + ~${e.tts_chars.toLocaleString("ru")} символов ElevenLabs из вашего тарифа.` + (n > 1 ? ` Всего роликов: ${n}.` : "");
    } catch (e) { }
  };
  ["#dur", "#sec", "#music", "#short", "#count", "#anim", "#anim-n", "#anim-s", "#lang"].forEach(id => $(id).addEventListener("change", upd)); upd();
  let fmt = "long";
  const setFmt = (f, fromUser) => {
    fmt = f; $("#fmtv").value = f;
    main.querySelectorAll("[data-fmt]").forEach(b => b.classList.toggle("on", b.dataset.fmt === f));
    main.querySelectorAll(".long-only").forEach(el => el.style.display = f === "short" ? "none" : "");
    main.querySelectorAll(".short-only").forEach(el => el.style.display = f === "short" ? "" : "none");
    $("#subs-v-l").textContent = f === "short" ? "Субтитры" : "Субтитры на длинном видео";
    const pj = projs.find(x => x.id === curProj);  // a chosen project keeps its own subtitles and effects
    if (fromUser) $("#subs-v").checked = pj ? !!(f === "short" ? pj.subs_short : pj.subs_video) : (f === "short" ? !!s.subtitles_short : !!s.subtitles_long);
    if (fromUser) $("#fx").value = pj ? pj.fx_mode || "light" : (f === "short" ? "strong" : (s.fx_mode || "light"));
    upd();
  };
  main.querySelectorAll("[data-fmt]").forEach(b => b.onclick = () => { setFmt(b.dataset.fmt, true); main.dispatchEvent(new Event("input")); });
  $("#fmtv").addEventListener("change", () => setFmt($("#fmtv").value));
  const syncChars = () => { $("#charsv").value = [...main.querySelectorAll("[data-char].on")].map(b => b.dataset.char).join(","); };
  $("#charsv").addEventListener("change", () => { const want = new Set($("#charsv").value.split(",")); main.querySelectorAll("[data-char]").forEach(b => b.classList.toggle("on", want.has(b.dataset.char))); });
  const syncItems = () => { $("#itemsv").value = [...main.querySelectorAll("[data-item].on")].map(b => b.dataset.item).join(","); };
  $("#itemsv").addEventListener("change", () => { const want = new Set($("#itemsv").value.split(",")); main.querySelectorAll("[data-item]").forEach(b => b.classList.toggle("on", want.has(b.dataset.item))); });
  const applyProj = () => {
    const p = projs.find(x => x.id === curProj);
    main.querySelectorAll("[data-proj]").forEach(b => b.classList.toggle("on", b.dataset.proj === curProj));
    $("#niche").value = (p && p.niche) || s.niche;
    $("#lang").value = p ? p.language : "en";
    $("#dur").value = p ? p.duration_min : s.duration_min;
    $("#sec").value = p ? p.short_seconds || 50 : 50;
    $("#music").value = p ? p.music_mode || "library" : "library";
    $("#short").checked = p ? p.make_short : true;
    $("#anim").value = p ? p.anim_mode : s.anim_mode;
    $("#anim-s").checked = p ? p.anim_short : s.anim_short;
    $("#anim-n").value = (p && p.anim_count) || s.anim_count || 3;
    $("#anim-p").checked = !!(p && p.anim_pause);
    $("#subs-v").checked = p ? p.subs_video : s.subtitles_long;
    $("#subs-s").checked = p ? p.subs_short : s.subtitles_short;
    if ($("#chan")) $("#chan").value = (p && p.channel_id && yt.channels.find(c => c.id === p.channel_id)) ? p.channel_id : "";
    setFmt(p ? p.format || "long" : "long");
    const hint = $("#series-hint"); hint.style.display = "none";
    if (p && p.series) api(`/api/projects/${p.id}/episodes`).then(r => {
      if (curProj !== p.id) return;
      const lines = (p.season_plan || "").split("\n").map(l => l.trim()).filter(Boolean)
        .map(l => l.replace(/^\s*(серия|episode|ep\.?)?\s*\d+\s*[.):—-]*\s*/i, "").trim());
      const plan = lines[r.next - 1];
      hint.innerHTML = `📺 Это сериал: следующий ролик станет <b>серией ${r.next}</b>. ` + (plan
        ? `Если тему не писать, сюжет возьмётся из плана сезона: «${esc(plan)}», с учётом библии сериала и прошлых серий.`
        : `В плане сезона нет строки для этой серии, поэтому если тему не писать, программа сама придумает продолжение по библии сериала и прошлым сериям.`)
        + ` Если тему написать, серия будет про неё, но всё равно продолжит сюжет.`;
      hint.style.display = "";
    }).catch(() => { });
    const want = new Set(p ? p.characters || [] : []);
    $("#charsel").querySelectorAll("[data-char]").forEach(b => b.classList.toggle("on", want.has(b.dataset.char)));
    syncChars();
    const wantI = new Set(p ? p.items || [] : []);
    $("#itemsel").querySelectorAll("[data-item]").forEach(b => b.classList.toggle("on", wantI.has(b.dataset.item)));
    syncItems();
    $("#fx").value = p ? p.fx_mode || "light" : (s.fx_mode || "light");
    if (p && p.format === "short") $("#subs-v").checked = p.subs_short;
  };
  main.querySelectorAll("[data-proj]").forEach(b => b.onclick = () => { curProj = b.dataset.proj; lsSet("ay_proj", curProj); applyProj(); });
  const allChars = await api("/api/characters").catch(() => []);
  $("#charsel").innerHTML = `<label class="f">Персонажи в ролике <span class="hint">Нажмите, чтобы выбрать. Сценарий и картинки будут строиться вокруг них. Персонажи проекта отмечаются сами. <a href="#/characters">Добавить персонажа</a></span></label>
    ${allChars.length ? `<div class="row">${allChars.map(c => `<button class="cbtn" data-char="${c.id}" title="${esc(c.role)}">${charIcon(c, 48)}<span>${esc(c.name)}</span></button>`).join("")}</div>`
      : `<p class="muted small" style="margin:0">Персонажей пока нет. Их можно добавить в разделе «Персонажи» слева.</p>`}`;
  $("#charsel").querySelectorAll("[data-char]").forEach(b => b.onclick = () => { b.classList.toggle("on"); syncChars(); main.dispatchEvent(new Event("input")); });
  const allItems = await api("/api/items").catch(() => []);
  $("#itemsel").innerHTML = `<label class="f">Предметы в ролике <span class="hint">Товары и вещи, которые должны появиться в кадре точно как на ваших фото. Предметы проекта отмечаются сами. <a href="#/items">Добавить предмет</a></span></label>
    ${allItems.length ? `<div class="row">${allItems.map(it => `<button class="cbtn" data-item="${it.id}" title="${esc(it.kind)}">${itemIcon(it, 48)}<span>${esc(it.name)}</span></button>`).join("")}</div>`
      : `<p class="muted small" style="margin:0">Предметов пока нет. Их можно добавить в разделе «Предметы» слева.</p>`}`;
  $("#itemsel").querySelectorAll("[data-item]").forEach(b => b.onclick = () => { b.classList.toggle("on"); syncItems(); main.dispatchEvent(new Event("input")); });
  if (curProj) applyProj();
  if ($("#chan")) $("#chan").onchange = () => {
    const c = yt.channels.find(x => x.id === $("#chan").value);
    if (!curProj) $("#niche").value = (c && c.profile.niche) || s.niche;  // a chosen project keeps its own niche
  };
  $("#go").onclick = async () => {
    if (!curProj && projs.length && !confirm("Проект не выбран. Ролик возьмёт общую нишу из настроек и общий голос, а не нишу и голос вашего проекта. Всё равно запустить?\n\nЧтобы взять настройки проекта, нажмите «Отмена» и выберите проект вверху страницы.")) return;
    const jobs = await api("/api/jobs", { json: { topic: $("#topic").value, duration_min: +$("#dur").value, short_seconds: +$("#sec").value, music_mode: $("#music").value, count: +$("#count").value, niche: $("#niche").value, make_short: $("#short").checked, review_pause: $("#pause").checked, idea_pause: $("#ideapause").checked, channel_id: $("#chan") ? $("#chan").value : undefined, subs_video: $("#subs-v").checked, subs_short: $("#subs-s").checked, anim_mode: $("#anim").value, anim_short: $("#anim-s").checked, anim_count: +$("#anim-n").value, anim_pause: $("#anim-p").checked, project_id: curProj, language: $("#lang").value, format: fmt, fx_mode: $("#fx").value, characters: [...main.querySelectorAll("[data-char].on")].map(b => b.dataset.char), items: [...main.querySelectorAll("[data-item].on")].map(b => b.dataset.item) } });
    draftClear(); toast("Запущено"); location.hash = jobs.length === 1 ? `#/job/${jobs[0].id}` : "#/jobs";
  };
  $("#demo").onclick = async () => {
    const jobs = await api("/api/jobs", { json: { demo: true, make_short: true } });
    location.hash = `#/job/${jobs[0].id}`;
  };
}

// ------------------------------------------------------------------ list
function lsGet(k, d = "") { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { } }

async function viewJobs() {
  const projs = await api("/api/projects");
  let filter = lsGet("ay_jobs_filter");
  if (filter && filter !== "none" && !projs.find(p => p.id === filter)) filter = "";
  const render = async () => {
    const jobs = await api(`/api/jobs?project=${encodeURIComponent(filter)}`);
    const tabs = projs.length ? `<div class="tabs">
      <button class="${filter ? "" : "on"}" data-f="">Все</button>
      ${projs.map(p => `<button class="${filter === p.id ? "on" : ""}" data-f="${p.id}">${esc(p.icon)} ${esc(p.name)}</button>`).join("")}
      <button class="${filter === "none" ? "on" : ""}" data-f="none">Без проекта</button></div>` : "";
    main.innerHTML = `<div class="row"><h1 class="grow">Ролики</h1><button id="openvids" title="Фильмы → Авто ютуб: папка на каждый проект, внутри папка на каждый ролик">📂 Папка с роликами</button><a class="btn primary" href="#/new">＋ Новый ролик</a></div>
    ${tabs}
    ${jobs.length ? "" : `<div class="card muted">${filter ? "В этом проекте пока нет роликов." : "Пока нет ни одного ролика. Нажмите «Новый ролик»."}</div>`}
    <div class="jobs">${jobs.map(j => `
      <a class="jobcard" href="#/job/${j.id}">
        <div class="thumb" style="${j.thumb ? `background-image:url('/files/${j.id}/${j.thumb}')` : ""}">${j.thumb ? "" : "🎬"}</div>
        <div class="body">
          <div class="t">${j.options.episode ? `Серия ${j.options.episode}. ` : ""}${esc(j.options.title_ru || j.title || j.topic || "Тема ещё выбирается…")}${j.options.demo ? " <span class='muted small'>(тест)</span>" : ""}</div>
          <div class="row small">${badge(j.status)} <span class="muted">${esc(j.status === "running" ? j.stage_name : new Date(j.created * 1000).toLocaleString("ru"))}</span></div>
          ${j.project || j.channel_title ? `<div class="muted small" style="margin-top:4px">${j.project ? `${esc(j.project.icon)} ${esc(j.project.name)}` : ""}${j.project && j.channel_title ? " · " : ""}${j.channel_title ? `📺 ${esc(j.channel_title)}` : ""}${j.options.language === "ru" ? " · RU" : ""}</div>` : ""}
          ${["running", "queued", "paused"].includes(j.status) ? `<div class="bar" style="margin-top:8px"><div style="width:${j.progress}%"></div></div>` : ""}
          ${j.status === "error" ? `<div class="small" style="color:var(--bad);margin-top:6px">${esc(j.error.slice(0, 140))}</div>` : ""}
        </div>
      </a>`).join("")}</div>`;
    main.querySelectorAll("[data-f]").forEach(b => b.onclick = () => { filter = b.dataset.f; lsSet("ay_jobs_filter", filter); render(); });
    $("#openvids").onclick = () => api("/api/open", { json: { what: "videos" } }).catch(e => toast(e.message, true));
  };
  await render();
  timer = setInterval(() => render().catch(() => { }), 3000);
}

// ------------------------------------------------------------------ projects
function charIcon(c, size = 56) {
  return c.has_portrait
    ? `<img class="cicon" style="width:${size}px;height:${size}px" src="/portraits/${c.id}.png?v=${Date.now()}">`
    : `<div class="cicon" style="width:${size}px;height:${size}px;font-size:${size / 2.4}px">${esc((c.name || "?")[0].toUpperCase())}</div>`;
}

async function renderChars() {
  const box = $("#chars");
  const list = await api("/api/characters");
  const projs = await api("/api/projects").catch(() => []);
  const oldStyle = c => c.style || (projs.find(p => p.id === c.style_project) || {}).image_style || "";
  const projSel = (cur) => `<div class="row" style="gap:6px">${Object.entries(STYLE_PRESETS).map(([k, v]) => `<button class="sm ${v.text === cur ? "on" : ""}" data-cstyle="${k}" title="${esc(v.desc)}">${esc(v.name)}</button>`).join("")}</div>
    <div class="small c-style-desc" style="margin-top:6px">${styleDesc(cur)}</div>
    <details style="margin-top:4px"><summary class="muted small">Точный текст стиля для нейросети (по-английски, можно поправить)</summary><textarea class="c-style" rows="3">${esc(cur)}</textarea></details>`;
  const form = (c = {}) => `<div class="cform" data-cid="${c.id || ""}">
    <div class="grid2"><div>
      <label class="f">Имя</label><input type="text" class="c-name" value="${esc(c.name)}" placeholder="Например: Дед Архип">
    </div><div>
      <label class="f">Роль <span class="hint">рассказчик, ведущий, главный герой…</span></label><input type="text" class="c-role" value="${esc(c.role)}">
    </div></div>
    <label class="f">Стиль портрета <span class="hint">Как будет нарисован персонаж. Наведите на кнопку, чтобы прочитать описание.</span></label>${projSel(oldStyle(c))}
    <label class="f">Описание по-русски <span class="hint">Возраст, характер, чем занимается. Если загружаете фото, внешность программа опишет сама строго по фото.</span></label>
    <textarea class="c-desc">${esc(c.description_ru)}</textarea>
    <label class="f">Дополнения для сцен <span class="hint">Необязательно. Другая одежда, вещи, детали, которые можно добавлять в некоторых сценах, например: «зимой — тёплая куртка и шапка», «в офисе — костюм». Основная внешность при этом не меняется.</span></label>
    <textarea class="c-extra" rows="2">${esc(c.extra_ru || "")}</textarea>
    <label class="f">Манера речи <span class="hint">Необязательно. Как говорит персонаж: тон, любимые словечки, присказки, 2–3 примера фраз. Сценарист будет писать его реплики и рассказ именно так.</span></label>
    <textarea class="c-speech" rows="4" placeholder="Например: говорит спокойно и с ухмылкой, коротко. Словечки: «блин», «короче», «бабло». Фразы: «Вёз я вчера одного…», «Ладно, поехали дальше.»">${esc(c.speech_ru || "")}</textarea>
    <label class="f">Голос персонажа <span class="hint">Если выбрать, персонаж будет сам говорить своим голосом: сценарий напишет ему реплики, а озвучка прочитает их этим голосом. Без голоса его слова пересказывает рассказчик.</span></label>
    <div class="row" style="gap:6px;flex-wrap:nowrap"><select class="c-voice"><option value="">Без своего голоса</option>${c.voice_id ? `<option value="${esc(c.voice_id)}" selected>${esc(c.voice_name || c.voice_id)}</option>` : ""}</select>
      <button class="sm c-vprev" type="button">▶</button></div>
    <input type="text" class="c-voice-id" placeholder="…или вставьте ID голоса из ElevenLabs" style="margin-top:6px">
    ${c.id ? "" : `<label class="f">Фото персонажа <span class="hint">Необязательно. Можно выбрать сразу несколько (до 9): спереди, сбоку, со спины, детали. Первое станет главным портретом, остальные — фото с разных сторон.</span></label><input type="file" accept="image/*" multiple class="c-newfile">`}
    ${c.id ? `<label class="f">Внешность для картинок (по-английски) <span class="hint">Составлена автоматически, можно поправить</span></label><textarea class="c-app">${esc(c.appearance)}</textarea>` : ""}
    <div class="row" style="margin-top:10px">
      <button class="primary sm c-save">${c.id ? "Сохранить" : "Добавить персонажа"}</button>
      ${c.id ? `<button class="sm c-desc-btn">🔍 Описать внешность по фото</button>
        <button class="sm c-draw">🎨 Нарисовать портрет</button>
        <label class="btn sm" style="cursor:pointer">📤 Загрузить свой<input type="file" accept="image/*" class="c-file" style="display:none"></label>
        <span class="grow"></span><button class="sm danger c-del">Удалить</button>` : ""}
    </div></div>`;
  box.innerHTML = (list.length ? `<div class="clist">${list.map(c => `<div class="citem">
      ${charIcon(c, 72)}<div class="grow"><b>${esc(c.name)}</b> <span class="muted small">${esc(c.role)}</span>
      <div class="small">${esc(c.description_ru)}</div>${c.appearance ? `<div class="en">${esc(c.appearance)}</div>` : ""}${c.extra_ru ? `<div class="small" style="margin-top:2px">➕ Дополнения для сцен: ${esc(c.extra_ru)}</div>` : ""}${c.speech_ru ? `<div class="small" style="margin-top:2px">💬 Манера речи: ${esc(c.speech_ru)}</div>` : ""}<div class="small" style="margin-top:2px">${c.voice_id ? `🗣 Свой голос: ${esc(c.voice_name || c.voice_id)}` : `<span class="muted">🗣 Своего голоса нет, его слова пересказывает рассказчик</span>`}</div><div class="small muted" style="margin-top:2px">${styleDesc(oldStyle(c)).replace("Выбрано", "Стиль портрета")}</div>
      <div class="row" style="margin-top:6px;gap:6px"><button class="sm c-edit" data-cid="${c.id}">Изменить</button>
        <label class="btn sm" style="cursor:pointer">📤 ${c.has_portrait ? "Заменить портрет" : "Загрузить портрет"}<input type="file" accept="image/*" class="c-qfile" data-cid="${c.id}" style="display:none"></label>
        <button class="sm c-qdraw" data-cid="${c.id}">🎨 Нарисовать портрет</button>
        <label class="btn sm" style="cursor:pointer">📷 Фото с разных сторон (${(c.refs || []).length}/8)<input type="file" accept="image/*" multiple class="c-refs" data-cid="${c.id}" style="display:none"></label></div>
      ${(c.refs || []).length ? `<div class="refstrip">${c.refs.map(r => `<div class="ref"><img src="/charrefs/${c.id}/${r}?v=${Date.now()}">
        <b data-rmain="${c.id}/${r}" title="Сделать главным портретом">★</b><b data-rdel="${c.id}/${r}" title="Удалить фото">×</b></div>`).join("")}</div>` : ""}
      </div></div>`).join("")}</div>` : `<p class="muted">Персонажей пока нет.</p>`)
    + `<h3>Новый персонаж</h3>` + form();
  const bind = (root) => {
    const cid = root.dataset.cid;
    const val = cls => root.querySelector(cls)?.value ?? "";
    const vsel = root.querySelector(".c-voice");
    const voiceVal = () => {
      const manual = val(".c-voice-id").trim();
      if (manual) return { voice_id: manual, voice_name: `Свой голос (${manual.slice(0, 6)}…)` };
      const o = vsel.options[vsel.selectedIndex];
      return { voice_id: vsel.value, voice_name: vsel.value ? (o.dataset.name || o.text) : "" };
    };
    loadVoices().then(vs => { const cur = vsel.value; vsel.innerHTML = vsel.options[0].outerHTML + voiceOptions(vs, cur, ""); vsel.value = cur; }).catch(() => { });
    root.querySelector(".c-vprev").onclick = () => {
      const v = (VOICES || []).find(x => x.voice_id === vsel.value);
      if (!v || !v.preview_url) return toast("Выберите голос из списка (для своего ID образца нет)", true);
      const a = new Audio(v.preview_url); a.play();
    };
    root.querySelectorAll("[data-cstyle]").forEach(b => b.onclick = () => {
      const v = STYLE_PRESETS[b.dataset.cstyle];
      root.querySelector(".c-style").value = v.text;
      root.querySelector(".c-style-desc").innerHTML = styleDesc(v.text);
      root.querySelectorAll("[data-cstyle]").forEach(x => x.classList.toggle("on", x === b));
      root.querySelector(".c-style").dispatchEvent(new Event("input", { bubbles: true }));
    });
    root.querySelector(".c-style").oninput = () => { root.querySelector(".c-style-desc").innerHTML = styleDesc(root.querySelector(".c-style").value); };
    root.querySelector(".c-save").onclick = async e => {
      e.target.disabled = true;
      const files = [...(root.querySelector(".c-newfile")?.files || [])];
      const body = { name: val(".c-name"), role: val(".c-role"), description_ru: val(".c-desc"), extra_ru: val(".c-extra"), speech_ru: val(".c-speech"), style: val(".c-style"), ...voiceVal(), ...(cid ? { appearance: val(".c-app") } : {}) };
      try {
        let c = await api(`/api/characters${cid ? "/" + cid : ""}`, { json: { ...body, later: !cid && files.length > 0 } });
        if (!cid && files.length) {
          // Photos first, then the look is described from them.
          for (const [i, f] of files.slice(0, 9).entries())
            await api(`/api/characters/${c.id}/${i ? "refs" : "upload"}`, { method: "POST", body: f });
          toast("Фото загружены, описываю внешность по фото…");
          c = await api(`/api/characters/${c.id}`, { json: { ...body, describe: true } });
        }
        draftClear(); if (c.warning) toast(c.warning, true); else toast("Персонаж сохранён");
      } catch (err) { toast(err.message, true); }
      renderChars();
    };
    if (!cid) return;
    root.querySelector(".c-desc-btn").onclick = async e => {
      e.target.disabled = true;
      toast("Смотрю на фото и описываю внешность…");
      await api(`/api/characters/${cid}`, { json: { name: val(".c-name"), role: val(".c-role"), description_ru: val(".c-desc"), extra_ru: val(".c-extra"), speech_ru: val(".c-speech"), style: val(".c-style"), ...voiceVal(), describe: true } })
        .then(c => c.warning ? toast(c.warning, true) : toast("Описание обновлено по фото")).catch(err => toast(err.message, true));
      renderChars();
    };
    root.querySelector(".c-draw").onclick = async e => {
      e.target.disabled = true; e.target.textContent = "Рисую…";
      await api(`/api/characters/${cid}/portrait`, { json: {} }).then(() => toast("Портрет готов")).catch(err => toast(err.message, true));
      renderChars();
    };
    root.querySelector(".c-file").onchange = async e => {
      const f = e.target.files[0]; if (!f) return;
      await api(`/api/characters/${cid}/upload`, { method: "POST", body: f }).then(() => toast("Портрет загружен")).catch(err => toast(err.message, true));
      renderChars();
    };
    root.querySelector(".c-del").onclick = async () => {
      if (!confirm("Удалить персонажа?")) return;
      await api(`/api/characters/${cid}`, { method: "DELETE" }); renderChars();
    };
  };
  box.querySelectorAll(".cform").forEach(bind);
  box.querySelectorAll(".c-qfile").forEach(inp => inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    await api(`/api/characters/${inp.dataset.cid}/upload`, { method: "POST", body: f }).then(() => toast("Портрет загружен")).catch(err => toast(err.message, true));
    renderChars();
  });
  box.querySelectorAll(".c-refs").forEach(inp => inp.onchange = async () => {
    const files = [...inp.files]; if (!files.length) return;
    toast(`Загружаю фото: ${files.length}…`);
    for (const f of files) {
      const ok = await api(`/api/characters/${inp.dataset.cid}/refs`, { method: "POST", body: f }).then(() => true).catch(err => { toast(err.message, true); return false; });
      if (!ok) break;
    }
    toast("Фото добавлены. Нажмите «Изменить» → «Описать внешность по фото», чтобы описание учло новые фото.");
    renderChars();
  });
  box.querySelectorAll("[data-rdel]").forEach(x => x.onclick = async () => {
    if (!confirm("Удалить это фото?")) return;
    await api(`/api/characters/${x.dataset.rdel.replace("/", "/refs/")}`, { method: "DELETE" }); renderChars();
  });
  box.querySelectorAll("[data-rmain]").forEach(x => x.onclick = async () => {
    await api(`/api/characters/${x.dataset.rmain.replace("/", "/refs/")}/main`, { method: "POST" }).then(() => toast("Главный портрет заменён")).catch(err => toast(err.message, true));
    renderChars();
  });
  box.querySelectorAll(".c-qdraw").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "Рисую…";
    await api(`/api/characters/${b.dataset.cid}/portrait`, { json: {} }).then(() => toast("Портрет готов")).catch(err => toast(err.message, true));
    renderChars();
  });
  box.querySelectorAll(".c-edit").forEach(b => b.onclick = () => {
    const c = list.find(x => x.id === b.dataset.cid);
    const item = b.closest(".citem");
    item.outerHTML = `<div class="citem">${charIcon(c, 72)}<div class="grow">${form(c)}</div></div>`;
    bind(box.querySelector(`.cform[data-cid="${c.id}"]`));
  });
}

async function viewCharacters() {
  main.innerHTML = `<h1>Персонажи</h1>
  <p class="muted">Ваша библиотека постоянных героев: рассказчики, ведущие, герои мультиков и сериалов. Опишите персонажа по-русски, программа сама составит описание внешности для картинок. Потом нарисуйте портрет или загрузите свою картинку, а ещё до 8 фото с разных сторон (спина, профиль, детали): они складываются в один лист-подсказку, и нейросеть рисует персонажа одинаково со всех сторон. Персонажи хранятся всегда и подходят для любого проекта: при запуске ролика их выбирают кнопками, а в проекте можно отметить, кто выбирается сам по умолчанию. На картинках они выглядят как на портрете (через fal.ai, ~$0.04 за такую картинку).</p>
  <div class="card" id="chars">Загружаю…</div>`;
  await renderChars();
}

// ------------------------------------------------------------------ items
function itemIcon(it, size = 56) {
  return it.has_photo
    ? `<img class="cicon iicon" style="width:${size}px;height:${size}px" src="/itemphotos/${it.id}.png?v=${Date.now()}">`
    : `<div class="cicon iicon" style="width:${size}px;height:${size}px;font-size:${size / 2.4}px">📦</div>`;
}

async function renderItems() {
  const box = $("#items");
  const list = await api("/api/items");
  const form = (it = {}) => `<div class="cform" data-iid="${it.id || ""}">
    <div class="grid2"><div>
      <label class="f">Название</label><input type="text" class="i-name" value="${esc(it.name)}" placeholder="Например: Бритва Philips S5000">
    </div><div>
      <label class="f">Что это <span class="hint">товар, техника, игрушка, упаковка…</span></label><input type="text" class="i-kind" value="${esc(it.kind)}">
    </div></div>
    <label class="f">Описание по-русски <span class="hint">Что это за вещь и как ей пользуются. Как она выглядит, программа опишет сама строго по фото.</span></label>
    <textarea class="i-desc">${esc(it.description_ru)}</textarea>
    <label class="f">Как показывать в кадре <span class="hint">Необязательно. Например: «всегда в руке героя», «стоит на столе логотипом к зрителю», «показывать в коробке».</span></label>
    <textarea class="i-extra" rows="2">${esc(it.extra_ru || "")}</textarea>
    ${it.id ? "" : `<label class="f">Фото предмета <span class="hint">Можно выбрать сразу несколько (до 9): спереди, сбоку, сзади, упаковка, логотип крупно. Первое станет главным фото. Лучше на однотонном фоне.</span></label><input type="file" accept="image/*" multiple class="i-newfile">`}
    ${it.id ? `<label class="f">Описание для картинок (по-английски) <span class="hint">Составлено автоматически по фото, можно поправить</span></label><textarea class="i-app">${esc(it.appearance)}</textarea>` : ""}
    <div class="row" style="margin-top:10px">
      <button class="primary sm i-save">${it.id ? "Сохранить" : "Добавить предмет"}</button>
      ${it.id ? `<button class="sm i-desc-btn">🔍 Описать по фото</button><span class="grow"></span><button class="sm danger i-del">Удалить</button>` : ""}
    </div></div>`;
  box.innerHTML = (list.length ? `<div class="clist">${list.map(it => `<div class="citem">
      ${itemIcon(it, 72)}<div class="grow"><b>${esc(it.name)}</b> <span class="muted small">${esc(it.kind)}</span>
      <div class="small">${esc(it.description_ru)}</div>${it.appearance ? `<div class="en">${esc(it.appearance)}</div>` : ""}${it.extra_ru ? `<div class="small" style="margin-top:2px">🎬 В кадре: ${esc(it.extra_ru)}</div>` : ""}
      ${it.has_photo ? "" : `<div class="small" style="margin-top:2px;color:var(--bad, #b91c1c)">Нет фото: без него предмет рисуется только по описанию и может отличаться.</div>`}
      <div class="row" style="margin-top:6px;gap:6px"><button class="sm i-edit" data-iid="${it.id}">Изменить</button>
        <label class="btn sm" style="cursor:pointer">📤 ${it.has_photo ? "Заменить главное фото" : "Загрузить фото"}<input type="file" accept="image/*" class="i-qfile" data-iid="${it.id}" style="display:none"></label>
        <label class="btn sm" style="cursor:pointer">📷 Фото с разных сторон (${(it.refs || []).length}/8)<input type="file" accept="image/*" multiple class="i-refs" data-iid="${it.id}" style="display:none"></label></div>
      ${(it.refs || []).length ? `<div class="refstrip">${it.refs.map(r => `<div class="ref"><img src="/itemrefs/${it.id}/${r}?v=${Date.now()}">
        <b data-rmain="${it.id}/${r}" title="Сделать главным фото">★</b><b data-rdel="${it.id}/${r}" title="Удалить фото">×</b></div>`).join("")}</div>` : ""}
      </div></div>`).join("")}</div>` : `<p class="muted">Предметов пока нет.</p>`)
    + `<h3>Новый предмет</h3>` + form();
  const bind = (root) => {
    const iid = root.dataset.iid;
    const val = cls => root.querySelector(cls)?.value ?? "";
    const body = () => ({ name: val(".i-name"), kind: val(".i-kind"), description_ru: val(".i-desc"), extra_ru: val(".i-extra"), ...(iid ? { appearance: val(".i-app") } : {}) });
    root.querySelector(".i-save").onclick = async e => {
      e.target.disabled = true;
      const files = [...(root.querySelector(".i-newfile")?.files || [])];
      try {
        let it = await api(`/api/items${iid ? "/" + iid : ""}`, { json: { ...body(), later: !iid && files.length > 0 } });
        if (!iid && files.length) {
          for (const [i, f] of files.slice(0, 9).entries())
            await api(`/api/items/${it.id}/${i ? "refs" : "upload"}`, { method: "POST", body: f });
          toast("Фото загружены, описываю предмет по фото…");
          it = await api(`/api/items/${it.id}`, { json: { ...body(), describe: true } });
        }
        draftClear(); if (it.warning) toast(it.warning, true); else toast("Предмет сохранён");
      } catch (err) { toast(err.message, true); }
      renderItems();
    };
    if (!iid) return;
    root.querySelector(".i-desc-btn").onclick = async e => {
      e.target.disabled = true;
      toast("Смотрю на фото и описываю предмет…");
      const { appearance, ...rest } = body();
      await api(`/api/items/${iid}`, { json: { ...rest, describe: true } })
        .then(it => it.warning ? toast(it.warning, true) : toast("Описание обновлено по фото")).catch(err => toast(err.message, true));
      renderItems();
    };
    root.querySelector(".i-del").onclick = async () => {
      if (!confirm("Удалить предмет?")) return;
      await api(`/api/items/${iid}`, { method: "DELETE" }); renderItems();
    };
  };
  box.querySelectorAll(".cform").forEach(bind);
  box.querySelectorAll(".i-qfile").forEach(inp => inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    await api(`/api/items/${inp.dataset.iid}/upload`, { method: "POST", body: f }).then(() => toast("Фото загружено")).catch(err => toast(err.message, true));
    renderItems();
  });
  box.querySelectorAll(".i-refs").forEach(inp => inp.onchange = async () => {
    const files = [...inp.files]; if (!files.length) return;
    toast(`Загружаю фото: ${files.length}…`);
    for (const f of files) {
      const ok = await api(`/api/items/${inp.dataset.iid}/refs`, { method: "POST", body: f }).then(() => true).catch(err => { toast(err.message, true); return false; });
      if (!ok) break;
    }
    toast("Фото добавлены. Нажмите «Изменить» → «Описать по фото», чтобы описание учло новые фото.");
    renderItems();
  });
  box.querySelectorAll("[data-rdel]").forEach(x => x.onclick = async () => {
    if (!confirm("Удалить это фото?")) return;
    await api(`/api/items/${x.dataset.rdel.replace("/", "/refs/")}`, { method: "DELETE" }); renderItems();
  });
  box.querySelectorAll("[data-rmain]").forEach(x => x.onclick = async () => {
    await api(`/api/items/${x.dataset.rmain.replace("/", "/refs/")}/main`, { method: "POST" }).then(() => toast("Главное фото заменено")).catch(err => toast(err.message, true));
    renderItems();
  });
  box.querySelectorAll(".i-edit").forEach(b => b.onclick = () => {
    const it = list.find(x => x.id === b.dataset.iid);
    b.closest(".citem").outerHTML = `<div class="citem">${itemIcon(it, 72)}<div class="grow">${form(it)}</div></div>`;
    bind(box.querySelector(`.cform[data-iid="${it.id}"]`));
  });
}

async function viewItems() {
  main.innerHTML = `<h1>Предметы</h1>
  <p class="muted">Ваша библиотека товаров и вещей, которые появляются в кадре: продукт, который вы рекламируете, фирменная кружка ведущего, машина героя. Загрузите фото предмета (лучше несколько, с разных сторон и с логотипом крупно), программа сама составит по ним точное описание. При запуске ролика нажмите на нужные предметы: сценарий найдёт им место в истории, а на картинках они будут как на ваших фото (через fal.ai, ~$0.04 за такую картинку). В проекте можно отметить, какие предметы выбираются сами.</p>
  <div class="card" id="items">Загружаю…</div>`;
  await renderItems();
}

const AUD = { ru: "🇷🇺 Для русской аудитории", en: "🇺🇸 Для английской аудитории", both: "🌍 Не определено (можно пометить в Настройках)" };
let VOICES = null;
async function loadVoices(force) { if (!VOICES || force) VOICES = await api("/api/voices"); return VOICES; }
const KIND = { male: "👨 Мужские", female: "👩 Женские", child: "🧒 Детские", other: "🎙️ Другие" };
const voiceLabel = v => `${esc(v.name)} — ${v.flag || ""} ${esc(v.country || "")}${v.description ? ", " + esc(v.description) : ""}`;
function voiceOptions(vs, cur, lang) {
  const order = lang === "ru" ? ["ru", "both", "en"] : lang === "en" ? ["en", "both", "ru"] : ["ru", "en", "both"];
  let html = order.map(a => Object.keys(KIND).map(k => {
    const g = vs.filter(v => v.audience === a && (v.kind || "other") === k);
    return g.length ? `<optgroup label="${AUD[a].split(" (")[0]} · ${KIND[k]}">${g.map(v => `<option value="${v.voice_id}" data-name="${esc(v.name)}" ${v.voice_id === cur ? "selected" : ""}>${voiceLabel(v)}</option>`).join("")}</optgroup>` : "";
  }).join("")).join("");
  if (cur && !vs.find(v => v.voice_id === cur)) html = `<option value="${esc(cur)}" selected>Свой голос (${esc(cur.slice(0, 6))}…)</option>` + html;
  return html;
}

// Russian voices from the public ElevenLabs library. onPick(voice_id, name) gets a voice already added to the account.
function openVoiceLibrary(onPick) {
  const wrap = document.createElement("div");
  wrap.className = "vlib"; wrap.dataset.nodraft = 1;
  wrap.innerHTML = `<div class="box"><button class="x" title="Закрыть">×</button>
    <h2>Русские голоса из библиотеки ElevenLabs</h2>
    <p class="muted small">Это носители русского языка, поэтому ударения и интонации у них естественные. Нажмите ▶, чтобы послушать. Кнопка «Выбрать» добавит голос в ваш аккаунт ElevenLabs и поставит его. После этого не забудьте нажать «Сохранить».</p>
    <div class="row" style="gap:8px;margin:10px 0">
      <select data-f="gender" style="width:auto"><option value="">Любой пол</option><option value="male">👨 Мужские</option><option value="female">👩 Женские</option></select>
      <select data-f="use_case" style="width:auto"><option value="">Для чего угодно</option><option value="narrative_story">Рассказы и истории</option><option value="informative_educational">Обучающие, научпоп</option><option value="social_media">Соцсети, шортсы</option><option value="entertainment_tv">ТВ, развлечения</option><option value="characters_animation">Персонажи, мультфильмы</option><option value="advertisement">Реклама</option></select>
      <input data-f="search" placeholder="поиск: низкий, спокойный, диктор…" style="width:240px">
      <button class="sm" data-go>Найти</button></div>
    <div data-list>Загружаю…</div><audio data-audio></audio>
    <div class="row" style="margin-top:10px"><button class="sm" data-more style="display:none">Показать ещё</button></div></div>`;
  document.body.appendChild(wrap);
  const close = () => { wrap.querySelector("[data-audio]").pause(); wrap.remove(); };
  wrap.querySelector(".x").onclick = close;
  wrap.addEventListener("click", e => { if (e.target === wrap) close(); });
  const list = wrap.querySelector("[data-list]"), more = wrap.querySelector("[data-more]");
  let page = 0, items = [];
  const f = k => wrap.querySelector(`[data-f="${k}"]`).value;
  const draw = () => {
    list.innerHTML = items.length ? items.map((v, i) => `<div class="vrow">
      <button class="sm" data-play="${i}" ${v.preview_url ? "" : "disabled"}>▶</button>
      <div class="grow"><b>${esc(v.name)}</b> <span class="muted small">${KIND[v.kind || "other"].split(" ")[0]} ${esc(v.description)}${v.users ? ` · выбрали ${v.users.toLocaleString("ru")} чел.` : ""}</span>
        ${v.about ? `<div class="muted small">${esc(v.about)}</div>` : ""}</div>
      <button class="sm ${v.added ? "" : "primary"}" data-pick="${i}">${v.added ? "✓ Уже у вас · выбрать" : "Выбрать"}</button></div>`).join("")
      : `<p class="muted">Ничего не нашлось. Попробуйте убрать фильтры.</p>`;
    list.querySelectorAll("[data-play]").forEach(b => b.onclick = () => { const a = wrap.querySelector("[data-audio]"); a.src = items[+b.dataset.play].preview_url; a.play(); });
    list.querySelectorAll("[data-pick]").forEach(b => b.onclick = async () => {
      const v = items[+b.dataset.pick]; b.disabled = true; b.textContent = "Добавляю…";
      try {
        const r = await api("/api/voices/library/add", { json: { owner: v.owner, voice_id: v.voice_id, name: v.name } });
        await loadVoices(true).catch(() => null);
        onPick(r.voice_id, v.name); close(); toast(`Голос «${v.name}» выбран. Нажмите «Сохранить».`);
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = "Выбрать"; }
    });
  };
  const load = async (reset) => {
    if (reset) { page = 0; items = []; list.innerHTML = "Загружаю…"; }
    try {
      const q = new URLSearchParams({ gender: f("gender"), use_case: f("use_case"), search: f("search"), page });
      const d = await api(`/api/voices/library?${q}`);
      items = items.concat(d.voices); draw(); more.style.display = d.has_more ? "" : "none";
    } catch (e) { list.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
  };
  wrap.querySelector("[data-go]").onclick = () => load(true);
  wrap.querySelectorAll("select[data-f]").forEach(s => s.onchange = () => load(true));
  wrap.querySelector('[data-f="search"]').onkeydown = e => { if (e.key === "Enter") load(true); };
  more.onclick = () => { page++; load(false); };
  load(true);
}
function pickIntoSelect(sel, vid, name) {
  const opt = sel.querySelector(`option[value="${CSS.escape(vid)}"]`);
  if (!opt) sel.insertAdjacentHTML("beforeend", `<option value="${esc(vid)}" data-name="${esc(name)}">${esc(name)}</option>`);
  else if (!opt.dataset.name) { opt.dataset.name = name; opt.textContent = name; }
  sel.value = vid; sel.dispatchEvent(new Event("change", { bubbles: true }));
}

const ICONS = ["🎬", "🏛️", "🕯️", "🔍", "⚽", "🚀", "🏆", "🐉", "📜", "👁️", "💰", "🧠", "🌍", "⚔️", "🎵", "🍳", "🐾", "💡"];

async function viewProjects(id) {
  if (id) return viewProject(id);
  const projs = await api("/api/projects");
  const niches = await api("/api/niches").catch(() => META.presets);
  main.innerHTML = `<h1>Проекты и ниши</h1>
  <p class="muted">Проект — это ниша со своими готовыми настройками: язык, тема канала, стиль картинок, голос, анимация, субтитры. Ролики каждого проекта хранятся отдельно. При запуске ролика проект выбирается одной кнопкой.</p>
  ${projs.length ? `<h2>Мои проекты</h2><div class="pgrid">${projs.map(p => `<a class="pcard" href="#/projects/${p.id}">
      <div class="pic">${esc(p.icon)}</div><div><b>${esc(p.name)}</b>
      <div class="muted small">${esc(META.languages[p.language] || p.language)} · роликов: ${p.count}</div></div>
      <b class="pdel" data-del="${p.id}" data-count="${p.count}" title="Удалить проект">×</b></a>`).join("")}</div>` : ""}
  <div class="card"><h2>Новый проект из ниши</h2>
    <p class="muted small">Выберите нишу: все настройки подставятся сами, потом их можно поменять. Если проект с этой нишей уже есть, откроется он, а не новая копия. Ненужную нишу уберите крестиком в углу: готовые ниши скрываются (их можно вернуть внизу), ваши удаляются. Проекты при этом не трогаются. Свой проект можно сделать нишей кнопкой «⭐ Сохранить как свою нишу» в проекте: тогда он появится здесь с пометкой «моя ниша», и из него можно быстро делать новые проекты.</p>
    <div class="pgrid">${Object.entries(niches).filter(([k, p]) => !p.hidden).sort((a, b) => (b[1].mine ? 1 : 0) - (a[1].mine ? 1 : 0)).map(([k, p]) => `<button class="pcard" data-preset="${k}"><div class="pic">${esc(p.icon)}</div><div><b>${esc(p.name)}</b><div class="muted small">${p.mine ? "⭐ моя ниша · " : ""}${p.language === "ru" ? "на русском" : "на английском"}${projs.some(x => x.preset === k) ? " · ✓ уже есть" : ""}</div></div><b class="pdel" data-ndel="${k}" data-mine="${p.mine ? 1 : ""}" title="Убрать из списка ниш">×</b></button>`).join("")}
      <button class="pcard" data-preset="custom"><div class="pic">＋</div><div><b>Свой проект</b><div class="muted small">с нуля</div></div></button></div>
    ${Object.values(niches).some(p => p.hidden) ? `<details style="margin-top:14px"><summary class="muted">Скрытые ниши (${Object.values(niches).filter(p => p.hidden).length})</summary>
      <div class="row" style="gap:6px;margin-top:8px">${Object.entries(niches).filter(([k, p]) => p.hidden).map(([k, p]) => `<button class="sm" data-nshow="${k}" title="Вернуть в список">${esc(p.icon)} ${esc(p.name)} · вернуть</button>`).join("")}</div></details>` : ""}
  </div>`;
  main.querySelectorAll("[data-del]").forEach(x => x.onclick = async e => {
    e.preventDefault(); e.stopPropagation();
    const n = +x.dataset.count;
    if (!confirm(n ? `Удалить проект? Его ролики (${n}) останутся, они перейдут в «Без проекта».` : "Удалить проект?")) return;
    await api(`/api/projects/${x.dataset.del}`, { method: "DELETE" }).catch(err => toast(err.message, true));
    const tabs = getTabs().filter(t => t.hash !== `#/projects/${x.dataset.del}`); lsSet("ay_tabs", JSON.stringify(tabs));
    viewProjects(); renderTabs();
  });
  main.querySelectorAll("[data-ndel]").forEach(x => x.onclick = async e => {
    e.preventDefault(); e.stopPropagation();
    if (x.dataset.mine) {
      if (!confirm("Удалить вашу нишу из списка? Проекты, созданные из неё, останутся.")) return;
      await api(`/api/niches/${x.dataset.ndel}`, { method: "DELETE" });
    } else {
      await api(`/api/niches/${x.dataset.ndel}/hidden`, { json: { hidden: true } });
      toast("Ниша скрыта. Вернуть её можно внизу, в «Скрытые ниши».");
    }
    viewProjects();
  });
  main.querySelectorAll("[data-nshow]").forEach(x => x.onclick = async () => {
    await api(`/api/niches/${x.dataset.nshow}/hidden`, { json: { hidden: false } }); viewProjects();
  });
  main.querySelectorAll("[data-preset]").forEach(b => b.onclick = async () => {
    const have = projs.find(x => x.preset === b.dataset.preset && b.dataset.preset !== "custom");
    if (have) { toast(`Проект «${have.name}» уже есть, открываю его`); location.hash = `#/projects/${have.id}`; return; }
    const p = await api("/api/projects", { json: { preset: b.dataset.preset } }).catch(e => toast(e.message, true));
    if (p) { toast("Проект создан"); location.hash = `#/projects/${p.id}`; }
  });
}

async function viewProject(id) {
  const projs = await api("/api/projects");
  const p = projs.find(x => x.id === id);
  if (!p) { main.innerHTML = `<div class="banner bad">Проект не найден. <a href="#/projects">К списку проектов</a></div>`; return; }
  const yt = await api("/api/youtube").catch(() => ({ channels: [] }));
  const gs = await api("/api/settings").catch(() => ({}));
  const allChars = await api("/api/characters").catch(() => []);
  const allItems = await api("/api/items").catch(() => []);
  const opt = (v, cur) => String(v) === String(cur) ? "selected" : "";
  const sel = (key, options) => `<select data-q="${key}">${options.map(([v, n]) => `<option value="${esc(v)}" ${opt(v, p[key])}>${esc(n)}</option>`).join("")}</select>`;
  main.innerHTML = `<div class="row"><h1 class="grow">${esc(p.icon)} ${esc(p.name)}</h1>
    <a class="btn" href="#/jobs" id="pj-jobs">Ролики проекта (${p.count})</a>
    <a class="btn primary" href="#/new" id="pj-new">＋ Новый ролик в проекте</a></div>
  <div class="card">
    <div class="grid2"><div>
      <label class="f">Название</label><input type="text" data-q="name" value="${esc(p.name)}">
    </div><div>
      <label class="f">Значок</label><input type="text" data-q="icon" value="${esc(p.icon)}" style="max-width:90px">
      <div class="row" style="gap:4px;margin-top:6px">${ICONS.map(i => `<button class="sm" data-icon="${i}">${i}</button>`).join("")}</div>
    </div></div>
    <label class="f">Язык сценария, озвучки и субтитров <span class="hint">Для русского языка выберите ниже русскоязычный голос (в ElevenLabs есть фильтр по языку).</span></label>
    ${sel("language", Object.entries(META.languages))}
    <label class="f">Формат роликов</label>
    ${sel("format", [["long", "Видео + шортс"], ["short", "Только шортс (вертикальные до 60 секунд)"]])}
    <label class="f">Ниша и тематика канала <span class="hint">Можно по-русски. Чем конкретнее, тем лучше темы.</span></label>
    <textarea data-q="niche">${esc(p.niche)}</textarea>
    <label class="f">Стиль картинок <span class="hint">Пусто — как в общих настройках. По-английски.</span></label>
    <textarea data-q="image_style">${esc(p.image_style)}</textarea>
    <div class="row" style="margin-top:6px">${Object.entries(STYLE_PRESETS).map(([k, v]) => `<button class="sm" data-style="${k}" title="${esc(v.desc)}">${esc(v.name)}</button>`).join("")}</div>
    <div class="grid2"><div>
      <label class="f">Голос <span class="hint">Голоса разделены на подходящие для русской и для английской аудитории. Сверху — под язык проекта.</span></label>
      <select data-q="voice_id" id="pj-voice"><option value="">Как в общих настройках (${esc(gs.elevenlabs_voice_name || "не выбран")})</option>${p.voice_id ? `<option value="${esc(p.voice_id)}" selected>${esc(p.voice_name || p.voice_id)}</option>` : ""}</select>
      <div class="row" style="margin-top:6px"><button class="sm" id="pj-voice-prev">▶ Образец</button><button class="sm primary" id="pj-voice-lib">🇷🇺 Подобрать русский голос</button><audio id="pj-voice-audio"></audio></div>
      <input type="text" id="pj-voice-id" placeholder="…или вставьте ID голоса из ElevenLabs" style="margin-top:6px">
    </div><div>
      <label class="f">Канал YouTube по умолчанию</label>
      ${sel("channel_id", [["", "Не выбран"], ...yt.channels.map(c => [c.id, c.title])])}
    </div></div>
    <label class="f">Как звучат персонажи <span class="hint">Работает для персонажей, у которых в разделе «Персонажи» выбран свой голос.</span></label>
    ${sel("voice_mode", [["mixed", "Рассказчик + реплики героев их голосами"], ["dialogue", "Мультик: в основном диалоги героев"], ["host", "Герой-ведущий: весь ролик рассказывает главный герой своим голосом"], ["narrator", "Только рассказчик (герои не говорят сами)"]])}
    <h3 id="pj-voice-h">Озвучка проекта</h3>
    <p class="muted small">Пустые поля берутся из общих настроек. Для русского языка нажмите «Настройки для русского», для шортсов — «Под шортс» (бодрее и чуть быстрее).</p>
    <div class="grid2"><div>
      <label class="f">Модель озвучки</label>${sel("tts_model", [["", "Как в общих настройках"], ...META.tts_models.map(m => [m.id, m.name])])}
      <label class="f">Стабильность (0–1) <span class="hint">меньше — эмоциональнее</span></label><input type="number" step="0.05" min="0" max="1" data-q="voice_stability" value="${esc(p.voice_stability)}">
      <label class="f">Похожесть на оригинал (0–1)</label><input type="number" step="0.05" min="0" max="1" data-q="voice_similarity" value="${esc(p.voice_similarity)}">
    </div><div>
      <label class="f">Выразительность (0–1)</label><input type="number" step="0.05" min="0" max="1" data-q="voice_style" value="${esc(p.voice_style)}">
      <label class="f">Скорость (0.7–1.2)</label><input type="number" step="0.01" min="0.7" max="1.2" data-q="voice_speed" value="${esc(p.voice_speed)}">
      <div class="row" style="margin-top:14px"><button class="sm" id="pj-ru-voice">Настройки для русского</button><button class="sm" id="pj-short-voice">Под шортс</button><button class="sm" id="pj-test-voice">▶ Проверить голос проекта</button></div>
      <audio id="pj-audio" controls style="display:none;margin-top:8px;width:100%"></audio>
    </div></div>
    <div class="grid2"><div>
      <div class="pj-long"><label class="f">Длительность, минут</label><input type="number" data-q="duration_min" min="1" max="30" value="${esc(p.duration_min)}"></div>
      <div class="pj-short"><label class="f">Длина шортса, секунд <span class="hint">15–180. Лучше всего заходят 30–60</span></label><input type="number" data-q="short_seconds" min="15" max="180" step="5" value="${esc(p.short_seconds || 50)}"></div>
      <label class="f">Анимация сцен <span class="hint">платно, считается за каждую оживлённую сцену</span></label>${sel("anim_mode", animModes())}
      <div id="pj-anim-n"><label class="f">Сколько сцен оживлять <span class="hint">Конкретные сцены можно выбрать на странице ролика</span></label><input type="number" data-q="anim_count" min="1" max="60" value="${esc(p.anim_count || 3)}"></div>
      <div id="pj-anim-est" class="muted small" style="margin-top:4px"></div>
      <label class="f">Музыка по умолчанию <span class="hint">В «Новом ролике» можно поменять для каждого ролика</span></label>${sel("music_mode", Object.entries(META.music_modes))}
      <label class="f">Эффекты</label>${sel("fx_mode", Object.entries(META.fx_modes))}
      <label class="f">Призыв на шортсах <span class="hint">анимированные кнопки «Подписаться» и «Нравится» в стиле YouTube во второй половине каждого шортса</span></label>${sel("cta_short", Object.entries(META.cta_modes))}
      <label class="f">Язык кнопок призыва</label>${sel("cta_lang", Object.entries(META.cta_langs))}
    </div><div>
      <label class="check pj-long" style="margin-top:38px"><input type="checkbox" data-q="make_short" ${p.make_short ? "checked" : ""}> Делать шортс</label>
      <label class="check pj-long"><input type="checkbox" data-q="anim_short" ${p.anim_short ? "checked" : ""}> Оживлять шортс</label>
      <label class="check"><input type="checkbox" data-q="anim_pause" ${p.anim_pause ? "checked" : ""}> Пауза перед анимацией: выбирать сцены самому</label>
      <label class="check pj-long"><input type="checkbox" data-q="subs_video" ${p.subs_video ? "checked" : ""}> Субтитры на длинном видео</label>
      <label class="check"><input type="checkbox" data-q="subs_short" ${p.subs_short ? "checked" : ""}> <span id="pj-subs-l">Субтитры на шортсе</span></label>
      <label class="check"><input type="checkbox" data-q="made_for_kids" ${p.made_for_kids ? "checked" : ""}> Ролики для детей <span class="muted small">(сказочный добрый тон; на YouTube ставится отметка «для детей», по закону это обязательно для детского контента)</span></label>
      <label class="check"><input type="checkbox" data-q="same_place" ${p.same_place !== false ? "checked" : ""}> Каждое место выглядит одинаково во всех своих кадрах <span class="muted small">(сначала рисуется каждое место действия, и все сцены там рисуются по этому образцу; нужен ключ fal.ai, +1 картинка на каждое место)</span></label>
      <label class="check"><input type="checkbox" data-q="fiction" ${p.fiction ? "checked" : ""}> Выдуманные истории <span class="muted small">(мультики, сказки, притчи: сюжеты придумываются, а не берутся из реальных событий)</span></label>
    </div></div>
    <div class="row" style="margin-top:16px"><button class="primary" id="pj-save">Сохранить проект</button><button id="pj-tpl" title="Проект появится в списке ниш, из него можно будет быстро создавать новые проекты с такими же настройками">⭐ Сохранить как свою нишу</button><span class="grow"></span><button class="danger" id="pj-del">Удалить проект</button></div>
  </div>
  <div class="card"><h2>Персонажи проекта</h2>
    <p class="muted small">Отметьте героев, которые будут выбраны сами при запуске ролика в этом проекте (при запуске их можно поменять). Сами персонажи хранятся в разделе <a href="#/characters">«Персонажи»</a> слева: там их добавляют, рисуют и загружают портреты.</p>
    ${allChars.length ? `<div class="row" id="pj-chars">${allChars.map(c => `<button class="cbtn ${(p.characters || []).includes(c.id) ? "on" : ""}" data-pchar="${c.id}" title="${esc(c.role)}">${charIcon(c, 48)}<span>${esc(c.name)}</span></button>`).join("")}</div>`
      : `<p class="muted">Персонажей пока нет. <a href="#/characters">Добавить персонажа</a></p>`}
  </div>
  <div class="card"><h2>Предметы проекта</h2>
    <p class="muted small">Отметьте товары и вещи, которые будут выбраны сами при запуске ролика в этом проекте (при запуске их можно поменять). Сами предметы с фото хранятся в разделе <a href="#/items">«Предметы»</a> слева.</p>
    ${allItems.length ? `<div class="row" id="pj-items">${allItems.map(it => `<button class="cbtn ${(p.items || []).includes(it.id) ? "on" : ""}" data-pitem="${it.id}" title="${esc(it.kind)}">${itemIcon(it, 48)}<span>${esc(it.name)}</span></button>`).join("")}</div>`
      : `<p class="muted">Предметов пока нет. <a href="#/items">Добавить предмет</a></p>`}
  </div>
  <div class="card" id="pj-golden"><h2>⭐ Копилка удачных сценариев</h2>
    <p class="muted small">Сценарии этого проекта, которые сработали. Перед каждым новым роликом проекта сценарист изучает последние 6 (отдельно для видео и для шортсов): какие темы, первые фразы, ритм и повороты здесь заходят. Добавляйте кнопкой «⭐ В копилку удачных» на странице ролика. Ниже программа сама подсказывает хиты по статистике YouTube.</p>
    <div id="pj-golden-list" class="muted small">Загружаю…</div>
  </div>
  <div class="card"><h2>🔎 Поиск на YouTube</h2>
    <p class="muted small">Название, описание и теги программа составляет сама под поиск: главная фраза темы в начале названия, описание с ключевыми словами темы и ниши, 25–35 тегов (точные запросы по теме, массовые теги с огромной аудиторией, теги ниши и героя). Здесь можно добавить свои постоянные теги и фразы, которые нужны в каждом ролике проекта: через запятую. Они всегда попадут в теги, а подходящие и в описание.</p>
    <textarea data-q="seo_ru" rows="3" placeholder="Например: таксист, жизненно, юмор, горькая правда, бритва, мужской уход">${esc(p.seo_ru || "")}</textarea>
    <p class="muted small">Не забудьте нажать «Сохранить проект».</p>
  </div>
  <div class="card"><h2>🎯 Образцы удачных шортсов</h2>
    <p class="muted small">Вставьте сюда тексты шортсов, которые вам нравятся и набирают просмотры: что говорят в ролике, от первой фразы до последней. Можно описать своими словами, что происходит и в чём поворот. Каждый образец отделяйте пустой строкой или чертой. Сценарист шортсов изучит, как они цепляют в первую секунду, где поворот и как бьёт концовка, и будет писать на том же уровне. Сюжеты и фразы он не копирует. Для длинных роликов это поле не используется.</p>
    <textarea data-q="short_examples" rows="8" placeholder="Например:&#10;«Мой хозяин решил побриться тёркой для сыра…» (дальше весь текст ролика)&#10;———&#10;Второй образец…">${esc(p.short_examples || "")}</textarea>
    <p class="muted small">Не забудьте нажать «Сохранить проект».</p>
  </div>
  <div class="card"><h2>Сериал</h2>
    <p class="muted small">Включите, если ролики проекта — серии одной истории. Каждая серия получает номер, а после сценария программа сама записывает, что в ней произошло. Следующая серия читает библию сериала, план сезона и краткое содержание прошлых серий, поэтому продолжает сюжет, помнит, что пережили герои, и заканчивается крючком на следующую серию.</p>
    <label class="check"><input type="checkbox" data-q="series" ${p.series ? "checked" : ""}> Это сериал</label>
    <label class="f">Библия сериала <span class="hint">По-русски. О чём сериал, правила мира, герои и как они меняются, главный вопрос сезона, тон, фирменные фразы.</span></label>
    <textarea data-q="series_bible" rows="8">${esc(p.series_bible || "")}</textarea>
    <label class="f">План сезона <span class="hint">Одна строка — одна серия: «1. Название — что происходит». Если тема ролика не задана, серия берёт свою строку отсюда. Можно оставить пустым, тогда сюжет придумывается по ходу.</span></label>
    <textarea data-q="season_plan" rows="8">${esc(p.season_plan || "")}</textarea>
    <div class="row" style="margin-top:8px"><button id="pj-series-plan">✨ Придумать библию и план сезона</button>
      <span class="muted small">На чём основывается: ниша проекта, персонажи проекта (отмечены ниже), всё, что уже написано в библии, и вышедшие серии. Писать заранее ничего не обязательно. Но если у вас есть идея, впишите её в библию хоть одной строкой (например: «мошенник Лис обманывает весь город, пока его не находит прошлое»), и программа разовьёт именно её. Результат появится в полях выше, поправьте и нажмите «Сохранить проект».</span></div>
    <h3>Вышедшие серии</h3>
    <div id="episodes" class="muted small">Загружаю…</div>
  </div>
  <div class="card" id="pj-stats"><h2>📈 Что работает на канале</h2><p class="muted small">Загружаю…</p></div>
  </div>`;
  const animEst = async () => {
    const box = $("#pj-anim-est"); if (!box) return;
    const q = k => main.querySelector(`[data-q="${k}"]`);
    const fmtP = q("format").value, mode = q("anim_mode").value, animS = fmtP === "long" && q("anim_short").checked;
    $("#pj-anim-n").style.display = ["hook", "key"].includes(mode) ? "" : "none";
    if (mode === "off" && !animS) { box.textContent = "Анимация выключена: бесплатно, в видео будет плавное движение камеры по картинкам."; return; }
    try {
      const e = await api(`/api/estimate?minutes=${q("duration_min").value}&short=${q("make_short").checked}&anim_mode=${mode}&anim_short=${animS}&anim_count=${q("anim_count").value}&language=${q("language")?.value || "ru"}&format=${fmtP}&short_seconds=${q("short_seconds")?.value || 50}`);
      const m = META.video_models.find(x => x.id === (SETTINGS_CACHE || {}).video_model) || META.video_models.find(x => x.id === "kling25");
      box.innerHTML = `⚠️ Анимация в одном ролике обойдётся примерно в <b>$${e.anim_cost}</b> (~$${m.clip} за каждую оживлённую сцену).`;
    } catch (err) { }
  };
  ["anim_mode", "anim_short", "anim_count", "format", "duration_min", "short_seconds", "make_short"].forEach(k => main.querySelector(`[data-q="${k}"]`)?.addEventListener("change", animEst));
  animEst();
  const collect = () => {
    const out = {};
    main.querySelectorAll("[data-q]").forEach(i => out[i.dataset.q] = i.type === "checkbox" ? i.checked : i.value);
    const vs = $("#pj-voice"); out.voice_name = vs.value ? (vs.options[vs.selectedIndex].dataset.name || vs.options[vs.selectedIndex].text) : "";
    const manual = $("#pj-voice-id").value.trim();
    if (manual) { out.voice_id = manual; out.voice_name = `Свой голос (${manual.slice(0, 6)}…)`; }
    out.characters = [...main.querySelectorAll("[data-pchar].on")].map(b => b.dataset.pchar);
    out.items = [...main.querySelectorAll("[data-pitem].on")].map(b => b.dataset.pitem);
    return out;
  };
  main.querySelectorAll("[data-pchar]").forEach(b => b.onclick = async () => {
    b.classList.toggle("on");
    await api(`/api/projects/${id}`, { json: { characters: [...main.querySelectorAll("[data-pchar].on")].map(x => x.dataset.pchar) } })
      .then(() => toast("Персонажи проекта сохранены")).catch(e => toast(e.message, true));
  });
  main.querySelectorAll("[data-pitem]").forEach(b => b.onclick = async () => {
    b.classList.toggle("on");
    await api(`/api/projects/${id}`, { json: { items: [...main.querySelectorAll("[data-pitem].on")].map(x => x.dataset.pitem) } })
      .then(() => toast("Предметы проекта сохранены")).catch(e => toast(e.message, true));
  });
  const loadGolden = async () => {
    const box = $("#pj-golden-list"); if (!box) return;
    try {
      const g = await api(`/api/projects/${id}/golden`);
      const kindRu = k => k === "short" ? "шортс" : "видео";
      box.className = "";
      box.innerHTML = (g.items.length ? `<table>${g.items.slice().reverse().map(x => `<tr><td>${x.kind === "short" ? "📱" : "🎬"}</td><td><b>${esc(x.title_ru || x.title)}</b>${x.title_ru ? `<div class="en">${esc(x.title)}</div>` : ""}${x.note_ru ? `<div class="small">💬 ${esc(x.note_ru)}</div>` : ""}<details><summary class="muted small">Текст</summary><pre class="text">${esc(x.text)}</pre></details></td><td class="small muted" style="white-space:nowrap">${kindRu(x.kind)}${x.views != null ? `<br>${x.views.toLocaleString("ru")} просм.` : ""}<br>${new Date(x.added * 1000).toLocaleDateString("ru")}</td><td><a href="#/job/${x.job}" class="small">ролик</a> <button class="sm danger" data-gdel="${x.id}">×</button></td></tr>`).join("")}</table>`
        : `<p class="muted">Пока пусто. Откройте удачный ролик этого проекта и нажмите «⭐ В копилку удачных».</p>`)
        + (g.suggest.length ? `<h3>Хиты по статистике, которых нет в копилке</h3><table>${g.suggest.map(x => `<tr><td>${esc(x.title)}</td><td class="small">${x.views.toLocaleString("ru")} просм. (обычно ${x.median.toLocaleString("ru")})${x.avg_view_pct ? `, досматривают ${x.avg_view_pct}%` : ""}</td><td><button class="sm" data-gadd="${x.job}:${x.kind}">⭐ Добавить</button></td></tr>`).join("")}</table>` : "");
      box.querySelectorAll("[data-gdel]").forEach(b => b.onclick = async () => { if (!confirm("Убрать из копилки?")) return; await api(`/api/projects/${id}/golden/${b.dataset.gdel}`, { method: "DELETE" }); loadGolden(); });
      box.querySelectorAll("[data-gadd]").forEach(b => b.onclick = async () => { const [jid, kind] = b.dataset.gadd.split(":"); await api(`/api/jobs/${jid}/golden`, { json: { kind, note: "хит по статистике" } }).catch(e => toast(e.message, true)); loadGolden(); });
    } catch (e) { box.textContent = e.message; }
  };
  loadGolden();
  const langSel = main.querySelector('[data-q="language"]'), fmtSel = main.querySelector('[data-q="format"]');
  const fillVoices = async () => {
    const vsel = $("#pj-voice"); const cur = vsel.value;
    try { const vs = await loadVoices(); vsel.innerHTML = vsel.options[0].outerHTML + voiceOptions(vs, cur, langSel.value); vsel.value = cur; }
    catch (e) { vsel.title = "Список голосов не загрузился: " + e.message; }
  };
  fillVoices(); langSel.addEventListener("change", fillVoices);
  $("#pj-voice-lib").onclick = () => openVoiceLibrary((vid, name) => { $("#pj-voice-id").value = ""; pickIntoSelect($("#pj-voice"), vid, name); });
  $("#pj-voice-prev").onclick = () => {
    const v = (VOICES || []).find(x => x.voice_id === ($("#pj-voice").value || gs.elevenlabs_voice_id));
    if (!v || !v.preview_url) return toast("Для этого голоса нет образца. Нажмите «Проверить голос проекта».", true);
    const a = $("#pj-voice-audio"); a.src = v.preview_url; a.play();
  };
  const applyFmt = () => {
    const short = fmtSel.value === "short";
    main.querySelectorAll(".pj-long").forEach(el => el.style.display = short ? "none" : "");
    main.querySelectorAll(".pj-short").forEach(el => el.style.display = short ? "" : "none");
    $("#pj-subs-l").textContent = short ? "Субтитры" : "Субтитры на шортсе";
    $("#pj-voice-h").textContent = short ? "Озвучка проекта (шортсы)" : "Озвучка проекта";
  };
  fmtSel.addEventListener("change", applyFmt); applyFmt();
  $("#pj-short-voice").onclick = () => {
    const set = (k, v) => { main.querySelector(`[data-q="${k}"]`).value = v; };
    set("voice_stability", 0.35); set("voice_similarity", 0.8); set("voice_style", 0.35); set("voice_speed", 1.08);
    toast("Подставлено: бодрее и чуть быстрее, как принято в шортсах. Нажмите «Сохранить проект».");
  };
  main.querySelectorAll("[data-icon]").forEach(b => b.onclick = () => { main.querySelector('[data-q="icon"]').value = b.dataset.icon; });
  main.querySelectorAll("[data-style]").forEach(b => b.onclick = () => { main.querySelector('[data-q="image_style"]').value = STYLE_PRESETS[b.dataset.style].text; });
  $("#pj-save").onclick = async () => {
    await api(`/api/projects/${id}`, { json: collect() }).then(() => { draftClear(); draftBaseReset(); toast("Проект сохранён"); }).catch(e => toast(e.message, true));
  };
  $("#pj-tpl").onclick = async () => {
    try {
      await api(`/api/projects/${id}`, { json: collect() }); draftClear(); draftBaseReset();
      await api(`/api/projects/${id}/template`, { method: "POST" });
      toast("Готово: проект появился в «Проекты и ниши» → «Новый проект из ниши» с пометкой «моя ниша»");
    } catch (e) { toast(e.message, true); }
  };
  $("#pj-del").onclick = async () => {
    if (!confirm("Удалить проект? Его ролики останутся, они перейдут в «Без проекта».")) return;
    await api(`/api/projects/${id}`, { method: "DELETE" });
    location.hash = "#/projects";
  };
  $("#pj-ru-voice").onclick = () => {
    const set = (k, v) => { main.querySelector(`[data-q="${k}"]`).value = v; };
    set("tts_model", "eleven_multilingual_v2"); set("voice_stability", 0.45); set("voice_similarity", 0.8); set("voice_style", 0.15); set("voice_speed", 1.0);
    toast("Подставлено. Не забудьте выбрать русскоязычный голос и сохранить проект.");
  };
  $("#pj-test-voice").onclick = async () => {
    try {
      await api(`/api/projects/${id}`, { json: collect() });
      toast("Озвучиваю пробную фразу…");
      const b = await api("/api/test/tts", { json: { project_id: id } });
      const a = $("#pj-audio"); a.style.display = ""; a.src = URL.createObjectURL(b); a.play();
    } catch (e) { toast(e.message, true); }
  };
  $("#pj-jobs").onclick = () => lsSet("ay_jobs_filter", id);
  $("#pj-series-plan").onclick = async () => {
    const b = $("#pj-series-plan"); b.disabled = true; b.textContent = "Придумываю…";
    try {
      const c = collect();
      const r = await api(`/api/projects/${id}/series/plan`, { json: { niche: c.niche, series_bible: c.series_bible, characters: c.characters, count: 10 } });
      main.querySelector('[data-q="series_bible"]').value = r.series_bible;
      main.querySelector('[data-q="season_plan"]').value = r.season_plan;
      main.querySelector('[data-q="series"]').checked = true;
      toast("Готово. Проверьте и нажмите «Сохранить проект».");
    } catch (e) { toast(e.message, true); }
    b.disabled = false; b.textContent = b.dataset.label || "✨ Придумать библию и план сезона";
  };
  renderEpisodes(id);
  renderStats(id);
  $("#pj-new").onclick = async e => {
    e.preventDefault();
    await api(`/api/projects/${id}`, { json: collect() }).catch(() => { });
    lsSet("ay_proj", id); location.hash = "#/new";
  };
}

async function renderStats(pid) {
  const box = $("#pj-stats"); if (!box) return;
  const r = await api(`/api/projects/${pid}/stats`).catch(() => null);
  if (!r) { box.innerHTML = `<h2>📈 Что работает на канале</h2><p class="muted">Не удалось загрузить статистику.</p>`; return; }
  const L = r.lessons || {}, rules = L.rules || [];
  const fmtN = n => (n || 0).toLocaleString("ru");
  let h = `<h2>📈 Что работает на канале</h2>
    <p class="muted small">Программа раз в 6 часов берёт с YouTube статистику опубликованных роликов этого проекта: просмотры и сколько процентов ролика досматривают. Когда наберётся ${r.ready.need} роликов старше ${r.min_age_days} дней, она сравнит лучшие и худшие и запишет правила, что работает. Сценарист учитывает их в каждом новом ролике проекта. Любое правило можно выключить или удалить.</p>`;
  if (!r.channels) h += `<div class="banner info small">Подключите канал в <a href="#/settings/youtube">Настройках → YouTube</a>. Ролики, загруженные вручную, тоже можно учитывать: вставьте их ссылку на странице ролика.</div>`;
  if (r.error) h += `<div class="banner warn small">${esc(r.error)}</div>`;
  h += `<div class="row" style="margin:8px 0"><button class="sm" id="st-refresh">↻ Обновить статистику</button>
    <button class="sm ${r.ready.ok ? "primary" : ""}" id="st-learn" ${r.ready.ok ? "" : "disabled"}>✨ Сделать выводы сейчас</button>
    <span class="muted small">${r.ready.ok ? `Роликов для анализа: ${r.ready.videos}` : `Роликов со статистикой: ${r.ready.videos} из ${r.ready.need}`}${r.last_run ? ` · обновлено ${new Date(r.last_run * 1000).toLocaleString("ru")}` : ""}</span></div>`;
  if (L.summary_ru) h += `<div class="banner ok small"><b>Вывод:</b> ${esc(L.summary_ru)}<br><span class="muted">Сделан ${new Date(L.learned_at * 1000).toLocaleString("ru")} по ${L.videos} роликам</span></div>`;
  if (rules.length) h += `<h3>Правила для сценариста</h3>` + rules.map(x => `<div class="citem" style="align-items:flex-start;${x.off ? "opacity:.5" : ""}">
      <div class="grow"><b>${esc(x.rule_ru)}</b> <span class="muted small">(${{ high: "уверенно", medium: "скорее да", low: "пока догадка" }[x.confidence] || ""})</span>
      <div class="muted small">${esc(x.evidence_ru || "")}</div></div>
      <button class="sm" data-roff="${x.id}" data-v="${x.off ? 0 : 1}">${x.off ? "Включить" : "Выключить"}</button>
      <button class="sm danger" data-rdel="${x.id}">×</button></div>`).join("");
  if (r.videos.length) h += `<h3>Опубликованные ролики</h3><table class="small"><tr><th>Ролик</th><th>Просмотры</th><th>Досматривают</th><th>Лайки</th><th>Дней</th></tr>
    ${r.videos.map(v => `<tr><td><a href="#/job/${v.job_id}">${esc(v.title || "без названия")}</a> <span class="muted">${v.kind === "shorts" ? "шортс" : "видео"}</span></td><td>${fmtN(v.views)}</td><td>${v.avg_view_pct != null ? v.avg_view_pct + "%" : "—"}</td><td>${fmtN(v.likes)}</td><td>${v.age_days}</td></tr>`).join("")}</table>`;
  box.innerHTML = h;
  $("#st-refresh").onclick = async e => {
    e.target.disabled = true; e.target.textContent = "Обновляю…";
    const res = await api("/api/stats/refresh", { method: "POST" }).catch(err => { toast(err.message, true); return null; });
    if (res) toast(res.errors && res.errors.length ? res.errors[0] : `Обновлено роликов: ${res.videos}`, !!(res.errors && res.errors.length));
    renderStats(pid);
  };
  $("#st-learn").onclick = async e => {
    e.target.disabled = true; e.target.textContent = "Анализирую…";
    await api(`/api/projects/${pid}/lessons`, { method: "POST" }).then(() => toast("Выводы готовы")).catch(err => toast(err.message, true));
    renderStats(pid);
  };
  box.querySelectorAll("[data-roff]").forEach(b => b.onclick = async () => { await api(`/api/projects/${pid}/lessons/${b.dataset.roff}`, { json: { off: b.dataset.v === "1" } }); renderStats(pid); });
  box.querySelectorAll("[data-rdel]").forEach(b => b.onclick = async () => { if (!confirm("Удалить это правило?")) return; await api(`/api/projects/${pid}/lessons/${b.dataset.rdel}`, { json: { delete: true } }); renderStats(pid); });
}

async function renderEpisodes(pid) {
  const box = $("#episodes"); if (!box) return;
  const r = await api(`/api/projects/${pid}/episodes`).catch(() => ({ episodes: [], next: 1 }));
  const planBtn = $("#pj-series-plan");
  if (planBtn && r.episodes.length) planBtn.dataset.label = planBtn.textContent = `✨ Придумать следующие 10 серий (с ${r.next}-й)`;
  box.innerHTML = (r.episodes.length ? r.episodes.map(e => `<div class="citem" style="align-items:flex-start">
      <div class="grow"><b>Серия ${e.num}. ${esc(e.title_ru || e.title)}</b>
        <div>${esc(e.summary_ru || e.summary || "")}</div>
        ${e.cliffhanger ? `<div class="muted small">Чем закончилась: ${esc(e.cliffhanger)}</div>` : ""}
        ${e.job_id ? `<a class="small" href="#/job/${e.job_id}">Открыть ролик</a>` : ""}</div>
      <button class="sm danger" data-ep="${e.num}" title="Следующие серии забудут её сюжет">Забыть</button></div>`).join("")
    : `<p>Пока ни одной. Сюжет серии запоминается сам, когда готов её сценарий.</p>`)
    + `<p class="muted small">Следующий ролик проекта станет серией ${r.next}.</p>`;
  box.querySelectorAll("[data-ep]").forEach(b => b.onclick = async () => {
    if (!confirm("Забыть эту серию? Следующие серии не будут знать, что в ней было.")) return;
    await api(`/api/projects/${pid}/episodes/${b.dataset.ep}`, { method: "DELETE" });
    renderEpisodes(pid);
  });
}

// ------------------------------------------------------------------ job page
const openStages = new Set();
let lastStagesKey = "";

async function viewJob(id) {
  lastStagesKey = "";
  main.innerHTML = `<div id="jhead"></div><div id="jstages"></div>
    <details class="stage" id="logbox"><summary><span class="ic">📜</span><span class="nm">Журнал работы</span></summary><div class="content"><pre class="log" id="jlog"></pre></div></details>`;
  const render = async () => {
    const d = await api(`/api/jobs/${id}`);
    renderHead(d);
    const key = JSON.stringify([d.stages, d.images, d.files, d.thumb, d.subs, d.anim, d.fx_mode, d.job.status, d.job.stage, d.job.error]);
    if (key !== lastStagesKey) { lastStagesKey = key; lastUpKey = ""; renderStages(d); }
    renderUploads(d);
    const log = $("#jlog"); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    if (log.textContent !== d.log) { log.textContent = d.log; if (atBottom) log.scrollTop = log.scrollHeight; }
  };
  await render();
  timer = setInterval(() => render().catch(() => { }), 2500);
}

function renderHead(d) {
  const j = d.job, u = d.usage || {};
  const cost = (u.llm_cost || 0) + (u.image_cost || 0) + (u.anim_cost || 0) + (u.music_cost || 0);
  const act = [];
  if (["running", "queued"].includes(j.status)) act.push(`<button data-act="pause">⏸ Пауза</button>`);
  if (j.status === "paused") act.push(`<button class="primary" data-act="resume">▶ Продолжить</button>`);
  if (j.status === "error") act.push(`<button class="primary" data-act="retry">↻ Попробовать ещё раз</button>`);
  const ideaWait = j.status === "review" && j.options.idea_pause && !j.options.idea_picked;
  const animWait = d.anim && d.anim.waiting;
  if (j.status === "review" && !ideaWait && !animWait) act.push(`<button class="primary" data-act="approve">✓ Сценарий хороший, продолжить</button>`);
  if (j.options.project_id && d.stages.review && j.status !== "running") act.push(`<button data-gold="video" title="Сценарист будет изучать этот текст перед новыми роликами проекта">⭐ ${d.format === "short" ? "Шортс" : "Сценарий"} в копилку удачных</button>`);
  if (j.options.project_id && d.format !== "short" && d.stages.short_script && j.status !== "running") act.push(`<button data-gold="short">⭐ Шортс в копилку</button>`);
  if (d.stages.images && j.status !== "running" && !j.options.translate_from && (j.options.language || "en") !== "en") act.push(`<button data-tr="1" title="Те же картинки и анимация, английский текст, голос, субтитры и описание">🌐 Английская версия</button>`);
  act.push(`<button data-open="1">📂 Открыть папку</button>`);
  act.push(`<button class="danger" data-act="delete">Удалить</button>`);
  $("#jhead").innerHTML = `
  <div class="card">
    <div class="row"><a href="#/jobs" class="muted small">← Все ролики</a></div>
    <h1 style="margin:6px 0 2px">${j.options.episode ? `Серия ${j.options.episode}. ` : ""}${esc(j.options.title_ru || j.title || j.topic || "Тема ещё выбирается…")}</h1>
    ${j.title ? `<div class="en">${esc(j.title)}</div>` : ""}
    <div class="row" style="margin:10px 0">${badge(j.status)}
      <span class="muted small">${j.status === "running" ? "Сейчас: " + esc(META.stages.find(s => s.id === j.stage)?.name || "") : ""}</span>
      <span class="grow"></span>
      <span class="muted small" title="Текст (ИИ): $${(u.llm_cost || 0).toFixed(2)} · Картинки: $${(u.image_cost || 0).toFixed(2)} (${u.images || 0} шт.) · Анимация: $${(u.anim_cost || 0).toFixed(2)}">Потрачено: ~$${cost.toFixed(2)} (картинки $${(u.image_cost || 0).toFixed(2)}${u.anim_cost ? `, анимация $${u.anim_cost.toFixed(2)}` : ""}${u.music_cost ? `, музыка $${u.music_cost.toFixed(2)}` : ""}, текст $${(u.llm_cost || 0).toFixed(2)})${u.tts_chars ? ` + ${Math.round(u.tts_chars).toLocaleString("ru")} символов ElevenLabs` : ""}</span>
    </div>
    ${j.status !== "done" ? `<div class="bar"><div style="width:${j.progress}%"></div></div>` : ""}
    ${j.status === "error" ? `<div class="banner bad">${nl(j.error)}</div>` : ""}
    ${j.options.translate_from ? `<div class="banner info">🌐 Английская версия ролика <a href="#/job/${esc(j.options.translate_from)}">${esc(j.options.translate_from)}</a>: те же картинки и анимация${(j.options.flipped || []).length ? ", кадры отражены и камера движется по-другому" : ""}, новый текст, голос, субтитры, описание и обложка.</div>` : ""}
    ${ideaWait ? `<div class="banner info">Темы готовы, программа ждёт вашего выбора. Откройте шаг «Выбор темы», отметьте понравившуюся тему и нажмите «Взять эту тему». Сценарий ещё не писался, деньги почти не потрачены.</div>` : ""}
    ${animWait ? `<div class="banner info">🎬 Картинки и озвучка готовы, программа ждёт вашего выбора анимации. Откройте шаг «Анимация сцен» ниже: программа уже отметила сцены, которые оживила бы сама. Добавьте или снимите любые галочки и нажмите «Оживить выбранные и продолжить». Перед оплатой покажется цена.</div>` : ""}
    ${j.status === "review" && !ideaWait && !animWait ? `<div class="banner info">Текст готов и проверен редактором. Программа остановилась и ждёт вас: сцены, картинки и озвучка ещё не делались. Прочитайте текст в шаге «Проверка редактором» → «Итоговый текст для озвучки». Если нужно, исправьте его там же и нажмите «Сохранить мой текст». Когда всё устраивает, нажмите «Сценарий хороший, продолжить». Или нажмите «Переделать» у шага «Текст сценария», чтобы ИИ написал заново.</div>` : ""}
    <div class="row" style="margin-top:12px">${act.join("")}</div>
  </div>`;
  $("#jhead").querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
    const a = b.dataset.act;
    if (a === "delete" && !confirm("Удалить этот ролик вместе со всеми файлами?")) return;
    await api(`/api/jobs/${j.id}/action`, { json: { action: a } }).catch(e => toast(e.message, true));
    if (a === "delete") location.hash = "#/jobs"; else lastStagesKey = "";
  });
  $("#jhead").querySelectorAll("[data-gold]").forEach(b => b.onclick = async () => {
    const note = prompt("Чем этот текст хорош? Например: «сильный первый кадр», «смешная концовка». Можно оставить пустым.", "");
    if (note === null) return;
    await api(`/api/jobs/${j.id}/golden`, { json: { kind: b.dataset.gold, note } })
      .then(() => toast("Добавлено в копилку удачных сценариев проекта")).catch(e => toast(e.message, true));
  });
  const tr = $("#jhead [data-tr]");
  if (tr) tr.onclick = () => openTranslate(j, d);
  $("#jhead [data-open]").onclick = () => api("/api/open", { json: { what: "job", id: j.id } }).catch(e => toast(e.message, true));
}

async function openTranslate(j, d) {
  const proj = d.project;
  const yt = await api("/api/youtube").catch(() => ({ channels: [] }));
  const scenes = ((d.stages.scenes || {}).scenes || []).length;
  const wrap = document.createElement("div");
  wrap.className = "vlib"; wrap.dataset.nodraft = 1;
  wrap.innerHTML = `<div class="box" style="max-width:560px"><button class="x" title="Закрыть">×</button>
    <h2>🌐 Английская версия</h2>
    <p class="muted small">Программа возьмёт картинки и анимацию этого ролика, переведёт текст по сценам на живой английский, озвучит выбранным голосом, наложит английские субтитры и сделает английские название, описание, теги и новую обложку. Русский ролик останется как был. Платится только перевод (центы), одна картинка обложки и символы ElevenLabs.</p>
    <label class="f">Голос для английской версии <span class="hint">им говорит рассказчик или ведущий; персонажи со своими голосами в диалогах говорят своими</span></label>
    <select data-v><option value="">Загружаю голоса…</option></select>
    <label class="f">Канал YouTube</label>
    <select data-ch><option value="">Без канала (загрузить вручную)</option>${yt.channels.map(c => `<option value="${c.id}">${esc(c.title)}</option>`).join("")}</select>
    <label class="check" style="margin-top:10px"><input type="checkbox" data-rs checked> Изменить вид кадров: отразить картинки и анимацию зеркально и повести камеру в другую сторону (бесплатно; кадры с товарами не отражаются, чтобы надписи не читались задом наперёд)</label>
    <label class="f">Перерисовать заново первые сцены (по тому же описанию, платно за каждую картинку)</label>
    <input type="number" data-rd min="0" max="${scenes}" value="0" style="width:100px">
    <div class="row" style="margin-top:14px"><button class="primary" data-go>Сделать английскую версию</button></div></div>`;
  document.body.appendChild(wrap);
  const close = () => wrap.remove();
  wrap.querySelector(".x").onclick = close;
  wrap.addEventListener("click", e => { if (e.target === wrap) close(); });
  const vsel = wrap.querySelector("[data-v]");
  const cur = (proj && proj.en_voice_id) || "";
  loadVoices().then(vs => { vsel.innerHTML = `<option value="">— выберите голос —</option>` + voiceOptions(vs, cur, "en"); vsel.value = cur; })
    .catch(e => { vsel.innerHTML = `<option value="">Голоса не загрузились: ${esc(e.message)}</option>`; });
  if (proj && proj.en_channel_id) wrap.querySelector("[data-ch]").value = proj.en_channel_id;
  wrap.querySelector("[data-go]").onclick = async () => {
    const o = vsel.options[vsel.selectedIndex];
    if (!vsel.value) return toast("Выберите голос", true);
    const r = await api(`/api/jobs/${j.id}/translate`, { json: { voice_id: vsel.value, voice_name: o.dataset.name || o.text,
      channel_id: wrap.querySelector("[data-ch]").value, restyle: wrap.querySelector("[data-rs]").checked,
      redraw: +wrap.querySelector("[data-rd]").value || 0 } }).catch(e => { toast(e.message, true); return null; });
    if (!r) return;
    close(); toast("Английская версия запущена"); location.hash = `#/job/${r.id}`;
  };
}

function stageIcon(sid, d) {
  const j = d.job;
  if (!d.enabled[sid]) return "—";
  if (d.stages[sid]) return "✅";
  if (j.stage === sid && j.status === "running") return "⏳";
  if (j.stage === sid && j.status === "error") return "❌";
  if (sid === "approval" && j.status === "review" && !(d.anim && d.anim.waiting)) return "👀";
  if (sid === "animate" && d.anim && d.anim.waiting) return "👀";
  return "○";
}

function renderStages(d) {
  const el = $("#jstages");
  el.querySelectorAll("details[data-sid]").forEach(x => { if (x.open) openStages.add(x.dataset.sid); else openStages.delete(x.dataset.sid); });
  const typed = el.querySelector("#final-text");  // keep the owner's unsaved edit when the page refreshes
  const keepText = typed && typed.value !== typed.defaultValue ? typed.value : null;
  const j = d.job;
  el.innerHTML = META.stages.map(st => {
    const sid = st.id, data = d.stages[sid];
    const enabled = d.enabled[sid];
    const cur = j.stage === sid && ["running", "error"].includes(j.status);
    let body = "";
    try { body = enabled ? (RENDER[sid] ? RENDER[sid](data, d) : "") : (d.format === "short" ? `<p class="muted">Режим «Только шортс»: основное видео и есть шортс.</p>` : `<p class="muted">Шортс не заказан для этого ролика.</p>`); }
    catch (e) { body = `<p class="muted">Не удалось показать: ${esc(e.message)}</p>`; }
    if (!data && enabled && !body) body = `<p class="muted">${cur ? "Выполняется…" : "Ещё не начато."}</p>`;
    const redo = enabled && data && sid !== "approval" ? `<button class="sm" data-redo="${sid}">↻ Переделать с этого шага</button>` : "";
    return `<details class="stage ${enabled ? "" : "off"} ${cur ? "cur" : ""}" data-sid="${sid}" ${openStages.has(sid) || (cur && j.status === "error") || (sid === "animate" && d.anim && d.anim.waiting) ? "open" : ""}>
      <summary><span class="ic">${stageIcon(sid, d)}</span><span class="nm">${esc(st.name)}</span>${redo}</summary>
      <div class="content">${body}</div></details>`;
  }).join("");
  el.querySelectorAll("[data-redo]").forEach(b => b.onclick = async ev => {
    ev.preventDefault(); ev.stopPropagation();
    const name = META.stages.find(s => s.id === b.dataset.redo).name;
    if (!confirm(`Переделать шаг «${name}» и все шаги после него? Результаты этих шагов будут удалены и созданы заново (это снова потратит деньги на сервисах).`)) return;
    await api(`/api/jobs/${j.id}/action`, { json: { action: "restart_from", stage: b.dataset.redo } }).catch(e => toast(e.message, true));
    toast("Шаг поставлен в работу"); lastStagesKey = "";
  });
  const fSave = el.querySelector("#final-save");
  if (fSave) {
    const ta = el.querySelector("#final-text"), wc = el.querySelector("#final-wc");
    if (keepText !== null && !ta.disabled) ta.value = keepText;
    const count = () => { wc.textContent = `${(ta.value.match(/[0-9A-Za-zА-Яа-яЁё]+/g) || []).length} слов`; };
    ta.oninput = count; count();
    fSave.onclick = async () => {
      if (!confirm("Сохранить ваш текст? Сцены, картинки, озвучка и видео будут сделаны заново по нему.")) return;
      fSave.disabled = true;
      await api(`/api/jobs/${j.id}/script`, { json: { text: ta.value } })
        .then(() => toast("Текст сохранён, ролик собирается заново")).catch(e => { toast(e.message, true); fSave.disabled = false; });
      lastStagesKey = "";
    };
  }
  el.querySelectorAll("[data-regen]").forEach(b => b.onclick = async () => {
    const [kind, idx] = b.dataset.regen.split(":");
    const current = kind === "thumbnail" ? d.stages.extras.thumbnail_prompt
      : (kind === "short" ? d.stages.short_script.scenes : d.stages.scenes.scenes)[+idx].prompt;
    const fix = prompt("Что исправить на картинке? Напишите по-русски, например: «женщина сидит сзади, а не за рулём». Оставьте пустым, чтобы просто перерисовать." + (kind === "thumbnail" ? "" : " Видео потом пересоберётся."), "");
    if (fix === null) return;
    toast(fix.trim() ? "Исправляю описание и перерисовываю…" : "Перерисовываю…");
    await api(`/api/jobs/${j.id}/image`, { json: { kind, index: +idx, prompt: current, fix_ru: fix } }).then(() => toast("Картинка будет перерисована")).catch(e => toast(e.message, true));
    lastStagesKey = "";
  });
  el.querySelectorAll("[data-subs]").forEach(b => b.onclick = async () => {
    const [kind, on] = b.dataset.subs.split(":");
    b.disabled = true;
    await api(`/api/jobs/${j.id}/subtitles`, { json: { kind, on: on === "1" } })
      .then(() => toast(on === "1" ? "Субтитры включены, видео пересобирается (это бесплатно)" : "Субтитры выключены, видео пересобирается"))
      .catch(e => { toast(e.message, true); b.disabled = false; });
    lastStagesKey = "";
  });
  el.querySelectorAll(".speed-apply").forEach(b => b.onclick = async () => {
    const sel = b.parentElement.querySelector(".speed-sel");
    b.disabled = true; b.textContent = "Ускоряю…"; toast("Ускоряю видео, на длинном ролике это займёт пару минут…");
    await api(`/api/jobs/${j.id}/speed`, { json: { kind: sel.dataset.kind, speed: +sel.value } })
      .then(x => toast(x.speed === 1 ? "Вернул обычную скорость" : `Готово: быстрее в ${x.speed} раза`)).catch(e => toast(e.message, true));
    b.disabled = false; b.textContent = "Применить";
    lastStagesKey = "";
  });
  el.querySelectorAll(".fx-apply").forEach(b => b.onclick = async () => {
    b.disabled = true;
    await api(`/api/jobs/${j.id}/effects`, { json: { mode: b.parentElement.querySelector(".fx-sel").value } })
      .then(() => toast("Эффекты изменены, видео пересобирается")).catch(e => { toast(e.message, true); b.disabled = false; });
    lastStagesKey = "";
  });
  const moreBtn = el.querySelector("#idea-more");
  if (moreBtn) moreBtn.onclick = async () => {
    moreBtn.disabled = true; moreBtn.textContent = "Придумываю…";
    await api(`/api/jobs/${j.id}/ideas/more`, { method: "POST" }).then(r => toast(`Добавлено новых тем: ${r.added}`)).catch(e => toast(e.message, true));
    moreBtn.disabled = false; moreBtn.textContent = "➕ Ещё темы";
    lastStagesKey = "";
  };
  const ideaBtn = el.querySelector("#idea-apply");
  if (ideaBtn) ideaBtn.onclick = async () => {
    const pick = el.querySelector('input[name="idea-pick"]:checked');
    if (!pick) return toast("Отметьте тему", true);
    const wait = j.status === "review" && j.options.idea_pause && !j.options.idea_picked;
    if (!wait && !confirm("Сменить тему? Сценарий, картинки и озвучка этого ролика сделаются заново.")) return;
    ideaBtn.disabled = true;
    try { await api(`/api/jobs/${j.id}/idea`, { json: { index: +pick.value } }); toast("Тема выбрана, пишу сценарий"); }
    catch (e) { toast(e.message, true); ideaBtn.disabled = false; }
    lastStagesKey = "";
  };
  const anMode = el.querySelector("#an-mode");
  if (anMode) {
    const sync = () => {
      const n = el.querySelectorAll(".an-pick:checked").length;
      const lbl = el.querySelector("#an-picked"); if (lbl) lbl.textContent = n ? `Отмечено: ${n}` : "";
      el.querySelector(".an-count-box").style.display = ["hook", "key"].includes(anMode.value) ? "" : "none";
    };
    el.querySelectorAll(".an-pick").forEach(c => c.addEventListener("change", () => { anMode.value = "custom"; sync(); }));
    anMode.addEventListener("change", () => { if (anMode.value === "custom") { const b = el.querySelector(".an-pick-box"); if (b) b.open = true; } sync(); });
    const none = el.querySelector("#an-none");
    if (none) none.onclick = () => { el.querySelectorAll(".an-pick").forEach(c => c.checked = false); sync(); };
    sync();
  }
  const anSkip = el.querySelector("#an-skip");
  if (anSkip) anSkip.onclick = async () => {
    anSkip.disabled = true;
    await api(`/api/jobs/${j.id}/animation`, { json: { mode: "off", short: false, count: +el.querySelector("#an-count").value, scenes: [] } })
      .then(() => toast("Продолжаю без анимации")).catch(e => { toast(e.message, true); anSkip.disabled = false; });
    lastStagesKey = "";
  };
  const anBtn = el.querySelector("#an-apply");
  if (anBtn) anBtn.onclick = async () => {
    anBtn.disabled = true;
    const picked = [...el.querySelectorAll(".an-pick:checked")].map(c => +c.value);
    if (anBtn.dataset.go && !picked.length) { anBtn.disabled = false; return toast("Отметьте хотя бы одну сцену или нажмите «Без анимации, продолжить»", true); }
    const body = { mode: anBtn.dataset.go ? "custom" : el.querySelector("#an-mode").value, short: el.querySelector("#an-short").checked,
      count: +el.querySelector("#an-count").value, scenes: picked };
    try {
      let r = await api(`/api/jobs/${j.id}/animation`, { json: body });
      if (r.confirm) {
        const ok = confirm(`Будет оживлено новых сцен: ${r.new}${r.kept ? ` (ещё ${r.kept} уже оживлены и бесплатны)` : ""}.\n\nЭто платно: примерно $${r.cost.toFixed(2)} на fal.ai.\n\nОживить?`);
        if (!ok) { anBtn.disabled = false; return; }
        r = await api(`/api/jobs/${j.id}/animation`, { json: { ...body, confirm: true } });
      }
      toast(anBtn.dataset.go ? "Оживляю выбранные сцены, ролик продолжается" : r.new === 0 && !r.confirm ? "Сохранено. Видео пересоберётся (новых оплат нет)." : "Сохранено. Видео пересоберётся.");
    } catch (e) { toast(e.message, true); anBtn.disabled = false; }
    lastStagesKey = "";
  };
  const thBtn = el.querySelector("#th-apply");
  if (thBtn) thBtn.onclick = async () => {
    thBtn.disabled = true;
    await api(`/api/jobs/${j.id}/thumbnail`, { json: { text: el.querySelector("#th-text").value, side: el.querySelector("#th-side").value, font: el.querySelector("#th-font").value, look: el.querySelector("#th-look").value } })
      .then(() => toast("Превью обновлено")).catch(e => toast(e.message, true));
    lastStagesKey = "";
  };
}

let lastUpKey = "";
const PRIVACY = [["private", "Приватно (видите только вы)"], ["unlisted", "По ссылке"], ["public", "Публично"]];
function statLine(s) {
  if (!s) return "";
  return `<div class="small" style="margin-top:6px">📈 Просмотров: <b>${(s.views || 0).toLocaleString("ru")}</b>${s.avg_view_pct != null ? ` · досматривают в среднем <b>${s.avg_view_pct}%</b> (${Math.round(s.avg_view_sec || 0)} сек)` : ""} · лайков ${s.likes || 0}${s.subs != null ? ` · подписчиков +${s.subs}` : ""} <span class="muted">(обновлено ${new Date(s.fetched_at * 1000).toLocaleString("ru")})</span></div>`;
}

function renderUploads(d) {
  const key = JSON.stringify([d.upload, d.channels, d.stats, d.instagram]);
  if (key === lastUpKey) return;
  lastUpKey = key;
  document.querySelectorAll(".igbox").forEach(box => {
    const st = (d.upload || {}).instagram || {};
    let h = `<h3>Instagram</h3>`;
    if (st.status === "done") h += `<div class="banner ok">✅ Рилс опубликован${st.username ? ` в @${esc(st.username)}` : ""}: <a href="${esc(st.url)}" target="_blank">${esc(st.url)}</a></div>`;
    else if (st.status === "queued" || st.status === "uploading") h += `<p>Выкладываю рилс в Instagram… ${esc(st.step || "")}</p>`;
    else if (!d.instagram) h += `<p class="muted">Чтобы выкладывать рилсы отсюда, подключите Instagram в <a href="#/settings/instagram">Настройках → Instagram</a>.</p>`;
    else h += (st.status === "error" ? `<div class="banner bad small">${nl(st.error)}</div>` : "")
      + `<div class="row"><button class="primary ig-go">📸 Выложить в Instagram (@${esc(d.instagram)})</button></div>`;
    box.innerHTML = h;
    const go = box.querySelector(".ig-go");
    if (go) go.onclick = async () => {
      go.disabled = true;
      await api(`/api/jobs/${d.job.id}/instagram`, { json: {} }).then(() => toast("Выкладываю в Instagram")).catch(e => { toast(e.message, true); go.disabled = false; });
      lastUpKey = "";
    };
  });
  document.querySelectorAll(".upbox").forEach(box => {
    const kind = box.dataset.kind, st = (d.upload || {})[kind] || {};
    const name = kind === "video" ? "видео" : "шортс";
    let h = `<h3>YouTube</h3>`;
    if (st.status === "done") {
      h += `<div class="banner ok">✅ Загружено на канал «${esc(st.channel_title)}»: <a href="${esc(st.url)}" target="_blank">${esc(st.url)}</a><br>
        <span class="small">${esc((PRIVACY.find(p => p[0] === st.privacy) || [, st.privacy])[1])}${st.publish_at ? ", публикация " + new Date(st.publish_at).toLocaleString("ru") : ""}. Проверить и поменять можно в <a href="https://studio.youtube.com" target="_blank">YouTube Studio</a>.</span></div>`
        + (st.note ? `<div class="banner warn small">${esc(st.note)}</div>` : "")
        + (kind === "video" && st.channel_id ? `<div class="row" style="margin-bottom:8px"><button class="sm up-thumb">🖼 ${st.note ? "Отправить превью ещё раз" : "Обновить превью на YouTube"}</button><span class="muted small">ставит текущую картинку превью из программы</span></div>` : "")
        + statLine((d.stats || {})[kind]);
    } else if (st.status === "queued" || st.status === "uploading") {
      h += `<p>Загружаю ${name} на «${esc(st.channel_title)}»… ${st.status === "queued" ? "(ждёт очереди)" : (st.progress || 0) + "%"}</p><div class="bar"><div style="width:${st.progress || 0}%"></div></div>`;
    } else if (!d.channels.length) {
      h += `<p class="muted">Чтобы загружать прямо отсюда, подключите канал в <a href="#/settings/youtube">Настройках → YouTube</a>.</p>`;
    } else {
      const def = d.job.options.channel_id && d.channels.find(c => c.id === d.job.options.channel_id) ? d.job.options.channel_id : d.channels[0].id;
      const prof = d.channels.find(c => c.id === def).profile;
      h += (st.status === "error" ? `<div class="banner bad small">${nl(st.error)}</div>` : "") + `
        <div class="grid2">
          <div><label class="f">Канал</label><select class="up-ch">${d.channels.map(c => `<option value="${c.id}" ${c.id === def ? "selected" : ""}>${esc(c.title)}</option>`).join("")}</select></div>
          <div><label class="f">Доступ</label><select class="up-pr">${PRIVACY.map(([v, n]) => `<option value="${v}" ${v === prof.privacy ? "selected" : ""}>${n}</option>`).join("")}</select></div>
        </div>
        <label class="f">Опубликовать по расписанию <span class="hint">Необязательно. Если указать время, ролик загрузится приватным и сам станет публичным в это время.</span></label>
        <input type="datetime-local" class="up-at" style="max-width:260px">
        <div class="row" style="margin-top:10px"><button class="primary up-go">⬆ Загрузить ${name} на YouTube</button></div>`;
    }
    if (st.status !== "done" && st.status !== "queued" && st.status !== "uploading" && d.job.status === "done") {
      h += `<details style="margin-top:10px"><summary class="small">Уже загрузили этот ${name} на YouTube сами? Вставьте ссылку, и программа будет учитывать его статистику</summary>
        <div class="row" style="margin-top:6px"><input type="text" class="up-link" placeholder="https://youtube.com/shorts/…" style="max-width:360px"><button class="sm up-link-go">Сохранить ссылку</button></div></details>`;
    }
    box.innerHTML = h;
    const th = box.querySelector(".up-thumb");
    if (th) th.onclick = async () => {
      th.disabled = true;
      await api(`/api/jobs/${d.job.id}/youtube_thumb`, { json: { kind } })
        .then(() => toast("Превью поставлено на YouTube")).catch(e => toast(e.message, true));
      th.disabled = false; lastUpKey = "";
    };
    const lk = box.querySelector(".up-link-go");
    if (lk) lk.onclick = async () => {
      await api(`/api/jobs/${d.job.id}/link`, { json: { kind, link: box.querySelector(".up-link").value } })
        .then(() => { toast("Ссылка сохранена. Статистика подтянется при следующем обновлении"); lastUpKey = ""; }).catch(e => toast(e.message, true));
    };
    const go = box.querySelector(".up-go");
    if (go) go.onclick = async () => {
      const at = box.querySelector(".up-at").value;
      go.disabled = true;
      await api(`/api/jobs/${d.job.id}/upload`, { json: { kind, channel_id: box.querySelector(".up-ch").value, privacy: box.querySelector(".up-pr").value, publish_at: at ? new Date(at).toISOString() : "" } })
        .then(() => toast("Загрузка началась")).catch(e => { toast(e.message, true); go.disabled = false; });
      lastUpKey = "";
    };
  });
}

const animModes = () => Object.entries(META.anim_modes).filter(([k]) => k !== "custom");
const img = (d, name, cls = "sc") => d.images.includes(name) ? `<a href="/files/${d.job.id}/images/${name}" target="_blank"><img class="${cls}" loading="lazy" src="/files/${d.job.id}/images/${name}?v=${d.job.updated}"></a>` : "";

const RENDER = {
  idea(r, d) {
    if (!r) return "";
    const j = d.job, busy = j.status === "running";
    const wait = j.status === "review" && j.options.idea_pause && !j.options.idea_picked;
    const rate = x => x.viewer ? (+x.viewer.stop_scroll || 0) + (+x.viewer.unpredictable || 0) + (+x.viewer.payoff || 0) : x.total * 3;
    const order = r.ideas.map((x, i) => i).sort((a, b) => (b === r.chosen_index) - (a === r.chosen_index) || rate(r.ideas[b]) - rate(r.ideas[a]));
    const row = i => { const x = r.ideas[i]; return `<tr class="${i === r.chosen_index ? "chosen" : ""}">
      <td>${busy ? "" : `<input type="radio" name="idea-pick" value="${i}" ${i === r.chosen_index ? "checked" : ""}>`}${i === r.chosen_index ? " ★" : ""}</td>
      <td><b>${esc(x.title_ru)}</b><div class="en">${esc(x.title)}</div></td>
      <td class="small">${esc(x.premise_ru)}${x.twist_ru ? `<div><b>Поворот:</b> ${esc(x.twist_ru)}</div>` : ""}${x.viewer ? `<div class="muted">👀 Зритель: цепляет ${x.viewer.stop_scroll}/10, непредсказуемо ${x.viewer.unpredictable}/10, концовка ${x.viewer.payoff}/10. ${esc(x.viewer.comment_ru || "")}</div>` : ""}</td>
      <td class="small" style="white-space:nowrap">${Object.entries(x.scores || {}).map(([k, v]) => `<span title="${({ curiosity: "Любопытство", emotion: "Эмоции", clarity: "Понятность", uniqueness: "Новизна", visual: "Наглядность" })[k] || k}">${score(v)}</span>`).join(" ")}</td>
      <td>${score(x.total)}</td></tr>`; };
    const head = `<tr><th></th><th>Тема</th><th>Суть</th><th title="Любопытство / Эмоции / Понятность / Новизна / Наглядность">Оценки сценариста</th><th>Итог</th></tr>`;
    const who = r.picked_by_owner ? "Тему выбрали вы." : r.writer_choice !== undefined
      ? "Тему выбрал строгий зритель: он смотрит не на «Итог» сценариста, а на то, зацепит ли первая фраза, угадывается ли концовка и сработает ли поворот." : "Тему выбрал сценарист.";
    return `${wait ? `<div class="banner info">Отметьте тему и нажмите «Взять эту тему». ★ — тема, которую предлагает программа.</div>` : ""}
    <div class="banner ok"><b>${who}</b> ${esc(r.picked_by_owner ? "" : r.reason_ru || "")}</div>
    <table>${head}${order.slice(0, 10).map(row).join("")}</table>
    ${order.length > 10 ? `<details><summary>Ещё ${order.length - 10} тем</summary><table>${head}${order.slice(10).map(row).join("")}</table></details>` : ""}
    ${busy ? "" : `<div class="row" style="margin-top:8px"><button class="${wait ? "primary" : ""}" id="idea-apply">✓ Взять эту тему</button>
      <button id="idea-more" title="Сценарист придумает новые темы, не повторяя эти. Стоит как один запрос к ИИ, остальное не переделывается">➕ Ещё темы</button>
      ${wait ? "" : `<span class="muted small">Если тема сменится, сценарий, картинки и озвучка сделаются заново.</span>`}</div>`}
    <p class="muted small">«Итог» — средняя оценка самого сценариста (любопытство, эмоции, понятность, новизна, наглядность).</p>`;
  },
  outline(o) {
    if (!o) return "";
    return `<h3>Хук (первые 15 секунд)</h3><div class="banner info">${esc(o.hook?.why_ru)}<div class="en">${esc(o.hook?.text_idea)}<br>Главный вопрос: ${esc(o.hook?.big_question)}</div></div>
    <h3>Сюжет коротко</h3><p>${nl(o.summary_ru)}</p>
    <h3>Структура</h3>
    <table><tr><th>Время</th><th>Часть</th><th>Удерживающий крючок</th></tr>
    ${(o.sections || []).map(s => `<tr><td>${esc(s.start_time)}</td><td><b>${esc(s.purpose_ru)}</b><div class="en">${esc(s.name)} · ${(s.beats || []).map(esc).join(" · ")}</div></td><td class="en">${esc(s.rehook)}</td></tr>`).join("")}</table>
    ${(o.open_loops || []).length ? `<h3>Открытые вопросы, которые держат зрителя</h3><ul>${o.open_loops.map(l => `<li class="en">${esc(l.question)} (открыт: ${esc(l.opened_in)}, закрыт: ${esc(l.closed_in)})</li>`).join("")}</ul>` : ""}
    ${(o.characters || []).length ? `<h3>Герои (для одинаковой внешности на всех картинках)</h3><ul>${o.characters.map(c => `<li><b>${esc(c.name)}</b> <span class="en">${esc(c.visual)}</span></li>`).join("")}</ul>` : ""}`;
  },
  script(r) {
    if (!r) return "";
    return checksHtml(r.checks) + `<details><summary>Первый вариант текста (английский)</summary><pre class="text">${esc(r.script)}</pre></details>
    <p class="muted small">Этот текст дальше проверяет и улучшает редактор. Русский перевод итогового текста в шаге «Перевод и описание для YouTube».</p>`;
  },
  review(r, d) {
    const live = !r && d.stages.review_progress;
    if (!r && !live) return "";
    const rounds = (r || live).rounds;
    const thr = `порог ${esc(SETTINGS_CACHE?.quality_threshold ?? 8)}`;
    let head = "";
    if (r) head = r.passed ? `<div class="banner ok">✓ Сценарий прошёл проверку: итоговая оценка <b>${r.final_overall}</b> из 10 (${thr}).</div>`
      : `<div class="banner warn">Сценарий не дотянул до порога за отведённые раунды. Взята лучшая версия (оценка <b>${r.final_overall}</b>). Можно нажать «Переделать с этого шага» или поправить шаблон редактора.</div>`;
    return head + `<table><tr><th>Раунд</th><th>Оценка</th><th>Худший пункт</th><th>Слов</th><th>Решение</th></tr>
      ${rounds.map((x, i) => `<tr><td>${i + 1}</td><td>${score(x.overall)}</td><td>${score(x.min_score)}</td><td>${x.checks.word_count}</td><td>${x.passed ? "принято" : x.score_only ? "финальная проверка" : "переписан"}</td></tr>`).join("")}</table>
      ${rounds.map((x, i) => `<details ${i === rounds.length - 1 ? "open" : ""}><summary><b>Раунд ${i + 1}: подробно</b></summary>
        <table><tr><th>Критерий</th><th>Оценка</th><th>Комментарий редактора</th></tr>
        ${x.scores.map(s => `<tr><td>${esc(s.criterion_ru)}</td><td>${score(s.score)}</td><td class="small">${esc(s.comment_ru)}</td></tr>`).join("")}</table>
        ${x.retell_ru ? `<p><b>Как редактор понял историю на слух:</b> ${esc(x.retell_ru)}</p>` : ""}
        <p><b>Вердикт:</b> ${esc(x.verdict_ru)}</p>
        ${(x.issues_ru || []).length && !x.passed && !x.score_only ? `<p><b>Что исправлено в следующей версии:</b></p><ul>${x.issues_ru.map(t => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
        ${checksHtml(x.checks)}
      </details>`).join("")}
      ${r ? `<details ${d.job.status === "review" ? "open" : ""}><summary><b>✏️ Итоговый текст для озвучки — можно исправить вручную</b>${r.edited_by_owner ? " (исправлен вами)" : ""}</summary>
        <p class="muted small">Здесь ровно то, что будет прочитано вслух. Исправьте что хотите и нажмите «Сохранить мой текст»: программа заново разобьёт его на сцены, нарисует картинки и озвучит. Пустая строка разделяет абзацы.${(d.job.options.characters || []).length ? " Реплика героя своим голосом пишется отдельным абзацем: «Имя: фраза»." : ""}</p>
        <textarea id="final-text" rows="14" style="width:100%" ${d.job.status === "running" ? "disabled" : ""}>${esc(r.final_script)}</textarea>
        <div class="row" style="margin-top:8px"><button class="primary" id="final-save" ${d.job.status === "running" ? "disabled title=\"Поставьте ролик на паузу\"" : ""}>Сохранить мой текст</button><span class="muted small" id="final-wc"></span></div>
      </details>` : ""}`;
  },
  scenes(r, d) {
    if (!r) return "";
    const durs = d.stages.voice?.durations;
    return `<p class="muted">${r.scenes.length} сцен, по одной картинке на каждую. Кнопка «Перерисовать» нарисует картинку заново.</p>
    <table><tr><th>#</th><th>Картинка</th><th>Текст сцены и что на картинке</th><th></th></tr>
    ${r.scenes.map((s, i) => `<tr><td>${i + 1}${durs ? `<div class="muted small">${durs[i].toFixed(1)}с</div>` : ""}</td><td>${img(d, `scene_${String(i).padStart(3, "0")}.png`, d.format === "short" ? "vsc" : "sc")}</td>
      <td><div>${esc(s.prompt_ru)}</div>${s.location || s.shot ? `<div class="muted small">${s.location ? `📍 ${esc(s.location)}` : ""}${s.shot ? ` 🎥 ${esc(({ wide: "общий план", medium: "средний план", pov: "от первого лица", closeup: "крупный план", ots: "из-за плеча", detail: "деталь крупно", low: "снизу", high: "сверху", profile: "в профиль" })[s.shot] || s.shot)}` : ""}</div>` : ""}<div class="en">${esc(s.text)}</div>${fxTag(s)}</td>
      <td><button class="sm" data-regen="scene:${i}">Перерисовать</button></td></tr>`).join("")}</table>`;
  },
  extras(r, d) {
    if (!r) return "";
    const t = d.thumb || {};
    const v = Date.now();
    const thumbBox = d.files.thumbnail
      ? `<a href="/files/${d.job.id}/output/thumbnail.jpg" target="_blank"><img style="width:${d.format === "short" ? "270px" : "480px"};max-width:100%;border-radius:8px;display:block" src="/files/${d.job.id}/output/thumbnail.jpg?v=${v}"></a>`
      : d.files.thumbnail_src ? `<div class="banner info">Картинка для превью готова, надписи пока нет. Нажмите «Обновить надпись».</div>`
      : `<p class="muted">Превью появится после шага «Картинки».</p>`;
    return `<h3>Превью (обложка)</h3>
    <div class="row" style="align-items:flex-start">
      <div>${thumbBox}</div>
      <div class="grow" style="min-width:260px">
        <label class="f">Надпись на превью <span class="hint">По-английски, 2–4 слова. Предложено: ${esc(r.thumbnail_text)} (${esc(r.thumbnail_text_ru)})</span></label>
        <input type="text" id="th-text" value="${esc(t.text ?? r.thumbnail_text)}">
        <label class="f">Где надпись</label>
        <select id="th-side"><option value="left" ${t.side !== "right" ? "selected" : ""}>${d.format === "short" ? "Сверху" : "Слева"}</option><option value="right" ${t.side === "right" ? "selected" : ""}>${d.format === "short" ? "Снизу" : "Справа"}</option></select>
        <label class="f">Шрифт</label>
        <select id="th-font"><option value="">Как предложил сценарист${r.thumbnail_font ? ` (${esc((META.thumb_fonts[r.thumbnail_font] || r.thumbnail_font).split(":")[0])})` : ""}</option>${Object.entries(META.thumb_fonts).map(([k, n]) => `<option value="${k}" ${t.font === k ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
        <label class="f">Оформление надписи</label>
        <select id="th-look"><option value="">Как предложил сценарист</option>${Object.entries(META.thumb_looks).map(([k, n]) => `<option value="${k}" ${t.look === k ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
        <p class="muted small">Шрифт без русских букв программа сама заменит на подходящий.</p>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="th-apply" ${d.files.thumbnail_src ? "" : "disabled"}>Обновить надпись</button>
          ${d.files.thumbnail ? `<a class="btn" href="/files/${d.job.id}/output/thumbnail.jpg" download="thumbnail.jpg">⬇ Скачать превью</a>` : ""}
          <button data-regen="thumbnail:0" ${d.files.thumbnail_src ? "" : "disabled"}>Перерисовать картинку</button>
        </div>
        <p class="muted small">Надпись меняется бесплатно и сразу. «Перерисовать картинку» заново рисует саму картинку (стоит как одна картинка).</p>
      </div>
    </div>
    <h3>Для загрузки на YouTube</h3>
    <div class="kv">
      <div>Название</div><div><b>${esc(r.youtube_title)}</b> ${copyBtn(r.youtube_title)}<div class="muted small">${esc(r.youtube_title_ru)}</div></div>
      <div>Описание</div><div><div class="small" style="white-space:pre-wrap">${esc(r.description)}</div>${copyBtn(r.description)}</div>
      <div>Теги</div><div class="tags">${(r.tags || []).map(t => `<span>${esc(t)}</span>`).join("")} ${copyBtn((r.tags || []).join(", "))}</div>
    </div>
    ${d.files.youtube_txt ? `<p><a class="btn" href="/files/${d.job.id}/output/youtube.txt" target="_blank">📄 Всё для YouTube одним файлом</a> <span class="muted small">Название, описание и теги для видео и шортса. Лежит в папке ролика рядом с видео.</span></p>` : ""}
    <h3>Сценарий на русском</h3><pre class="text">${esc(r.script_ru)}</pre>`;
  },
  short_script(r, d) {
    if (!r) return "";
    return `<div class="kv"><div>Название</div><div><b>${esc(r.title_ru)}</b><div class="en">${esc(r.title)}</div></div>
      <div>Сила первой фразы</div><div>${score(r.hook_score)}</div><div>Концовка</div><div>${score(r.payoff_score)}</div>
      <div>Почему этот момент</div><div>${esc(r.why_ru)}</div></div>
      <h3>Текст на русском</h3><pre class="text">${esc(r.script_ru)}</pre>
      <details><summary>Текст на английском</summary><pre class="text">${esc(r.script)}</pre></details>
      <h3>Сцены шортса</h3><table>${r.scenes.map((s, i) => `<tr><td>${i + 1}</td><td>${img(d, `short_${String(i).padStart(3, "0")}.png`, "vsc")}</td><td>${esc(s.prompt_ru)}<div class="en">${esc(s.text)}</div></td><td><button class="sm" data-regen="short:${i}">Перерисовать</button></td></tr>`).join("")}</table>`;
  },
  approval(r, d) {
    if (r) return `<p class="muted">Сценарий одобрен${d.job.options.review_pause ? " вами" : " автоматически (пауза для проверки не была включена)"}.</p>`;
    if (d.job.status === "review" && !d.anim.waiting) return `<div class="banner info">Ждёт вашего решения. <button class="primary" onclick="document.querySelector('[data-act=approve]').click()">✓ Сценарий хороший, продолжить</button></div>`;
    return d.job.options.review_pause ? `<p class="muted">Здесь программа остановится и подождёт вашей проверки.</p>` : `<p class="muted">Пауза не включена: шаг пройдёт автоматически.</p>`;
  },
  voice(r, d) {
    if (!r) return "";
    return `<p>Длительность: <b>${fmtTime(r.total)}</b>${r.short_total ? `, шортс: <b>${fmtTime(r.short_total)}</b>` : ""}</p>
      ${d.files.narration ? `<audio controls preload="none" src="/files/${d.job.id}/narration.wav"></audio>` : ""}`;
  },
  images(r, d) {
    if (!r) return d.images.length ? `<p class="muted">Готово картинок: ${d.images.length}</p>` : "";
    return `<p>Картинок: <b>${r.count}</b>${r.replaced ? `, из них ${r.replaced} не получились и заменены соседними (их можно перерисовать в шаге «Сцены»)` : ""}.</p>
      <div class="imgs">${d.images.filter(n => n.startsWith("scene_")).slice(0, 12).map(n => img(d, n)).join("")}</div>
      ${d.images.some(n => n.startsWith("loc_")) ? `<p><b>Места действия</b> <span class="muted small">(по ним рисуются все сцены в этом месте)</span></p><div class="imgs">${d.images.filter(n => n.startsWith("loc_")).map(n => img(d, n, n.includes("_v") ? "vsc" : "sc")).join("")}</div>` : ""}
      <p class="muted small">Все картинки с текстом сцен в шаге «Сцены и описания картинок».</p>`;
  },
  animate(r, d) {
    const a = d.anim;
    const busy = d.job.status === "running";
    const dis = busy ? "disabled" : "";
    const names = r ? [...r.scene.map(i => `scene_${String(i).padStart(3, "0")}.mp4`), ...r.short.map(i => `short_${String(i).padStart(3, "0")}.mp4`)].filter(n => a.files.includes(n)) : [];
    const scenes = d.images.filter(n => /^scene_\d+\.png$/.test(n));
    const chosen = new Set(r ? r.scene : a.waiting ? a.suggest : (a.mode === "custom" ? a.scenes : []));
    const clip = (META.video_models.find(m => m.id === META.video_model) || META.video_models[0] || {}).clip;
    let h = `<div class="row">
      <select id="an-mode" style="max-width:300px" ${dis}>${Object.entries(META.anim_modes).map(([k, n]) => `<option value="${k}" ${k === a.mode ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
      <label class="check an-count-box" style="margin:0">сцен: <input type="number" id="an-count" min="1" max="60" value="${a.count}" style="width:70px" ${dis}></label>
      <label class="check" style="margin:0"><input type="checkbox" id="an-short" ${a.short ? "checked" : ""} ${dis}> и шортс</label>
      ${a.waiting ? "" : `<button class="sm" id="an-apply" ${busy ? "disabled title=\"Подождите, пока ролик доделается\"" : ""}>Применить</button>`}</div>
      <p class="muted small">Перед оплатой программа покажет, сколько новых сцен оживится и сколько это стоит${clip ? ` (примерно $${clip} за сцену, даже если сцена короткая: клип оплачивается минимум за 5 секунд)` : ""}. Уже оживлённые сцены сохраняются и второй раз не оплачиваются.</p>`;
    if (scenes.length) {
      if (a.waiting) h = `<div class="banner info">⏸ Выберите сцены для анимации. Отмечено то, что программа оживила бы сама${a.suggest.length ? ` (${a.suggest.length})` : ""}: можно добавить сколько угодно или снять. <div class="row" style="margin-top:8px"><button class="primary" id="an-apply" data-go="1">🎬 Оживить выбранные и продолжить</button><button id="an-skip">Без анимации, продолжить</button></div></div>` + h;
      h += `<details class="an-pick-box" ${a.mode === "custom" || a.waiting ? "open" : ""}><summary>Выбрать сцены вручную (${scenes.length} шт.)</summary>
        <p class="muted small">${a.waiting ? "Отметьте галочками сцены, которые нужно оживить, и нажмите «Оживить выбранные и продолжить» выше." : "Отметьте галочками сцены, которые нужно оживить, и нажмите «Применить». Режим сам переключится на «Выбранные вручную»."}</p>
        <div class="row" style="margin-bottom:6px"><button class="sm ghost" type="button" id="an-none" ${dis}>Снять все</button><span class="muted small" id="an-picked"></span></div>
        <div class="imgs an-grid">${scenes.map(n => { const i = +n.slice(6, 9); return `<label class="an-cell"><img loading="lazy" src="/files/${d.job.id}/images/${n}?v=${d.job.updated}"><span><input type="checkbox" class="an-pick" value="${i}" ${chosen.has(i) ? "checked" : ""} ${dis}> Сцена ${i + 1}${a.files.includes(`scene_${String(i).padStart(3, "0")}.mp4`) ? " ✨" : ""}</span></label>`; }).join("")}</div>
        <p class="muted small">✨ — уже оживлена, бесплатно.</p></details>`;
    }
    if (r && r.mode !== "off" || names.length) {
      h += `<p>Оживлено сцен: <b>${names.length}</b>${r && r.failed.length ? `, не получилось: ${r.failed.length} (там будет обычная картинка)` : ""}.</p>
        <div class="imgs">${names.map(n => `<video class="${n.startsWith("short") ? "vert" : ""}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()" src="/files/${d.job.id}/anim/${n}?v=${d.job.updated}#t=0.5"></video>`).join("")}</div>
        ${names.length ? `<p class="muted small">Наведите мышку на клип, чтобы посмотреть.</p>` : ""}`;
    }
    return h;
  },
  video(r, d) {
    if (!r || !d.files.video) return "";
    const vert = d.format === "short";
    return `<video class="${vert ? "vert" : ""}" controls preload="metadata" src="/files/${d.job.id}/output/video.mp4?v=${d.job.updated}"></video>
      <div class="row" style="margin-top:10px"><a class="btn primary" href="/files/${d.job.id}/output/video.mp4" download="${esc(((d.job.title || "").replace(/[^\p{L}\p{N}\- ]+/gu, "").trim() || "video"))}.mp4">⬇ Скачать видео</a>
      <span class="muted">${fmtTime(r.duration)} · ${vert ? "1080×1920, шортс" : "1920×1080"}${r.music ? " · музыка: " + esc(r.music) : " · без музыки"}</span></div>
      ${subsRow(d, "video", vert ? "short_srt" : "srt", vert ? "short_subtitles.srt" : "subtitles.srt")}
      ${speedRow(d, "video", r)}
      ${fxRow(d)}
      <div class="upbox" data-kind="video"></div>${vert ? `<div class="igbox"></div>` : ""}`;
  },
  short_video(r, d) {
    if (!r || !d.files.short) return "";
    return `<video class="vert" controls preload="metadata" src="/files/${d.job.id}/output/short.mp4?v=${d.job.updated}"></video>
      <div class="row" style="margin-top:10px"><a class="btn primary" href="/files/${d.job.id}/output/short.mp4" download="short.mp4">⬇ Скачать шортс</a><span class="muted">${fmtTime(r.duration)} · 1080×1920</span></div>
      ${subsRow(d, "short", "short_srt", "short_subtitles.srt")}
      ${speedRow(d, "short", r)}
      <div class="upbox" data-kind="short"></div>${d.format !== "short" ? `<div class="igbox"></div>` : ""}`;
  },
};

function fxTag(s) {
  const parts = [META.effect_ru[s.effect], META.sfx_ru[s.sfx]].filter(Boolean);
  return (s.speaker_name ? `<div class="tag-mod">🗣 Говорит: ${esc(s.speaker_name)}</div>` : "")
    + (parts.length ? `<div class="tag-mod">✨ ${esc(parts.join(" + "))}</div>` : "");
}

const SPEEDS = [1, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5];
function speedRow(d, kind, r) {
  const busy = d.job.status === "running";
  const cur = r.speed || 1, base = r.orig_duration || r.duration;
  return `<div class="row" style="margin-top:8px"><span>Скорость:</span>
    <select class="speed-sel" data-kind="${kind}" style="width:auto" ${busy ? "disabled" : ""}>${SPEEDS.map(s => `<option value="${s}" ${s === cur ? "selected" : ""}>${s === 1 ? "Обычная (оригинал)" : "Быстрее в " + s + " раза"} · ${fmtTime(base / s)}</option>`).join("")}</select>
    <button class="sm speed-apply" ${busy ? "disabled" : ""}>Применить</button><span class="muted small">картинка и голос ускоряются вместе, голос не становится писклявым; бесплатно, оригинал сохраняется</span></div>`;
}

function fxRow(d) {
  const busy = d.job.status === "running";
  return `<div class="row" style="margin-top:8px"><span>Эффекты:</span>
    <select class="fx-sel" style="max-width:420px" ${busy ? "disabled" : ""}>${Object.entries(META.fx_modes).map(([k, n]) => `<option value="${k}" ${k === d.fx_mode ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
    <button class="sm fx-apply" ${busy ? "disabled" : ""}>Применить</button><span class="muted small">видео пересоберётся бесплатно</span></div>`;
}

function subsRow(d, kind, fileKey, fileName) {
  const on = d.subs[kind];
  const busy = d.job.status === "running" && d.job.stage === (kind === "video" ? "video" : "short_video");
  return `<div class="row" style="margin-top:10px">
    <span>Субтитры на видео: <b>${on ? "включены" : "выключены"}</b></span>
    <button class="sm" data-subs="${kind}:${on ? 0 : 1}" ${busy ? "disabled title=\"Подождите, пока ролик доделается\"" : ""}>${on ? "Выключить субтитры" : "Включить субтитры"}</button>
    ${d.files[fileKey] ? `<a class="small" href="/files/${d.job.id}/output/${fileName}" download>⬇ Файл субтитров .srt</a>` : ""}
  </div>`;
}

function checksHtml(c) {
  if (!c) return "";
  const items = [
    [c.length_ok, `Длина: ${c.word_count} слов ≈ ${c.minutes} мин (цель ${c.target_words} слов)`],
    [c.bad_openers.length === 0, c.bad_openers.length ? `Начало со штампа: «${c.bad_openers.join(", ")}»` : "Нет штампов в начале («welcome back», «in this video»)"],
    [c.long_sentences <= 3, `Длинных предложений (больше 30 слов): ${c.long_sentences}`],
    ...(c.avg_sentence !== undefined ? [[!c.choppy, c.choppy ? `Текст рубленый: в среднем ${c.avg_sentence} слова в предложении, на слух такое непонятно` : `Предложения нормальной длины: в среднем ${c.avg_sentence} слов`]] : []),
  ];
  return `<div class="small" style="margin:8px 0"><b>Автоматические проверки:</b> ${items.map(([ok, t]) => `<div>${ok ? "✅" : "⚠️"} ${esc(t)}</div>`).join("")}
    <div class="en">Первая фраза: “${esc(c.first_sentence)}”</div></div>`;
}

// ------------------------------------------------------------------ settings
let STYLE_PRESETS = {};  // picture styles, filled from the server (META.image_styles)
function styleDesc(text) {
  if (!text) return `<span class="muted">Стиль не выбран: портрет будет в стиле из общих настроек.</span>`;
  const v = Object.values(STYLE_PRESETS).find(x => x.text === text);
  return v ? `Выбрано: <b>${esc(v.name)}</b> — ${esc(v.desc)}` : `Выбрано: <b>свой стиль</b> (текст ниже)`;
}

let SETTINGS_CACHE = null;
api("/api/settings").then(s => SETTINGS_CACHE = s).catch(() => { });

// Keeps a model typed by hand earlier selectable after the field became a list.
const withCurrent = (cur, opts) => cur && !opts.some(([v]) => v === cur) ? [[cur, cur], ...opts] : opts;

function field(key, label, s, opts = {}) {
  const hint = opts.hint ? `<span class="hint">${opts.hint}</span>` : "";
  if (opts.type === "secret") return `<label class="f">${label}${hint}</label><input type="password" data-k="${key}" placeholder="${s[key] ? "сохранён: " + esc(s[key]) + " (вставьте новый, чтобы заменить)" : "вставьте ключ сюда"}" autocomplete="off">`;
  if (opts.type === "select") return `<label class="f">${label}${hint}</label><select data-k="${key}">${opts.options.map(([v, n]) => `<option value="${esc(v)}" ${String(s[key]) === String(v) ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`;
  if (opts.type === "textarea") return `<label class="f">${label}${hint}</label><textarea data-k="${key}">${esc(s[key])}</textarea>`;
  if (opts.type === "range") return `<label class="f">${label}: <span id="rv-${key}">${esc(s[key])}</span>${hint}</label><input type="range" data-k="${key}" min="${opts.min}" max="${opts.max}" step="${opts.step}" value="${esc(s[key])}" oninput="document.getElementById('rv-${key}').textContent=this.value">`;
  return `<label class="f">${label}${hint}</label><input type="${opts.type || "text"}" data-k="${key}" value="${esc(s[key])}" ${opts.list ? `list="${opts.list}"` : ""} step="any">`;
}

async function viewSettings(id) {
  const s = await api("/api/settings"); SETTINGS_CACHE = s;
  const yt = await api("/api/youtube");
  let voices = [];
  const P = META.providers;
  main.innerHTML = `<h1>Настройки</h1>
  <div class="card"><h2>Резервная копия</h2>
    <p class="muted small">Сохраняет настройки, ключи, проекты, персонажей с портретами, серии, шаблоны, каналы, музыку и звуки в папку «Авто ютуб — резерв» на рабочем столе. Там же лежит файл «О программе Авто ютуб» с описанием всего проекта и «Восстановить из копии» для восстановления. Сами ролики в копию не входят (они большие). Копия делается и сама при каждом обновлении.</p>
    <div class="row"><button id="bk-make">💾 Сделать резервную копию сейчас</button><span id="bk-info" class="muted small"></span></div>
  </div>
  <div class="card"><h2>1. Сценарий (нейросеть)</h2>
    <p class="muted small">Пишет темы, структуру, текст и сама его проверяет. Нужен ключ API (это не то же самое, что подписка ChatGPT Plus или Claude Pro: API оплачивается отдельно по факту, примерно $0.3–1 за ролик).</p>
    ${field("llm_provider", "Какую нейросеть использовать", s, { type: "select", options: [["anthropic", "Claude (Anthropic) — рекомендую для историй"], ["openai", "GPT (OpenAI)"]] })}
    <div class="grid2"><div>
      ${field("anthropic_key", "Ключ Anthropic", s, { type: "secret", hint: `Получить: <a href="https://console.anthropic.com/settings/keys" target="_blank">console.anthropic.com</a>` })}
      ${field("anthropic_model", "Модель Claude", s, { type: "select", options: withCurrent(s.anthropic_model, [["claude-opus-5-5", "Claude Opus 5.5 — лучшее качество (дороже)"], ["claude-sonnet-5-5", "Claude Sonnet 5.5 — почти так же хорошо, вдвое дешевле (рекомендую)"], ["claude-haiku-5-5", "Claude Haiku 5.5 — очень дёшево, тексты проще"]]) })}
      ${field("anthropic_effort", "Насколько глубоко думать", s, { type: "select", options: [["medium", "Средне (дешевле)"], ["high", "Глубоко (рекомендую)"], ["xhigh", "Очень глубоко (дороже)"]] })}
    </div><div>
      ${field("openai_key", "Ключ OpenAI", s, { type: "secret", hint: `Получить: <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>. Этот же ключ подходит для картинок OpenAI.` })}
      ${field("openai_model", "Модель GPT", s, { type: "select", options: withCurrent(s.openai_model, [["gpt-5", "GPT-5"], ["gpt-5-mini", "GPT-5 mini — дешевле"]]) })}
    </div></div>
    <div class="row" style="margin-top:12px"><button id="t-llm">Проверить нейросеть</button><span id="t-llm-r" class="muted small"></span></div>
    <h3>Качество сценария</h3>
    ${field("niche", "Ниша канала по умолчанию", s, { type: "textarea", hint: "Можно по-русски. Например: «мистические реальные истории», «истории выживания», «загадки истории»." })}
    <div class="grid2"><div>
      ${field("duration_min", "Длительность по умолчанию, минут", s, { type: "number" })}
      ${field("ideas_count", "Сколько тем придумывать за раз", s, { type: "number", hint: "От 3 до 10. Меньше тем — дешевле. На странице ролика можно попросить ещё столько же кнопкой «Ещё темы»" })}
      ${field("quality_threshold", "Порог качества (средняя оценка из 10)", s, { type: "number", hint: "Ниже порога редактор переписывает текст. 8 — строго, 7 — мягче." })}
    </div><div>
      ${field("min_criterion_score", "Минимум по любому пункту чек-листа", s, { type: "number" })}
      ${field("max_editor_rounds", "Сколько раз максимум переписывать", s, { type: "number" })}
    </div></div>
  </div>

  <div class="card"><h2>2. Озвучка (ElevenLabs)</h2>
    ${field("elevenlabs_key", "Ключ ElevenLabs", s, { type: "secret", hint: `Получить: <a href="https://elevenlabs.io/app/settings/api-keys" target="_blank">elevenlabs.io → Settings → API Keys</a>` })}
    <div class="row" style="margin-top:10px"><button id="load-voices">Загрузить список голосов</button><span id="el-acc" class="muted small"></span></div>
    <label class="f">Голос <span class="hint">Сейчас: ${esc(s.elevenlabs_voice_name)}. Для историй хорошо подходят спокойные низкие мужские голоса (George, Brian, Daniel).</span></label>
    <select id="voice-sel"><option value="${esc(s.elevenlabs_voice_id)}">${esc(s.elevenlabs_voice_name)}</option></select>
    <div class="row" style="margin-top:6px"><button class="sm" id="voice-preview">▶ Образец голоса</button><button class="sm primary" id="voice-lib">🇷🇺 Подобрать русский голос</button><audio id="voice-audio"></audio></div>
    <label class="f">…или ID голоса вручную <span class="hint">Если список голосов не загружается: на elevenlabs.io откройте голос, нажмите «…» → Copy voice ID и вставьте сюда. Если ошибка про разрешение (permission), откройте на сайте ElevenLabs ваш API-ключ → Edit и включите доступ «Voices: Read» и «User: Read», либо создайте новый ключ без ограничений.</span></label>
    <input type="text" id="voice-id" placeholder="например JBFqnCBsd6RMkjVDRZzb">
    <div id="voice-marks"></div>
    ${field("elevenlabs_model", "Модель озвучки", s, { type: "select", options: META.tts_models.map(m => [m.id, m.name]) })}
    <div class="grid2"><div>
      ${field("voice_stability", "Стабильность", s, { type: "range", min: 0, max: 1, step: 0.05, hint: "Меньше — эмоциональнее, больше — ровнее" })}
      ${field("voice_similarity", "Похожесть на оригинал", s, { type: "range", min: 0, max: 1, step: 0.05 })}
    </div><div>
      ${field("voice_style", "Выразительность", s, { type: "range", min: 0, max: 1, step: 0.05 })}
      ${field("voice_speed", "Скорость речи", s, { type: "range", min: 0.7, max: 1.2, step: 0.01 })}
    </div></div>
    <div class="row" style="margin-top:12px"><button id="t-tts">Проверить озвучку (сохраните настройки перед проверкой)</button><audio id="t-tts-a" controls style="display:none"></audio></div>
  </div>

  <div class="card"><h2>3. Картинки</h2>
    <p class="muted small">Выберите сервис и вставьте его ключ. Можно вставить ключи нескольких сервисов и переключаться.</p>
    ${Object.entries(P).map(([id, p]) => `<div class="provider ${s.image_provider === id ? "sel" : ""}">
      <label class="check" style="margin:0"><input type="radio" name="imgprov" value="${id}" ${s.image_provider === id ? "checked" : ""}> <b>${esc(p.name)}</b> <a class="small" href="${p.signup}" target="_blank">получить ключ</a></label>
      <div class="grid2">
        <div>${id === "openai" ? `<p class="small muted">Используется ключ OpenAI из раздела «Сценарий».</p>` : field(p.key_field, "Ключ", s, { type: "secret" })}</div>
        <div>${field(p.model_field, "Модель", s, { type: "select", options: p.models.map(m => [m.id, `${m.name} (~$${m.price} за картинку)`]) })}</div>
      </div>
      <div class="row"><button class="sm" data-timg="${id}">Проверить этот сервис</button></div>
    </div>`).join("")}
    ${field("openai_image_quality", "Качество для OpenAI", s, { type: "select", options: [["low", "Низкое (дёшево)"], ["medium", "Среднее"], ["high", "Высокое (дорого)"]] })}
    ${field("image_style", "Стиль всех картинок", s, { type: "textarea", hint: "Добавляется к каждой картинке, чтобы весь ролик был в одном стиле. По-английски. Можно взять готовый стиль кнопкой ниже." })}
    <div class="row" style="margin-top:6px">${Object.entries(STYLE_PRESETS).map(([k, v]) => `<button class="sm" data-style="${k}" title="${esc(v.desc)}">${esc(v.name)}</button>`).join("")}</div>
    ${field("char_reference", "Персонажи на картинках по портрету", s, { type: "select", options: [["true", "Да, как на портрете (fal.ai, ~$0.04 за картинку с персонажем)"], ["false", "Нет, только по текстовому описанию (дешевле, но похожесть хуже)"]] })}
    <div id="t-img-r" style="margin-top:10px"></div>
  </div>

  <div class="card"><h2>4. Видео</h2>
    <div class="grid2"><div>
      ${field("words_per_scene", "Слов на одну картинку (длинное видео)", s, { type: "number", hint: "22 слова ≈ 9 секунд на картинку" })}
      ${field("short_words_per_scene", "Слов на одну картинку (шортс)", s, { type: "number" })}
      ${field("motion_strength", "Сила движения камеры", s, { type: "range", min: 0.05, max: 0.35, step: 0.01 })}
      ${field("music_volume", "Громкость фоновой музыки", s, { type: "range", min: 0, max: 0.4, step: 0.01 })}
    </div><div>
      ${field("fps", "Кадров в секунду", s, { type: "select", options: [[24, "24"], [30, "30"]] })}
      ${field("scene_gap", "Пауза между сценами, сек", s, { type: "number" })}
      ${field("paragraph_gap", "Пауза между абзацами, сек", s, { type: "number" })}
      ${field("image_parallel", "Картинок одновременно", s, { type: "number" })}
      ${field("render_parallel", "Фрагментов видео одновременно", s, { type: "number", hint: "Для M1 подходит 3" })}
    </div></div>
    <h3>Анимация сцен</h3>
    <p class="muted small">Нейросеть превращает картинку сцены в короткий клип с движением (люди, огонь, вода, камера). Работает через fal.ai, нужен ключ fal.ai из раздела «Картинки». Стоит денег за каждую секунду, поэтому выгоднее оживлять только начало ролика и ключевые моменты. Здесь значения по умолчанию, у каждого ролика можно выбрать своё.</p>
    <div class="grid2"><div>
      ${field("video_model", "Модель анимации", s, { type: "select", options: META.video_models.map(m => [m.id, `${m.name}: ~$${m.clip} за одну сцену ($${m.price} за секунду клипа)`]) })}
      ${field("anim_mode", "Что оживлять по умолчанию", s, { type: "select", options: animModes(), hint: "Для роликов без проекта. У проекта своя настройка в его карточке" })}
      ${field("anim_short", "Оживлять шортс", s, { type: "select", options: [["false", "Нет"], ["true", "Да, столько же первых сцен шортса"]] })}
    </div><div>
      ${field("anim_count", "Сколько сцен оживлять", s, { type: "number", hint: "«Первые сцены»: первые N. «Начало и ключевые моменты»: треть в начале, остальное на поворотах сюжета. Одна сцена ≈ цена одного клипа" })}
    </div></div>
    <div class="row" style="margin-top:10px"><button id="t-anim">▶ Проверить анимацию (одна картинка + один клип, ~$0.4)</button><span id="t-anim-r" class="muted small"></span></div>
    <video id="t-anim-v" controls loop style="display:none;max-width:560px;margin-top:8px"></video>
    <h3>Эффекты</h3>
    <p class="muted small">Звук перелистывания на смене картинок, удары, «вжух» на переходах, резкие наезды и тряска в нужных местах сценария. Можно положить свои звуки (mp3 или wav) в папки <code>${esc(META.data_dir)}/sfx/whoosh</code>, <code>impact</code> и <code>swoosh</code>: программа возьмёт их вместо встроенных.</p>
    <div class="grid2"><div>
      ${field("fx_mode", "Эффекты по умолчанию", s, { type: "select", options: Object.entries(META.fx_modes), hint: "Для роликов без проекта. У проекта своя настройка в его карточке" })}
    </div><div>
      ${field("sfx_volume", "Громкость звуковых эффектов", s, { type: "range", min: 0, max: 1, step: 0.05 })}
    </div></div>
    <h3>Субтитры</h3>
    <p class="muted small">Текст озвучки появляется на видео в такт голосу. Это значение по умолчанию для новых роликов: у каждого ролика субтитры можно включить или выключить отдельно. Файл .srt для YouTube создаётся всегда.</p>
    <div class="grid2"><div>
      ${field("subtitles_long", "Субтитры на длинном видео", s, { type: "select", options: [["false", "Выключены"], ["true", "Включены"]], hint: "Для роликов без проекта. У проекта своя настройка в его карточке" })}
    </div><div>
      ${field("cta_short", "Призыв на шортсах", s, { type: "select", options: Object.entries(META.cta_modes), hint: "Анимированные кнопки «Подписаться» и «Нравится» в стиле YouTube во второй половине шортса. Для роликов без проекта; у проекта своя настройка в его карточке" })}
      ${field("cta_lang", "Язык кнопок призыва", s, { type: "select", options: Object.entries(META.cta_langs), hint: "Для роликов без проекта" })}
      ${field("subtitles_short", "Субтитры на шортсе", s, { type: "select", options: [["true", "Включены (рекомендую: шортсы часто смотрят без звука)"], ["false", "Выключены"]], hint: "Для роликов без проекта. У проекта своя настройка в его карточке" })}
    </div></div>
  </div>
  <div class="card" id="ytcard"><h2>5. YouTube: автоматическая загрузка</h2>
    <p class="muted small">Можно подключить несколько каналов. У каждого канала свои ниша, голос и стиль картинок, а ещё настройка «загружать сам, когда ролик готов».</p>
    ${yt.last_error ? `<div class="banner bad">${nl(yt.last_error)}</div>` : ""}
    <details ${yt.has_client ? "" : "open"}><summary><b>Как подключить (один раз, ~15 минут)</b></summary>
      <ol class="small">
        <li>Откройте <a href="https://console.cloud.google.com/" target="_blank">console.cloud.google.com</a> и войдите своим Google-аккаунтом. Вверху нажмите выбор проекта → «New project», назовите, например, «Avto YouTube» → Create.</li>
        <li>Откройте <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank">YouTube Data API v3</a> и нажмите <b>Enable</b>.</li>
        <li>Так же включите <a href="https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com" target="_blank">YouTube Analytics API</a> (<b>Enable</b>): по нему программа узнаёт, сколько процентов роликов досматривают, и учится, что работает.</li>
        <li>Откройте <a href="https://console.cloud.google.com/auth/overview" target="_blank">Google Auth Platform</a> → <b>Get started</b>: имя приложения «Avto YouTube», ваш email, Audience: <b>External</b>, снова email → Create.</li>
        <li>Там же, раздел <b>Audience</b>: нажмите <b>Publish app</b> и подтвердите (статус станет «In production»). Без этого Google будет отключать доступ каждые 7 дней. Проверка Google для личного пользования не нужна.</li>
        <li>Раздел <b>Clients</b> → <b>Create client</b> → Application type: <b>Desktop app</b> → Create → <b>Download JSON</b>.</li>
        <li>Загрузите этот файл кнопкой ниже.</li>
        <li>Нажмите «Подключить канал». Google покажет «Google hasn't verified this app»: нажмите <b>Advanced</b> («Дополнительно») → <b>Go to Avto YouTube</b> → выберите канал → отметьте все галочки → Continue.</li>
        <li>Чтобы YouTube принимал своё превью, подтвердите канал по телефону: <a href="https://www.youtube.com/verify" target="_blank">youtube.com/verify</a>.</li>
      </ol></details>
    <div class="row" style="margin:12px 0">
      <input type="file" id="ytfile" accept=".json,application/json" style="display:none">
      <button id="ytfile-btn">${yt.has_client ? "✅ Файл ключа Google загружен (заменить)" : "Загрузить файл ключа Google (JSON)"}</button>
      <a class="btn primary" href="/api/youtube/login" ${yt.has_client ? "" : 'style="pointer-events:none;opacity:.5"'}>＋ Подключить канал</a>
    </div>
    <p class="muted small">Каждый канал подключается отдельно. Если несколько каналов на одном Google-аккаунте, Google спросит, какой из них выбрать.</p>
    ${yt.channels.map(c => `<div class="provider" data-ch="${c.id}">
      <div class="row">${c.thumb ? `<img src="${esc(c.thumb)}" style="width:36px;height:36px;border-radius:50%">` : ""}<b class="grow">${esc(c.title)}</b>
        ${c.needs_login ? `<span class="badge error">нужно войти заново</span>` : `<span class="badge done">подключён</span>`}</div>
      ${c.needs_login ? `<div class="banner warn small">Доступ к каналу закончился. Нажмите «Подключить канал» и выберите этот канал ещё раз.</div>` : ""}
      <details><summary class="small" style="cursor:pointer;margin-top:6px">Настройки канала</summary>
        <label class="f">Ниша канала <span class="hint">Пусто — как в общих настройках</span></label><textarea data-p="niche">${esc(c.profile.niche)}</textarea>
        <label class="f">Голос</label><select data-p="voice_id" class="ch-voice"><option value="">Как в общих настройках (${esc(s.elevenlabs_voice_name)})</option>${c.profile.voice_id ? `<option value="${esc(c.profile.voice_id)}" selected>${esc(c.profile.voice_name || c.profile.voice_id)}</option>` : ""}</select>
        <label class="f">Стиль картинок <span class="hint">Пусто — как в общих настройках. По-английски.</span></label><textarea data-p="image_style">${esc(c.profile.image_style)}</textarea>
        <div class="grid2"><div>
          <label class="f">Доступ при загрузке</label><select data-p="privacy">${PRIVACY.map(([v, n]) => `<option value="${v}" ${v === c.profile.privacy ? "selected" : ""}>${n}</option>`).join("")}</select>
        </div><div>
          <label class="f">Категория</label><select data-p="category">${yt.categories.map(([v, n]) => `<option value="${v}" ${v === c.profile.category ? "selected" : ""}>${n}</option>`).join("")}</select>
        </div></div>
        <label class="check"><input type="checkbox" data-p="auto_upload" ${c.profile.auto_upload ? "checked" : ""}> Загружать на этот канал сам, как только ролик готов</label>
        <label class="check"><input type="checkbox" data-p="synthetic" ${c.profile.synthetic ? "checked" : ""}> Отмечать «ИИ-контент» (рекомендую: YouTube требует это для реалистичного ИИ-контента)</label>
        <div class="row"><button class="primary sm ch-save">Сохранить канал</button><button class="sm danger ch-del">Отключить канал</button></div>
      </details></div>`).join("")}
  </div>
  <div class="card" id="igcard"><h2>6. Instagram: выкладка рилсов</h2>
    <p class="muted small">Шортсы выкладываются в Instagram как рилсы с подписью и хэштегами. Видео идёт прямо с вашего компьютера в Instagram. Если вы в России, при выкладке должен быть включён VPN.</p>
    ${s.ig_username ? `<div class="banner ok">✅ Подключён аккаунт <b>@${esc(s.ig_username)}</b>${s.ig_page ? ` (страница Facebook «${esc(s.ig_page)}»)` : ""}. <button class="sm danger" id="ig-off">Отключить</button></div>` : ""}
    <details ${s.ig_username ? "" : "open"}><summary><b>Как подключить (один раз, ~10 минут)</b></summary>
      <p class="small"><b>Важно:</b> выкладывать видео прямо с компьютера Meta разрешает только при подключении через страницу Facebook. Маркер со страницы «Настройка API для входа в Instagram» для выкладки не подходит.</p>
      <ol class="small">
        <li>В приложении Instagram: Настройки → «Тип аккаунта и инструменты» → <b>«Переключиться на профессиональный аккаунт»</b> → «Автор». Это бесплатно.</li>
        <li>Привяжите Instagram к странице Facebook. Если страницы нет, создайте её на <a href="https://www.facebook.com/pages/create" target="_blank">facebook.com/pages/create</a> (подойдёт любое название). В настройках страницы: «Связанные аккаунты» → Instagram → <b>Подключить</b>.</li>
        <li>В кабинете приложения на <a href="https://developers.facebook.com/apps" target="_blank">developers.facebook.com/apps</a> слева: Instagram → <b>«Настройка API для входа на Facebook»</b>. Затем «Настройки приложения» → «Основное»: скопируйте <b>ID приложения</b> и <b>Секрет приложения</b> в поля ниже. Это не те ID и секрет, что на странице Instagram.</li>
        <li>Откройте <a href="https://developers.facebook.com/tools/explorer" target="_blank">Graph API Explorer</a>. Справа выберите своё приложение и «User Token». В «Permissions» добавьте: <b>instagram_basic</b>, <b>instagram_content_publish</b>, <b>pages_show_list</b>, <b>pages_read_engagement</b>, <b>business_management</b>. Нажмите <b>Generate Access Token</b>, войдите, отметьте свою страницу и аккаунт Instagram. Скопируйте токен (начинается с EAA) в поле справа.</li>
        <li>Нажмите «Подключить через Facebook». Программа сама сделает токен постоянным, секрет не сохраняет.</li>
      </ol></details>
    <div class="grid2"><div>
      <label class="f">Секрет приложения</label><input type="password" id="ig-secret" autocomplete="off">
      <label class="f">ID приложения (App ID)</label><input type="text" id="ig-app" autocomplete="off">
    </div><div>
      <label class="f">Маркер доступа (токен)</label><textarea id="ig-token" rows="4" autocomplete="off" placeholder="EAA…"></textarea>
    </div></div>
    <div class="row" style="margin:10px 0"><button id="ig-connect">${s.ig_username ? "Подключить заново через Facebook" : "Подключить через Facebook"}</button><span id="ig-r" class="muted small"></span></div>
    <div class="grid2"><div>
      ${field("ig_auto", "Выкладывать в Instagram сам, как только ролик готов", s, { type: "select", options: [["false", "Нет, только по кнопке на странице ролика"], ["true", "Да, автоматически"]] })}
    </div><div>
      ${field("ig_ai_label", "Отмечать «Создано с помощью ИИ»", s, { type: "select", options: [["true", "Да (рекомендую: Instagram требует это для ИИ-контента)"], ["false", "Нет"]] })}
    </div></div>
  </div>
  <div class="sticky-save"><button class="primary big" id="save">Сохранить настройки</button></div>`;
  $("#ig-connect").onclick = async () => {
    const r = $("#ig-r"); r.textContent = "Подключаю…";
    try {
      const x = await api("/api/instagram/connect", { json: { app_id: $("#ig-app").value, app_secret: $("#ig-secret").value, token: $("#ig-token").value } });
      toast(`Instagram подключён: @${x.username}`); viewSettings("instagram");
    } catch (e) { r.textContent = ""; toast(e.message, true); }
  };
  if ($("#ig-off")) $("#ig-off").onclick = async () => { if (!confirm("Отключить Instagram?")) return; await api("/api/instagram/disconnect", { json: {} }); viewSettings("instagram"); };
  if (id === "instagram") $("#igcard").scrollIntoView();
  if (id === "youtube-ok") toast("Канал подключён");
  if (id && id.startsWith("youtube")) $("#ytcard").scrollIntoView();
  $("#ytfile-btn").onclick = () => $("#ytfile").click();
  $("#ytfile").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    try { await api("/api/youtube/client", { method: "POST", body: f }); toast("Файл ключа загружен. Теперь нажмите «Подключить канал»."); viewSettings("youtube"); }
    catch (err) { toast(err.message, true); }
  };
  main.querySelectorAll("[data-ch]").forEach(box => {
    const cid = box.dataset.ch;
    box.querySelector("details").addEventListener("toggle", async () => {
      const sel = box.querySelector(".ch-voice");
      if (sel.dataset.loaded) return; sel.dataset.loaded = 1;
      try {
        const vs = voices.length ? voices : (voices = await api("/api/voices"));
        const cur = sel.value;
        sel.innerHTML = sel.options[0].outerHTML + voiceOptions(vs, cur, ""); sel.value = cur;
      } catch (e) { }
    });
    box.querySelector(".ch-save").onclick = async () => {
      const p = {};
      box.querySelectorAll("[data-p]").forEach(i => p[i.dataset.p] = i.type === "checkbox" ? i.checked : i.value);
      const vs = box.querySelector(".ch-voice"); p.voice_name = vs.value ? (vs.options[vs.selectedIndex].dataset.name || vs.options[vs.selectedIndex].text) : "";
      await api(`/api/youtube/channels/${cid}`, { json: p }).then(() => toast("Настройки канала сохранены")).catch(e => toast(e.message, true));
    };
    box.querySelector(".ch-del").onclick = async () => {
      if (!confirm("Отключить этот канал от программы? На YouTube ничего не удалится.")) return;
      await api(`/api/youtube/channels/${cid}`, { method: "DELETE" }); viewSettings("youtube");
    };
  });

  const collect = () => {
    const out = {};
    main.querySelectorAll("[data-k]").forEach(i => { if (i.type === "password" && !i.value) return; out[i.dataset.k] = i.value; });
    const prov = main.querySelector("input[name=imgprov]:checked"); if (prov) out.image_provider = prov.value;
    const vs = $("#voice-sel"); out.elevenlabs_voice_id = vs.value; out.elevenlabs_voice_name = vs.options[vs.selectedIndex]?.dataset.name || vs.options[vs.selectedIndex]?.text;
    const manual = $("#voice-id").value.trim();
    if (manual) { out.elevenlabs_voice_id = manual; out.elevenlabs_voice_name = `Свой голос (${manual.slice(0, 6)}…)`; }
    return out;
  };
  const save = async (quiet) => { SETTINGS_CACHE = await api("/api/settings", { json: collect() }); draftClear(); if (!quiet) { toast("Настройки сохранены"); await viewSettings(); draftBaseReset(); } };
  $("#save").onclick = () => save(false).catch(e => toast(e.message, true));
  main.querySelectorAll("input[name=imgprov]").forEach(r => r.onchange = () => main.querySelectorAll(".provider").forEach(p => p.classList.toggle("sel", p.contains(main.querySelector("input[name=imgprov]:checked")))));
  $("#load-voices").onclick = async () => {
    try {
      await save(true);
      voices = await loadVoices(true);
      const cur = $("#voice-sel").value;
      $("#voice-sel").innerHTML = voiceOptions(voices, cur, ""); $("#voice-sel").value = cur;
      renderMarks();
      const a = await api("/api/elevenlabs/account").catch(() => null);
      if (a) $("#el-acc").textContent = `Тариф: ${a.tier}, использовано ${a.used?.toLocaleString("ru")} из ${a.limit?.toLocaleString("ru")} символов`;
      toast(`Загружено голосов: ${voices.length}`);
    } catch (e) { toast(e.message, true); }
  };
  const renderMarks = () => {
    const box = $("#voice-marks");
    const sel = v => `<select class="sm" data-aud="${v.voice_id}" style="width:auto">
      <option value="auto" ${v.audience === v.auto_audience ? "selected" : ""}>Само (${{ ru: "RU", en: "EN", both: "?" }[v.auto_audience]})</option>
      <option value="ru" ${v.audience === "ru" && v.audience !== v.auto_audience ? "selected" : ""}>🇷🇺 Русская</option>
      <option value="en" ${v.audience === "en" && v.audience !== v.auto_audience ? "selected" : ""}>🇺🇸 Английская</option>
      <option value="both" ${v.audience === "both" && v.audience !== v.auto_audience ? "selected" : ""}>🌍 Обе</option></select>`;
    const countries = [...new Set(voices.map(v => v.country))].sort();
    const f = renderMarks.f || (renderMarks.f = { aud: "", kind: "", country: "" });
    const shown = voices.filter(v => (!f.aud || v.audience === f.aud) && (!f.kind || v.kind === f.kind) && (!f.country || v.country === f.country));
    box.innerHTML = `<h3>Голоса: аудитория, тип и страна</h3>
      <p class="muted small">Программа сама делит голоса на мужские, женские и детские и пишет, под какую страну подходит акцент. Аудиторию определяет так: голоса, у которых в ElevenLabs указан русский язык, идут в русскую; стандартные голоса ElevenLabs (George, Brian и др.) — носители английского. Если не согласны, поменяйте пометку в последнем столбце, она запомнится. Русские голоса удобно добавлять на elevenlabs.io: Voices → Voice Library → фильтр Language: Russian → «Add to my voices», потом нажмите «Загрузить список голосов».</p>
      <div class="row" style="gap:8px;margin:8px 0" data-nodraft>
        <select data-vf="aud" data-nodraft style="width:auto"><option value="">Любая аудитория</option>${Object.entries(AUD).map(([k, n]) => `<option value="${k}" ${f.aud === k ? "selected" : ""}>${n.split(" (")[0]}</option>`).join("")}</select>
        <select data-vf="kind" style="width:auto"><option value="">Все голоса</option>${Object.entries(KIND).map(([k, n]) => `<option value="${k}" ${f.kind === k ? "selected" : ""}>${n}</option>`).join("")}</select>
        <select data-vf="country" style="width:auto"><option value="">Все страны</option>${countries.map(c => `<option ${f.country === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>
        <span class="muted small">Показано: ${shown.length} из ${voices.length}</span></div>
      <div data-nodraft><table><tr><th>Тип</th><th>Имя</th><th>Страна</th><th>Описание</th><th></th><th>Аудитория</th></tr>${shown.map(v => `<tr>
        <td style="white-space:nowrap">${KIND[v.kind || "other"].split(" ")[0]}</td><td><b>${esc(v.name)}</b></td>
        <td style="white-space:nowrap">${v.flag} ${esc(v.country)}</td><td class="muted small">${esc(v.description)}</td>
        <td>${v.preview_url ? `<button class="sm" data-prev="${v.voice_id}">▶</button>` : ""}</td><td>${sel(v)}</td></tr>`).join("")}</table></div>`;
    box.querySelectorAll("[data-vf]").forEach(x => { x.dataset.nodraft = 1; x.onchange = () => { f[x.dataset.vf] = x.value; renderMarks(); }; });
    box.querySelectorAll("[data-aud]").forEach(x => x.onchange = async () => {
      await api(`/api/voices/${x.dataset.aud}/audience`, { json: { audience: x.value } }).catch(e => toast(e.message, true));
      voices = await loadVoices(true); const cur = $("#voice-sel").value;
      $("#voice-sel").innerHTML = voiceOptions(voices, cur, ""); $("#voice-sel").value = cur; renderMarks();
      toast("Пометка сохранена");
    });
    box.querySelectorAll("[data-prev]").forEach(b => b.onclick = () => {
      const v = voices.find(x => x.voice_id === b.dataset.prev); const a = $("#voice-audio"); a.src = v.preview_url; a.play();
    });
  };
  $("#voice-lib").onclick = () => openVoiceLibrary(async (vid, name) => {
    $("#voice-id").value = ""; voices = VOICES || voices;
    const sel = $("#voice-sel"); if (voices.length) sel.innerHTML = voiceOptions(voices, vid, "");
    pickIntoSelect(sel, vid, name);
  });
  $("#voice-preview").onclick = () => {
    const v = voices.find(x => x.voice_id === $("#voice-sel").value);
    if (!v?.preview_url) return toast("Сначала нажмите «Загрузить список голосов»", true);
    const a = $("#voice-audio"); a.src = v.preview_url; a.play();
  };
  main.querySelectorAll("[data-style]").forEach(b => b.onclick = () => {
    main.querySelector('[data-k="image_style"]').value = STYLE_PRESETS[b.dataset.style].text;
    toast("Стиль подставлен. Нажмите «Сохранить», а потом «Проверить этот сервис», чтобы увидеть пример.");
  });
  $("#t-anim").onclick = async () => {
    const r = $("#t-anim-r"), b = $("#t-anim");
    b.disabled = true; r.textContent = "Рисую картинку и оживляю её. Обычно это 1–3 минуты…";
    try { await save(true); const blob = await api("/api/test/anim", { json: {} }); const v = $("#t-anim-v"); v.style.display = ""; v.src = URL.createObjectURL(blob); v.play(); r.textContent = "✅ Анимация работает."; }
    catch (e) { r.textContent = "❌ " + e.message; }
    b.disabled = false;
  };
  const bkInfo = () => api("/api/backup").then(r => { $("#bk-info").textContent = r.items.length ? `Копий: ${r.items.length}, последняя: ${r.items[0].name}` : "Копий пока нет."; }).catch(() => { });
  bkInfo();
  $("#bk-make").onclick = async () => {
    try { const r = await api("/api/backup", { method: "POST" }); toast(`Сохранено ${r.where}: ${r.name}`); bkInfo(); } catch (e) { toast(e.message, true); }
  };
  $("#t-llm").onclick = async () => {
    $("#t-llm-r").textContent = "Проверяю…";
    try { await save(true); const r = await api("/api/test/llm", { json: {} }); $("#t-llm-r").textContent = `✅ Работает. Ответ: «${r.text}»`; }
    catch (e) { $("#t-llm-r").textContent = "❌ " + e.message; }
  };
  $("#t-tts").onclick = async () => {
    try { await save(true); toast("Озвучиваю пробную фразу…"); const b = await api("/api/test/tts", { json: {} }); const a = $("#t-tts-a"); a.style.display = ""; a.src = URL.createObjectURL(b); a.play(); }
    catch (e) { toast(e.message, true); }
  };
  main.querySelectorAll("[data-timg]").forEach(b => b.onclick = async () => {
    $("#t-img-r").innerHTML = `<p class="muted">Рисую пробную картинку через ${esc(P[b.dataset.timg].name)}…</p>`;
    $("#t-img-r").scrollIntoView({ block: "center" });
    try { await save(true); const blob = await api("/api/test/image", { json: { provider: b.dataset.timg } }); $("#t-img-r").innerHTML = `<p>✅ Работает:</p><img src="${URL.createObjectURL(blob)}" style="max-width:600px;width:100%;border-radius:8px">`; }
    catch (e) { $("#t-img-r").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
  });
}

// ------------------------------------------------------------------ prompts
async function viewPrompts(name) {
  const list = await api("/api/prompts");
  const cur = list.find(p => p.name === name) || list[0];
  main.innerHTML = `<h1>Шаблоны сценария</h1>
  <div class="banner info small">Это инструкции, по которым нейросеть пишет сценарий на каждом шаге. Они на английском, потому что так нейросеть пишет английский текст качественнее. Слова в {{двойных фигурных скобках}} программа подставляет сама, их не удаляйте. Формат ответа задаёт программа, поэтому менять можно только сами правила. Если что-то сломалось, нажмите «Вернуть как было».</div>
  <div class="row" style="align-items:flex-start">
    <div class="card plist" style="width:300px">${list.map(p => `<a href="#/prompts/${p.name}" class="${p.name === cur.name ? "active" : ""}">${esc(p.title)} ${p.modified ? `<span class="tag-mod">● изменён</span>` : ""}</a>`).join("")}</div>
    <div class="card grow">
      <h2>${esc(cur.title)}</h2>
      <textarea class="code" id="ptext">${esc(cur.text)}</textarea>
      <div class="row" style="margin-top:10px"><button class="primary" id="psave">Сохранить</button><button id="preset">Вернуть как было</button></div>
    </div></div>`;
  $("#psave").onclick = async () => { await api(`/api/prompts/${cur.name}`, { json: { text: $("#ptext").value } }); draftClear(); toast("Шаблон сохранён. Он будет использоваться в новых роликах."); viewPrompts(cur.name); };
  $("#preset").onclick = async () => { if (!confirm("Вернуть исходный шаблон?")) return; await api(`/api/prompts/${cur.name}`, { json: { reset: true } }); viewPrompts(cur.name); };
}

// ------------------------------------------------------------------ music
async function viewMusic() {
  const files = await api("/api/music");
  main.innerHTML = `<h1>Фоновая музыка</h1>
  <div class="card">
    <p>Для каждого ролика программа выбирает отсюда трек, который больше всего подходит по стилю к настроению ролика, и тихо подкладывает его под голос. Если раздел пустой, ролик будет без музыки.</p>
    <p class="muted small">Можно включить в «Новом ролике» «Сочинять новую»: программа сочинит музыку через ElevenLabs (около $0.30 за минуту) и сохранит её сюда со стилем, чтобы использовать снова бесплатно. Стиль можно подписать и у своих треков: по нему программа подбирает музыку.</p>
    <p class="muted small">Где взять музыку без проблем с авторскими правами: в <a href="https://studio.youtube.com/" target="_blank">YouTube Studio → Фонотека</a> (бесплатно, можно использовать на YouTube). Ищите спокойные, атмосферные или «cinematic» треки без слов.</p>
    <div class="row"><input type="file" id="mfile" multiple accept="audio/*" style="display:none"><button class="primary" onclick="document.getElementById('mfile').click()">＋ Добавить треки</button><button id="mopen">📂 Открыть папку с музыкой</button></div>
  </div>
  <div class="card">${files.length ? `<table>${files.map(f => `<tr><td>${esc(f.name)}<div><input type="text" class="mstyle" data-name="${esc(f.name)}" value="${esc(f.style || "")}" placeholder="Стиль, например «весёлый поп, бодрый»" style="margin-top:4px;max-width:320px"></div>${f.made_for ? `<div class="muted small">Сочинена для ролика «${esc(f.made_for)}»</div>` : ""}</td><td><audio controls preload="none" src="/music/${encodeURIComponent(f.name)}"></audio></td><td><button class="sm danger" data-del="${esc(f.name)}">Удалить</button></td></tr>`).join("")}</table>` : `<p class="muted">Треков пока нет.</p>`}</div>`;
  $("#mfile").onchange = async e => {
    for (const f of e.target.files) {
      try { await api(`/api/music/upload?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f }); } catch (err) { toast(err.message, true); }
    }
    toast("Музыка добавлена"); viewMusic();
  };
  $("#mopen").onclick = () => api("/api/open", { json: { what: "music" } });
  main.querySelectorAll(".mstyle").forEach(i => i.onchange = () => api(`/api/music/${encodeURIComponent(i.dataset.name)}/style`, { json: { style: i.value } }).then(() => toast("Стиль сохранён")).catch(e => toast(e.message, true)));
  main.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => { await api(`/api/music/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); viewMusic(); });
}

// ------------------------------------------------------------------ help
// ------------------------------------------------------------------ updates from GitHub
async function checkUpdate(byHand) {
  let u;
  const box = $("#updbox");
  try { u = await api("/api/update/check"); }
  catch (e) {
    // GitHub not reachable right now (often without VPN): keep the link so it can be tried again.
    box.innerHTML = `<a href="#" class="updcheck">Проверить обновления</a>`;
    box.querySelector("a").onclick = async ev => { ev.preventDefault(); await checkUpdate(true); };
    if (byHand) toast("Не удалось проверить обновления: " + e.message + " Если вы в России, включите VPN и нажмите ещё раз.", true);
    return;
  }
  if (!u.available) {
    box.innerHTML = `<a href="#" class="updcheck">Проверить обновления</a>`;
    box.querySelector("a").onclick = async ev => { ev.preventDefault(); await checkUpdate(true); if (!$("#updbox .updbtn")) toast(`У вас последняя версия (${u.current})`); };
    return;
  }
  box.innerHTML = `<button class="updbtn" title="${esc(u.notes)}">⬆ Обновить до версии ${u.latest}${u.label ? `: ${esc(u.label)}` : ""}</button>`;
  box.querySelector("button").onclick = () => {
    if (!u.can_install) {
      if (confirm(`Вышла версия ${u.latest}.\n\n${u.notes}\n\nСкачать новую программу?`) && u.download) window.open(u.download, "_blank");
      return;
    }
    if (!confirm(`Обновить программу до версии ${u.latest}?\n\n${u.notes}\n\nПрограмма закроется и через полминуты откроется уже новой. Ролики, проекты и ключи останутся. Если сейчас делается ролик, он продолжится после обновления.`)) return;
    runUpdate();
  };
}
async function runUpdate() {
  const wait = document.createElement("div"); wait.className = "updwait"; wait.textContent = "Обновляю программу…";
  document.body.appendChild(wait);
  try { await api("/api/update/apply", { json: {} }); }
  catch (e) { wait.remove(); return toast(e.message, true); }
  const poll = setInterval(async () => {
    let st;
    try { st = await api("/api/update/status"); } catch (e) { wait.textContent = "Программа перезапускается…"; return; }
    if (st.status === "error") { clearInterval(poll); wait.remove(); toast(st.error, true); return; }
    wait.textContent = st.status === "restarting" ? "Программа перезапускается…" : `Обновляю программу: ${st.step}…`;
  }, 1000);
}
checkUpdate();
setInterval(checkUpdate, 30 * 60 * 1000);

// ------------------------------------------------------------------ balances
const BAL_SHORT = { anthropic: "Claude", openai: "OpenAI", elevenlabs: "ElevenLabs", fal: "fal.ai", replicate: "Replicate", together: "Together", runware: "Runware" };
function balValue(r) {
  if (r.value == null) return "";
  if (r.unit === "символов") return `${Number(r.value).toLocaleString("ru")} симв.`;
  return `${r.kind === "estimate" ? "≈ " : ""}$${Number(r.value).toFixed(2)}`;
}
function renderBalSide(rows) {
  const shown = rows.filter(r => r.value != null);
  $("#balside").innerHTML = shown.length ? `<div class="bh">Балансы</div>` + shown.map(r =>
    `<div class="bl ${r.low ? "low" : ""}"><span>${esc(BAL_SHORT[r.id] || r.name)}</span><b>${esc(balValue(r))}</b></div>`).join("") : "";
}
async function refreshBalSide() {
  try { renderBalSide((await api("/api/balance")).services); } catch (e) { }
}
refreshBalSide();
setInterval(refreshBalSide, 10 * 60 * 1000);

async function viewBalance() {
  main.innerHTML = `<h1>Балансы сервисов</h1><p class="muted">Загружаю…</p>`;
  const d = await api("/api/balance");
  renderBalSide(d.services);
  const when = t => new Date(t * 1000).toLocaleString("ru");
  const cards = d.services.map(r => {
    let body = "";
    if (r.kind === "error") body = `<div class="banner bad small">${esc(r.error)}</div>`;
    else if (r.unit === "символов") {
      body = `<div class="big ${r.low ? "low" : ""}">${Number(r.value).toLocaleString("ru")} символов</div>
        <p class="small muted">Осталось из ${Number(r.limit).toLocaleString("ru")} по тарифу${r.plan ? ` «${esc(r.plan)}»` : ""}. Это примерно ${r.minutes} мин озвучки.${r.reset ? ` Лимит обновится ${new Date(r.reset * 1000).toLocaleDateString("ru")}.` : ""}</p>`;
    } else if (r.kind === "real") {
      body = `<div class="big ${r.low ? "low" : ""}">$${Number(r.value).toFixed(2)}</div><p class="small muted">Точный остаток, его присылает сам сервис.</p>`;
    } else {
      if (r.error && !r.fal_rights) body += `<div class="banner warn small">Не удалось спросить баланс: ${esc(r.error)}</div>`;
      if (r.fal_rights) body += `<div class="banner warn small">fal.ai не дал посмотреть баланс этим ключом. Чтобы видеть точный остаток, создайте на fal.ai новый ключ с правами «Full» или «Read only» и вставьте его в Настройках. Пока считаю примерно.</div>`;
      body += r.value != null
        ? `<div class="big ${r.low ? "low" : ""}">≈ $${Number(r.value).toFixed(2)}</div>
           <p class="small muted">Примерно: программа берёт сумму, которую вы вписали ${when(r.known_time)} ($${Number(r.known_amount).toFixed(2)}), и вычитает свои траты с того момента.</p>`
        : `<p>${r.id === "fal" ? "Точный остаток сейчас узнать не получилось." : "Этот сервис не сообщает баланс программам."} Впишите остаток из кабинета один раз, и программа будет сама вычитать свои траты.</p>`;
      body += `<div class="row"><span class="small">Сейчас в кабинете:</span> $ <input class="bal-in" data-s="${esc(r.id)}" style="width:110px" placeholder="например 20">
        <button class="sm bal-save" data-s="${esc(r.id)}">Сохранить</button></div>
        <p class="small muted">Сверяйте раз в неделю или после пополнения: счёт программы примерный.${r.spent ? ` Программа потратила здесь около $${Number(r.spent).toFixed(2)}${r.since ? ` с ${new Date(r.since * 1000).toLocaleDateString("ru")}` : ""}.` : ""}</p>`;
    }
    return `<div class="card balcard"><h3>${esc(r.name)}</h3>${body}<a href="${esc(r.billing)}" target="_blank" class="small">Открыть кабинет и пополнить →</a></div>`;
  }).join("");
  main.innerHTML = `<h1>Балансы сервисов</h1>
    <p class="muted">Красным выделено, когда денег осталось мало: меньше $${d.low_usd} или меньше 10% символов озвучки. Слева внизу балансы видны всегда.</p>
    ${cards || `<div class="banner warn">Пока не подключён ни один платный сервис. Вставьте ключи в <a href="#/settings">Настройках</a>.</div>`}
    <button id="bal-refresh">Обновить</button>`;
  $("#bal-refresh").onclick = () => viewBalance();
  main.querySelectorAll(".bal-save").forEach(b => b.onclick = async () => {
    const inp = main.querySelector(`.bal-in[data-s="${b.dataset.s}"]`);
    try { await api("/api/balance/known", { json: { service: b.dataset.s, amount: inp.value } }); toast("Сохранено"); viewBalance(); }
    catch (e) { toast(e.message, true); }
  });
}

function viewHelp() {
  main.innerHTML = `<h1>Как пользоваться</h1>
  <div class="card">
    <h2>Первый запуск</h2>
    <ol>
      <li>Откройте <a href="#/settings">Настройки</a> и вставьте три ключа: нейросеть для сценария (Anthropic или OpenAI), ElevenLabs и сервис картинок. После каждого ключа нажмите «Проверить».</li>
      <li>Добавьте 3–5 треков в <a href="#/music">Музыку</a> (по желанию).</li>
      <li>Нажмите <a href="#/new">Новый ролик</a>, впишите тему или оставьте пустым, нажмите «Запустить».</li>
    </ol>
    <h2>Как делается ролик</h2>
    <ol>
      <li><b>Выбор темы.</b> Нейросеть придумывает 5 тем (число меняется в Настройках, ещё темы можно попросить кнопкой), оценивает каждую по 5 пунктам и выбирает лучшую.</li>
      <li><b>Структура и хуки.</b> Хук на первые 15 секунд, повороты, удерживающие крючки каждые 1–1,5 минуты.</li>
      <li><b>Текст.</b> Около 150 слов на минуту ролика.</li>
      <li><b>Проверка редактором.</b> Отдельный запрос оценивает текст по 10 пунктам и переписывает, пока оценка не выше порога. Все оценки и комментарии на русском.</li>
      <li><b>Сцены.</b> Текст делится на куски по ~9 секунд, для каждого пишется описание картинки.</li>
      <li><b>Перевод и описание.</b> Полный перевод на русский, название, описание, теги и обложка для YouTube.</li>
      <li><b>Шортс.</b> Отдельный короткий сценарий из самого сильного момента.</li>
      <li><b>Озвучка, картинки, сборка.</b> Всё собирается в MP4 1920×1080 и вертикальный 1080×1920.</li>
    </ol>
    <h2>Полезно знать</h2>
    <ul>
      <li>Программа открывается в своём окне. Закрыли окно, и программа выключилась; если в этот момент делается ролик, она спросит, и при следующем запуске продолжит с того же места. Выключить можно и кнопкой «Выключить программу» слева внизу.</li>
      <li>Если выключить программу или Mac, работа продолжится с того же места при следующем запуске.</li>
      <li>Пока делается ролик, Mac не уснёт сам. Не закрывайте крышку, если ноутбук не на зарядке.</li>
      <li>Любой шаг можно переделать кнопкой «Переделать с этого шага». Любую картинку можно перерисовать в шаге «Сцены».</li>
      <li>Проекты (слева «Проекты и ниши»): у каждой ниши свои язык, тема, стиль картинок, голос и настройки. Создаются из готовой ниши одной кнопкой, ролики каждого проекта видны на отдельной вкладке в «Роликах». Для русских роликов выберите в проекте русскоязычный голос.</li>
      <li>Режим «Только шортс» выбирается в «Новом ролике» или в проекте: сценарий, картинки, озвучка и монтаж делаются под вертикальное видео до 60 секунд.</li>
      <li>Эффекты (звук перелистывания, удары, «вжух», наезды, тряска) нейросеть расставляет по смыслу сценария. Режим меняется под готовым видео, пересборка бесплатная.</li>
      <li>Персонажи создаются в проекте (описание по-русски, портрет рисуется или загружается). При запуске ролика нажмите на нужных персонажей: сценарий строится вокруг них, а на картинках они похожи на свой портрет.</li>
      <li>Предметы (товары и вещи) добавляются в разделе «Предметы» с фото. При запуске ролика нажмите на нужные: они появятся в истории и на картинках будут как на ваших фото.</li>
      <li>Анимация сцен выбирается при запуске ролика или в шаге «Анимация сцен». Выгоднее всего «Только хук»: оживает начало ролика, где зрители решают, смотреть ли дальше. Цена показывается до запуска.</li>
      <li>Субтитры включаются галочкой при запуске ролика или кнопкой «Включить/Выключить субтитры» под готовым видео. Видео пересобирается за пару минут бесплатно, без новых запросов к сервисам.</li>
      <li>Все ролики лежат в папке: <code>${esc(META.data_dir)}/jobs</code>. Кнопка «Открыть папку» на странице ролика открывает её в Finder.</li>
    </ul>
  </div>`;
}

$("#quit").onclick = async e => {
  e.preventDefault();
  if (!confirm("Выключить программу? Если сейчас делается ролик, он продолжится с того же места при следующем запуске.")) return;
  await api("/api/shutdown", { json: {} }).catch(() => { });
  clearInterval(timer);
  main.innerHTML = `<div class="card"><h2>Программа выключена</h2><p class="muted">Окно можно закрыть. Чтобы запустить снова, откройте «Авто ютуб» из Программ или из Дока.</p></div>`;
};

route();


// ------------------------------------------------------------------ remake of someone else's video
const RM_STATUS = { new: "Не запускалась", running: "⏳ Идёт работа", done: "✅ Готово", error: "⚠️ Ошибка" };

async function viewRemake(id) {
  if (id) return viewRemakeOne(id);
  const r = await api("/api/remakes");
  main.innerHTML = `<h1>🔁 Переделка ролика</h1>
  <div class="card">
    <p>Загрузите готовый ролик с русской речью, например чужой шортс с вшитыми субтитрами. Программа распознает речь, переведёт её на живой английский, озвучит выбранным голосом, закроет или сотрёт старые субтитры, наложит английские и напишет название, описание и теги под поиск. Можно ещё отразить картинку и чуть изменить кадр, чтобы ролик не выглядел копией.</p>
    <p class="muted small">Стоимость за минуту ролика: распознавание речи около $0.004, перевод и описание около $0.02, озвучка около 900 символов ElevenLabs. Стирание субтитров нейросетью оплачивается отдельно в Replicate. Для своих роликов из программы удобнее кнопка «🌐 Английская версия» на странице ролика: там субтитры стирать не нужно.</p>
    <label class="btn primary" style="display:inline-block;cursor:pointer">📤 Загрузить видео mp4<input type="file" id="rmfile" accept="video/*" style="display:none"></label>
    <span id="rmup" class="muted small"></span>
  </div>
  <div class="card"><h2>Переделки</h2>${r.items.length ? `<table>${r.items.map(x => `<tr><td><a href="#/remake/${x.id}">${esc(x.title || x.name)}</a><div class="muted small">${esc(x.name)} · ${fmtTime(x.duration)}</div></td><td>${RM_STATUS[x.status] || esc(x.status)}</td></tr>`).join("")}</table>` : `<p class="muted">Пока ничего не загружено.</p>`}</div>`;
  $("#rmfile").onchange = async e => {
    const f = e.target.files[0];
    if (!f) return;
    $("#rmup").textContent = `Загружаю ${f.name} (${(f.size / 1048576).toFixed(1)} МБ)…`;
    try {
      const m = await api(`/api/remakes?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
      location.hash = `#/remake/${m.id}`;
    } catch (err) { $("#rmup").textContent = ""; toast(err.message, true); }
  };
}

async function viewRemakeOne(id) {
  clearInterval(timer); timer = null;
  const [m, info] = await Promise.all([api(`/api/remakes/${id}`), api("/api/remakes")]);
  const o = m.opts || {};
  const box = m.box || { y: 75, h: 12 };
  const running = m.status === "running";
  const vertical = m.height > m.width;
  const mins = Math.max(m.duration / 60, 0.1);
  main.innerHTML = `<div class="row"><a href="#/remake" class="muted small">← Все переделки</a></div>
  <h1>${esc(m.title || m.name)}</h1>
  ${m.title_ru ? `<div class="muted">${esc(m.title_ru)}</div>` : ""}
  <div class="row" style="margin:8px 0">${RM_STATUS[m.status] || esc(m.status)}<span class="muted small">${esc(m.name)} · ${m.width}×${m.height} · ${fmtTime(m.duration)}</span></div>
  ${m.status === "error" ? `<div class="banner bad">${nl(m.error || "")}</div>` : ""}
  ${m.files["video.mp4"] ? `<div class="card"><h2>Результат</h2>
    <video controls src="/api/remakes/${id}/file/video.mp4?t=${m.finished || ""}" style="max-width:${vertical ? 320 : 640}px;width:100%"></video>
    <div class="row" style="margin-top:8px"><a class="btn" href="/api/remakes/${id}/file/video.mp4" download="remake.mp4">⬇️ Скачать видео</a>
      ${m.files["thumbnail.jpg"] ? `<a class="btn" href="/api/remakes/${id}/file/thumbnail.jpg" target="_blank">🖼 Обложка</a>` : ""}
      <button data-open>📂 Открыть папку</button></div>
    <div id="rmseo" class="small" style="margin-top:10px"></div></div>` : ""}
  <div class="card"><h2>Настройки переделки</h2>
    <div class="grid2"><div>
      <label class="f">Голос для английской озвучки</label>
      <select id="rm-v" ${running ? "disabled" : ""}><option value="">Загружаю голоса…</option></select>
      <label class="f">Старые субтитры</label>
      <select id="rm-s" ${running ? "disabled" : ""}>${Object.entries(info.subs_modes).map(([k, n]) => `<option value="${k}" ${k === (o.subs_mode || "blur") ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
      <div id="rm-ai" class="muted small" style="display:none">${info.replicate ? "Нейросеть найдёт и сотрёт текст на кадрах и дорисует фон. На лицах и сложном фоне иногда остаются разводы. Видео отправляется в ваш аккаунт Replicate и удаляется оттуда после обработки." : "⚠️ Нужен ключ Replicate: Настройки → 3. Картинки → Replicate → Ключ (выбирать Replicate для картинок не обязательно)."}</div>
      <label class="check"><input type="checkbox" id="rm-r" ${o.restyle !== false ? "checked" : ""} ${running ? "disabled" : ""}> Изменить вид: отразить зеркально, приблизить кадр на 6%, чуть ярче цвета</label>
      <label class="check"><input type="checkbox" id="rm-n" ${o.new_subs !== false ? "checked" : ""} ${running ? "disabled" : ""}> Английские субтитры на видео</label>
      <label class="f">Фоновая музыка</label>
      <select id="rm-m" ${running ? "disabled" : ""}><option value="library">Подобрать из раздела «Музыка»</option><option value="off" ${o.music === "off" ? "selected" : ""}>Без музыки</option></select>
      <p class="muted small">Русский голос из ролика полностью убирается вместе с оригинальным звуком, поэтому под английский голос ставится музыка из вашей библиотеки.</p>
    </div><div>
      <label class="f">Симулятор <span class="hint">включите ролик: жёлтая рамка показывает, что закроется размытой полосой, белый текст показывает новые субтитры</span></label>
      <div id="rm-stage" style="position:relative;display:inline-block;max-width:100%;overflow:hidden;border-radius:8px;background:#000">
        <video id="rm-vid" src="/api/remakes/${id}/file/source.mp4" poster="/api/remakes/${id}/file/preview.jpg" controls playsinline preload="metadata" style="display:block"></video>
        <div id="rm-box" style="position:absolute;left:0;right:0;border:3px solid #f5c400;background:rgba(245,196,0,.25);pointer-events:none"></div>
        <div id="rm-sub" style="position:absolute;text-align:center;color:#fff;font-family:'Arial Black','Arial Bold',Arial,sans-serif;font-weight:900;line-height:1.15;pointer-events:none;white-space:pre-wrap"></div></div>
      <div id="rm-boxui"><label class="f">Старые субтитры, сверху: <span id="rm-yv"></span>% <span class="hint">двигайте, пока жёлтая рамка не закроет старый текст во всём ролике</span></label><input type="range" id="rm-y" min="0" max="95" step="1" value="${box.y}" ${running ? "disabled" : ""}>
      <label class="f">Старые субтитры, высота: <span id="rm-hv"></span>%</label><input type="range" id="rm-h" min="3" max="60" step="1" value="${box.h}" ${running ? "disabled" : ""}></div>
      <div id="rm-subui"><label class="f">Размер новых субтитров: <span id="rm-zv"></span>%</label><input type="range" id="rm-z" min="40" max="250" step="5" value="${Math.round((o.subs_scale || 1) * 100)}" ${running ? "disabled" : ""}>
      <label class="check"><input type="checkbox" id="rm-pa" ${o.subs_pos == null ? "checked" : ""} ${running ? "disabled" : ""}> Ставить новые субтитры автоматически (на место старых, если их закрывает полоса)</label>
      <div id="rm-pui"><label class="f">Новые субтитры, середина текста сверху: <span id="rm-pv"></span>%</label><input type="range" id="rm-p" min="5" max="98" step="1" value="${o.subs_pos ?? 75}" ${running ? "disabled" : ""}></div></div>
    </div></div>
    <p class="muted small">Примерно: распознавание речи ~$${(0.004 * mins).toFixed(3)}, перевод и описание ~$${(0.02 * mins).toFixed(2)}, озвучка ~${Math.round(900 * mins).toLocaleString("ru")} символов ElevenLabs.</p>
    <div class="row"><button class="primary" id="rm-go" ${running ? "disabled" : ""}>${m.status === "new" ? "▶ Запустить переделку" : "↻ Переделать заново"}</button><span class="grow"></span><button class="danger" id="rm-del" ${running ? "disabled" : ""}>Удалить</button></div>
  </div>
  <div class="card"><h2>Журнал</h2><pre class="log" style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(m.log || "Пока пусто.")}</pre></div>`;
  // ---- simulator: the source video with the band to blur and the new subtitles as they will be drawn (ASS sizes)
  const W = m.width, H = m.height, vid = $("#rm-vid"), stage = $("#rm-stage");
  const fit = () => {
    const maxW = Math.max(200, stage.parentElement.clientWidth - 4), k = Math.min(maxW / W, (vertical ? 520 : 400) / H);
    vid.style.width = Math.round(W * k) + "px"; vid.style.height = Math.round(H * k) + "px"; return k;
  };
  const cues = [];
  (m.preview_lines || []).forEach(l => {  // like captions.group: short chunks, timed by their length
    const ws = l.text.split(/\s+/).filter(Boolean), maxW = vertical ? 3 : 8, maxC = vertical ? 18 : 42, chunks = [];
    let cur = [];
    ws.forEach(w => { if (cur.length && (cur.length >= maxW || (cur.join(" ") + " " + w).length > maxC)) { chunks.push(cur.join(" ")); cur = []; } cur.push(w); });
    if (cur.length) chunks.push(cur.join(" "));
    const total = chunks.reduce((a, c) => a + c.length, 0) || 1; let t = l.start;
    chunks.forEach(c => { const dt = (l.end - l.start) * c.length / total; cues.push({ s: t, e: t + dt, text: c }); t += dt; });
  });
  const sample = vertical ? "NEW SUBTITLES" : "This is how the new English subtitles will look";
  const draw = () => {
    const k = fit();
    const y = +$("#rm-y").value, h = +$("#rm-h").value, mode = $("#rm-s").value, on = $("#rm-n").checked;
    const scale = +$("#rm-z").value / 100, auto = $("#rm-pa").checked;
    $("#rm-box").style.top = y + "%"; $("#rm-box").style.height = h + "%";
    $("#rm-box").style.display = mode === "blur" ? "" : "none";
    $("#rm-yv").textContent = y; $("#rm-hv").textContent = h; $("#rm-zv").textContent = Math.round(scale * 100); $("#rm-pv").textContent = $("#rm-p").value;
    $("#rm-boxui").style.display = mode === "blur" ? "" : "none";
    $("#rm-ai").style.display = mode === "ai" ? "" : "none";
    $("#rm-subui").style.display = on ? "" : "none";
    $("#rm-pui").style.display = auto ? "none" : "";
    const sub = $("#rm-sub");
    if (!on) { sub.textContent = ""; return; }
    const size = Math.round((vertical ? 96 : 60) * scale), outline = Math.max(2, Math.round((vertical ? 6 : 4) * scale));
    const centre = !auto ? +$("#rm-p").value : mode === "blur" ? y + h / 2 : null;
    const marginV = centre != null ? Math.max(10, Math.round(H * (1 - centre / 100) - size / 2)) : vertical ? Math.round(H * 0.30) : 70;
    const t = vid.currentTime, cue = cues.find(c => t >= c.s && t < c.e);
    let text = cue ? cue.text : (vid.paused || !cues.length) ? (cues[0] ? cues[0].text : sample) : "";
    if (vertical) text = text.toUpperCase();
    const o = outline * k, sh = [];
    for (let a = 0; a < 16; a++) sh.push(`${(Math.cos(a * Math.PI / 8) * o).toFixed(1)}px ${(Math.sin(a * Math.PI / 8) * o).toFixed(1)}px 0 #000`);
    sh.push(`${2 * k}px ${2 * k}px 0 rgba(0,0,0,.5)`);
    Object.assign(sub.style, { fontSize: size * k + "px", bottom: marginV * k + "px", left: 80 * k + "px", right: 80 * k + "px", textShadow: sh.join(",") });
    sub.textContent = text;
  };
  ["#rm-y", "#rm-h", "#rm-z", "#rm-p"].forEach(k => $(k).oninput = draw);
  ["#rm-s", "#rm-n", "#rm-pa"].forEach(k => $(k).onchange = draw);
  ["timeupdate", "seeked", "play", "pause", "loadedmetadata"].forEach(ev => vid.addEventListener(ev, draw));
  const onResize = () => document.body.contains(vid) ? draw() : window.removeEventListener("resize", onResize);
  window.addEventListener("resize", onResize);
  draw();
  const vsel = $("#rm-v");
  loadVoices().then(vs => { vsel.innerHTML = `<option value="">— выберите голос —</option>` + voiceOptions(vs, o.voice_id || "", "en"); vsel.value = o.voice_id || ""; })
    .catch(e => { vsel.innerHTML = `<option value="">Голоса не загрузились: ${esc(e.message)}</option>`; });
  $("#rm-go").onclick = async () => {
    if (!vsel.value) return toast("Выберите голос", true);
    const op = vsel.options[vsel.selectedIndex];
    await api(`/api/remakes/${id}/start`, { json: { voice_id: vsel.value, voice_name: op.dataset.name || op.text, subs_mode: $("#rm-s").value,
      restyle: $("#rm-r").checked, new_subs: $("#rm-n").checked, music: $("#rm-m").value, box: { y: +$("#rm-y").value, h: +$("#rm-h").value },
      subs_scale: +$("#rm-z").value / 100, subs_pos: $("#rm-pa").checked ? null : +$("#rm-p").value } })
      .then(() => viewRemakeOne(id)).catch(e => toast(e.message, true));
  };
  $("#rm-del").onclick = async () => { if (!confirm("Удалить эту переделку со всеми файлами?")) return; await api(`/api/remakes/${id}`, { method: "DELETE" }); location.hash = "#/remake"; };
  const op = main.querySelector("[data-open]");
  if (op) op.onclick = () => api("/api/open", { json: { what: "remake", id } }).catch(e => toast(e.message, true));
  if (m.files["youtube.txt"]) {
    const t = await (await fetch(`/api/remakes/${id}/file/youtube.txt`)).text();
    $("#rmseo").innerHTML = `<pre style="white-space:pre-wrap">${esc(t)}</pre>${copyBtn(t)}`;
  }
  if (running) timer = setInterval(async () => {
    const n = await api(`/api/remakes/${id}`).catch(() => null);
    if (!n || location.hash !== `#/remake/${id}`) return;
    if (n.status !== "running") return viewRemakeOne(id);
    const log = main.querySelector("pre.log"); if (log) log.textContent = n.log;
  }, 3000);
}

// ------------------------------------------------------------------ AI producer: why competitors' videos worked
const PR_STATUS = { running: "⏳ Идёт разбор", done: "✅ Готово", error: "⚠️ Ошибка" };
function prList(arr) { return arr && arr.length ? `<ul>${arr.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""; }
function prViews(v) { return v ? `${Number(v).toLocaleString("ru")} просмотров` : ""; }
function prSection(title, body) { return body ? `<div style="margin-top:8px"><b>${title}</b><div>${body}</div></div>` : ""; }

async function viewProducer(id) {
  if (id) return viewProducerOne(id);
  clearInterval(timer); timer = null;
  const r = await api("/api/producer");
  main.innerHTML = `<h1>🎓 ИИ продюсер</h1>
  <div class="card">
    <p>Дайте ссылку на ролик или на целый канал YouTube, загрузите видео или вставьте текст сценария. Продюсер разберёт смысл и сценарий, объяснит, почему ролик набрал просмотры, и выпишет сильные стороны. Готовый разбор можно сохранить в «📚 Хранилище сценариев» нужной ниши, и сценарист будет на него опираться.</p>
    <label class="f">Ссылка на ролик, шортс или канал YouTube</label>
    <input id="pr-link" placeholder="https://www.youtube.com/shorts/…  или  https://www.youtube.com/@канал">
    <label class="f">Если это канал: сколько самых просматриваемых роликов разобрать</label>
    <select id="pr-count">${[3, 5, 7, 10].map(n => `<option ${n === 5 ? "selected" : ""}>${n}</option>`).join("")}</select>
    <label class="f">…или вставьте текст сценария</label>
    <textarea id="pr-text" rows="5" placeholder="Текст чужого ролика, который вам понравился"></textarea>
    <div class="row" style="margin-top:10px"><button class="primary" id="pr-go">▶ Разобрать</button>
      <label class="btn" style="display:inline-block;cursor:pointer">📤 Загрузить видео<input type="file" id="pr-file" accept="video/*" style="display:none"></label>
      <span id="pr-up" class="muted small"></span></div>
    <p class="muted small">Разбор одного ролика стоит примерно $0.03–0.08 у нейросети сценария. Если у ролика нет субтитров, речь распознаёт ElevenLabs (около $0.004 за минуту).</p>
  </div>
  <div class="card"><h2>Разборы</h2>${r.items.length ? `<table>${r.items.map(x => `<tr><td><a href="#/producer/${x.id}">${esc(x.title || x.source)}</a><div class="muted small">${esc(x.source)}${x.count ? ` · роликов: ${x.count}` : ""}</div></td><td>${PR_STATUS[x.status] || esc(x.status)}</td></tr>`).join("")}</table>` : `<p class="muted">Пока ни одного разбора.</p>`}</div>
  <div class="card"><h2>Загрузчик YouTube</h2>
    <p class="muted small">YouTube часто меняет сайт. Если ссылки перестали разбираться, обновите загрузчик. Сейчас: ${r.downloader ? `версия ${esc(r.downloader)}` : "не установлен"}.</p>
    <button id="pr-upd">🔄 Обновить загрузчик YouTube</button>
    <label class="f">Вход в YouTube <span class="hint">если YouTube принимает программу за робота, она смотрит ролики под вашим входом из браузера, как вы сами</span></label>
    <select id="pr-br"><option value="">Автоматически (найти браузер, где вы вошли в YouTube)</option>${Object.entries(r.browsers).map(([k, n]) => `<option value="${k}" ${k === r.browser ? "selected" : ""}>${esc(n)}${r.installed.includes(k) ? "" : " (не найден)"}</option>`).join("")}<option value="none" ${r.browser === "none" ? "selected" : ""}>Не использовать</option></select>
    <p class="muted small">Войдите в свой аккаунт YouTube в выбранном браузере. Для Chrome, Edge, Brave, Opera и Vivaldi Mac один раз спросит пароль: нажмите «Разрешать всегда». Для Safari нужно включить «Авто ютуб» в Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску. Проще всего Firefox или Chrome. Яндекс Браузер не подходит.</p></div>`;
  $("#pr-go").onclick = async () => {
    const link = $("#pr-link").value.trim(), text = $("#pr-text").value.trim();
    if (!link && !text) return toast("Вставьте ссылку или текст", true);
    $("#pr-go").disabled = true;
    try {
      const m = await api("/api/producer", { json: { link, text: link ? "" : text, count: +$("#pr-count").value } });
      location.hash = `#/producer/${m.id}`;
    } catch (e) { $("#pr-go").disabled = false; toast(e.message, true); }
  };
  $("#pr-file").onchange = async e => {
    const f = e.target.files[0];
    if (!f) return;
    $("#pr-up").textContent = `Загружаю ${f.name} (${(f.size / 1048576).toFixed(1)} МБ)…`;
    try {
      const m = await api(`/api/producer?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
      location.hash = `#/producer/${m.id}`;
    } catch (err) { $("#pr-up").textContent = ""; toast(err.message, true); }
  };
  $("#pr-br").onchange = e => api("/api/settings", { json: { yt_cookies_browser: e.target.value } }).then(() => toast("Сохранено")).catch(er => toast(er.message, true));
  $("#pr-upd").onclick = async () => {
    const b = $("#pr-upd"); b.disabled = true; b.textContent = "⏳ Обновляю, это может занять пару минут…";
    try { const v = await api("/api/producer/update-downloader", { method: "POST" }); toast(`Загрузчик обновлён: ${v.version}`); viewProducer(); }
    catch (e) { toast(e.message, true); b.disabled = false; b.textContent = "🔄 Обновить загрузчик YouTube"; }
  };
}

function prItem(x, i, canPick) {
  const head = `${canPick ? `<input type="checkbox" class="pr-pick" data-i="${i}" checked> ` : ""}<b>${esc(x.title || "Ролик")}</b>`;
  const sub = [x.channel, prViews(x.views), x.duration ? fmtTime(x.duration) : "", x.kind === "short" ? "шортс" : x.kind === "video" ? "длинный" : ""].filter(Boolean).join(" · ");
  return `<div class="card">
    <label style="display:block">${head}</label>
    <div class="muted small">${esc(sub)}${x.source ? ` · <a href="${esc(x.source)}" target="_blank">открыть</a>` : ""}</div>
    ${prSection("О чём ролик", nl(x.summary_ru))}
    ${prSection("Почему сработал", nl(x.why_ru))}
    ${prSection("Хук (начало)", nl(x.hook_ru))}
    ${prSection("Структура", Array.isArray(x.structure_ru) ? prList(x.structure_ru) : nl(x.structure_ru))}
    ${prSection("Сильные стороны", prList(x.strengths_ru))}
    ${prSection("Что можно лучше", prList(x.weaknesses_ru))}
    ${prSection("Название и обложка", nl(x.packaging_ru))}
    ${prSection("Похожие темы для нас", prList(x.topics_ru))}
    ${prSection("Приёмы для сценариста (на английском, так их читает нейросеть)", prList(x.techniques))}
    ${x.script || x.script_ru ? `<details style="margin-top:8px"><summary>Текст ролика</summary><pre style="white-space:pre-wrap">${esc(x.script_ru || x.script)}</pre></details>` : ""}
  </div>`;
}

async function viewProducerOne(id) {
  clearInterval(timer); timer = null;
  const [m, projs] = await Promise.all([api(`/api/producer/${id}`), api("/api/projects").catch(() => [])]);
  const running = m.status === "running", items = m.items || [], ch = m.channel;
  let last = ""; try { last = localStorage.getItem("ay_lib_project") || ""; } catch (e) { }
  main.innerHTML = `<div class="row"><a href="#/producer" class="muted small">← Все разборы</a></div>
  <h1>${esc(m.title || m.source)}</h1>
  <div class="row" style="margin:8px 0">${PR_STATUS[m.status] || esc(m.status)}<span class="muted small">${esc(m.source)}</span></div>
  ${m.status === "error" ? `<div class="banner bad">${nl(m.error || "")}</div>` : ""}
  ${!running && items.length ? `<div class="card"><h2>📚 Сохранить в хранилище</h2>
    ${projs.length ? `<label class="f">Ниша (проект)</label>
    <select id="pr-proj">${projs.map(p => `<option value="${p.id}" ${p.id === last ? "selected" : ""}>${esc((p.icon || "") + " " + (p.name || "Без названия"))}</option>`).join("")}</select>
    ${items.length > 1 ? `<p class="muted small">Сохранятся ролики с галочкой. Галочки стоят у каждого ролика ниже.</p>` : ""}
    ${ch ? `<label class="check"><input type="checkbox" id="pr-ch" checked> И общую формулу канала</label>` : ""}
    <div class="row" style="margin-top:8px"><button class="primary" id="pr-save">📚 Сохранить в хранилище</button></div>`
      : `<p class="muted">Сначала создайте проект в разделе «📁 Проекты и ниши»: у каждой ниши своё хранилище.</p>`}</div>` : ""}
  ${ch ? `<div class="card"><h2>Формула канала</h2>
    ${prSection("Что за канал", nl(ch.summary_ru))}${prSection("Формула хитов", nl(ch.formula_ru))}
    ${prSection("Сильные стороны", prList(ch.strengths_ru))}${prSection("Как обойти этот канал", nl(ch.advice_ru))}
    ${prSection("Темы для нас", prList(ch.topics_ru))}${prSection("Приёмы для сценариста", prList(ch.techniques))}</div>` : ""}
  ${items.map((x, i) => prItem(x, i, !running && items.length > 1)).join("")}
  <div class="card"><h2>Журнал</h2><pre class="log" style="white-space:pre-wrap;max-height:300px;overflow:auto">${esc(m.log || "Пока пусто.")}</pre>
    <div class="row"><span class="grow"></span><button class="danger" id="pr-del" ${running ? "disabled" : ""}>Удалить разбор</button></div></div>`;
  const save = $("#pr-save");
  if (save) save.onclick = async () => {
    const pid = $("#pr-proj").value;
    const picks = [...main.querySelectorAll(".pr-pick")];
    const indexes = picks.length ? picks.filter(c => c.checked).map(c => +c.dataset.i) : null;
    const withCh = $("#pr-ch") ? $("#pr-ch").checked : false;
    if (indexes && !indexes.length && !withCh) return toast("Отметьте хотя бы один ролик", true);
    try {
      const r = await api(`/api/producer/${id}/save`, { json: { project_id: pid, indexes, with_channel: withCh } });
      try { localStorage.setItem("ay_lib_project", pid); } catch (e) { }
      toast(`Сохранено в хранилище: ${r.added}`);
    } catch (e) { toast(e.message, true); }
  };
  $("#pr-del").onclick = async () => { if (!confirm("Удалить этот разбор? В хранилище сохранённое останется.")) return; await api(`/api/producer/${id}`, { method: "DELETE" }); location.hash = "#/producer"; };
  if (running) timer = setInterval(async () => {
    const n = await api(`/api/producer/${id}`).catch(() => null);
    if (!n || location.hash !== `#/producer/${id}`) return;
    if (n.status !== "running" || (n.items || []).length !== items.length) return viewProducerOne(id);
    const log = main.querySelector("pre.log"); if (log) log.textContent = n.log;
  }, 3000);
}

// ------------------------------------------------------------------ script library of a niche
async function viewLibrary() {
  clearInterval(timer); timer = null;
  const projs = await api("/api/projects").catch(() => []);
  if (!projs.length) {
    main.innerHTML = `<h1>📚 Хранилище сценариев</h1><div class="card"><p>Хранилище есть у каждой ниши. Сначала создайте проект в разделе <a href="#/projects">«📁 Проекты и ниши»</a>.</p></div>`;
    return;
  }
  let pid = ""; try { pid = localStorage.getItem("ay_lib_project") || ""; } catch (e) { }
  if (!projs.some(p => p.id === pid)) pid = projs[0].id;
  const proj = projs.find(p => p.id === pid);
  const lib = await api(`/api/projects/${pid}/library`);
  const items = [...lib.items].reverse();
  const topics = [...new Set(lib.items.flatMap(x => x.topics_ru || []))].slice(-40).reverse();
  main.innerHTML = `<h1>📚 Хранилище сценариев</h1>
  <div class="card">
    <label class="f">Ниша</label>
    <select id="lb-proj">${projs.map(p => `<option value="${p.id}" ${p.id === pid ? "selected" : ""}>${esc((p.icon || "") + " " + (p.name || "Без названия"))}</option>`).join("")}</select>
    <p class="muted small">Здесь лежат разборы роликов, которые сработали. Сценарист этой ниши читает правила ниши и последние разборы перед каждым новым роликом: учится приёмам, но не копирует темы и текст. Новые ниши появляются в списке сами, когда вы создаёте проект.</p>
    <label class="f">Описание ниши <span class="hint">то же поле, что в проекте</span></label>
    <textarea id="lb-niche" rows="4">${esc(proj.niche || "")}</textarea>
    <div class="row" style="margin-top:6px"><button id="lb-niche-save">Сохранить описание</button></div>
  </div>
  <div class="card"><h2>Правила ниши</h2>
    <p class="muted small">Продюсер собирает их из всех разборов этой ниши. Можно поправить текст руками: тогда сценарист будет читать ваш вариант.</p>
    <textarea id="lb-pb" rows="10" placeholder="Пока пусто. Сохраните сюда несколько разборов и нажмите «Собрать правила».">${esc(lib.playbook_ru || "")}</textarea>
    <div class="row" style="margin-top:6px"><button class="primary" id="lb-build" ${lib.items.length ? "" : "disabled"}>✨ Собрать правила из разборов</button><button id="lb-pb-save">Сохранить мои правки</button>
      ${lib.playbook_at ? `<span class="muted small">обновлено ${new Date(lib.playbook_at * 1000).toLocaleString("ru")}</span>` : ""}</div>
  </div>
  ${topics.length ? `<div class="card"><h2>Темы из разборов</h2><p class="muted small">Идеи, которые продюсер предложил по мотивам удачных роликов. Можно брать как свою тему на странице нового ролика.</p>${prList(topics)}</div>` : ""}
  <div class="card"><h2>Разборы (${lib.items.length})</h2>
    ${items.length ? items.map(x => `<details style="margin-bottom:8px"><summary><b>${x.type === "channel" ? "📺 " : x.kind === "short" ? "📱 " : "🎬 "}${esc(x.title || "Без названия")}</b> <span class="muted small">${esc([x.channel, prViews(x.views)].filter(Boolean).join(" · "))}</span></summary>
      ${x.source ? `<div class="small"><a href="${esc(x.source)}" target="_blank">${esc(x.source)}</a></div>` : ""}
      ${prSection("О чём", nl(x.summary_ru))}${prSection(x.type === "channel" ? "Формула хитов" : "Почему сработал", nl(x.why_ru))}
      ${prSection("Хук", nl(x.hook_ru))}${prSection("Сильные стороны", prList(x.strengths_ru))}
      ${prSection("Приёмы для сценариста", prList(x.techniques))}
      ${x.script || x.script_ru ? `<details><summary>Текст ролика</summary><pre style="white-space:pre-wrap">${esc(x.script_ru || x.script)}</pre></details>` : ""}
      <div class="row" style="margin-top:6px"><button class="danger sm" data-lbdel="${x.id}">Убрать из хранилища</button></div></details>`).join("")
      : `<p class="muted">Пусто. Откройте <a href="#/producer">«🎓 ИИ продюсер»</a>, разберите удачный ролик и сохраните разбор сюда.</p>`}
  </div>`;
  $("#lb-proj").onchange = e => { try { localStorage.setItem("ay_lib_project", e.target.value); } catch (er) { } viewLibrary(); };
  $("#lb-niche-save").onclick = () => api(`/api/projects/${pid}`, { json: { niche: $("#lb-niche").value } }).then(() => toast("Описание ниши сохранено")).catch(e => toast(e.message, true));
  $("#lb-pb-save").onclick = () => api(`/api/projects/${pid}/library/playbook`, { json: { playbook_ru: $("#lb-pb").value } }).then(() => toast("Правила сохранены")).catch(e => toast(e.message, true));
  $("#lb-build").onclick = async () => {
    const b = $("#lb-build"); b.disabled = true; b.textContent = "⏳ Собираю правила…";
    try { await api(`/api/projects/${pid}/library/build`, { method: "POST" }); toast("Правила собраны"); viewLibrary(); }
    catch (e) { toast(e.message, true); b.disabled = false; b.textContent = "✨ Собрать правила из разборов"; }
  };
  main.querySelectorAll("[data-lbdel]").forEach(b => b.onclick = async () => {
    if (!confirm("Убрать этот разбор из хранилища?")) return;
    await api(`/api/projects/${pid}/library/${b.dataset.lbdel}`, { method: "DELETE" }).catch(e => toast(e.message, true));
    viewLibrary();
  });
}
