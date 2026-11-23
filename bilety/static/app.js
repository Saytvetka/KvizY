/* BiletY — весь интерфейс. Без сборки и библиотек.
   Устройство:
   1. Общее: api(), экранирование, всплывашки, ошибки с «Повторить», меню, анимация загрузки.
   2. Роутер по адресу после # (#/create, #/r/<id> …). Каждая страница — функция page*(…).
   3. Страницы: вход, привет, главная (философы), создать, создание, результат (билеты / конспект / ответ /
      вопросы), прохождение вопросов, история, настройки.
   Правило: страницу рисуем один раз; потом меняем только нужные кусочки, чтобы не сбивалась прокрутка. */
"use strict";

const PALETTE_NAMES = {
  classic: "Классика ч/б", dark: "Тёмная", beige: "Бумага", bluegray: "Серо-голубая", sage: "Шалфей",
  terminal: "Терминал", sakura: "Сакура", desert: "Пустыня", deep: "Глубина", neon: "Неон",
};
const PALETTE_GROUPS = [["Классика", ["classic"]], ["Спокойные", ["dark", "beige", "bluegray", "sage"]],
  ["Необычные", ["terminal", "sakura", "desert", "deep", "neon"]]];
const PALETTE_BG = {classic:"#ffffff",dark:"#121212",beige:"#f4ede1",bluegray:"#edf1f5",sage:"#edf1ea",terminal:"#050a05",sakura:"#fff4f6",desert:"#f2e5cf",deep:"#0a1828",neon:"#0c0a19"};
const PURPOSE = { tickets: "билеты", summary: "конспект", answer: "ответ на вопрос", questions: "вопросы", ocr: "распознавание фото", ping: "проверка связи" };
const TYPE_NAME = { single: "один ответ", multi: "несколько ответов", tf: "верно или неверно" };
const FUNC_ORDER = ["tickets", "summary", "answer", "questions"];
// Иконки функций — тоже пиксельные (рисует pixel.js).
const FUNC_ICON = { tickets: { kind: "book", color: "#2f4a6b", title: "Билеты" }, summary: { kind: "scroll", title: "Конспект" },
  answer: { kind: "lamp", title: "Ответ" }, questions: { kind: "bust", title: "Вопросы" } };

const S = { boot: null, ready: false, phil: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const view = $("#view");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}
const num = (v) => String(Math.round((+v || 0) * 10) / 10).replace(".", ",");
const rub = (v) => `${(+v || 0).toFixed(2).replace(".", ",")} ₽`;
const big = (n) => (+n || 0).toLocaleString("ru-RU");
const secText = (sec) => (sec >= 60 ? `${num(sec / 60)} мин` : `${sec} с`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const levels = () => (S.boot && S.boot.limits.levels) || {};
const funcs = () => (S.boot && S.boot.functions) || {};

/** Маленький «маркдаун» ответа ИИ: абзацы, списки «- » и «1. », **жирный**, *курсив*. Всё экранируется. */
function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>");
  const out = [];
  let list = null;
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    const m = line.match(/^(?:[-•*]|(\d+)[.)])\s+(.*)$/);
    if (m) {
      const kind = m[1] ? "ol" : "ul";
      if (!list || list.kind !== kind) { if (list) out.push(`</${list.kind}>`); list = { kind }; out.push(`<${kind}>`); }
      out.push(`<li>${inline(m[2])}</li>`);
      continue;
    }
    if (list) { out.push(`</${list.kind}>`); list = null; }
    if (line) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list.kind}>`);
  return out.join("");
}

// ---------- API ----------
class ApiError extends Error {
  constructor(message, status, kind) { super(message); this.status = status; this.kind = kind; }
}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
  if (opts.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
  if (opts.form) init.body = opts.form;
  let resp;
  try {
    resp = await fetch(path, init);
  } catch (e) {
    throw new ApiError("Нет связи с сервером. Проверь интернет и нажми «Повторить».", 0, "network");
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (resp.status === 401 && !opts.noAuthRedirect) { showLogin(); throw new ApiError("Нужно войти", 401, "auth"); }
  if (!resp.ok) {
    const msg = (data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `Сервер ответил ${resp.status}`;
    throw new ApiError(msg, resp.status, resp.status >= 500 ? "server" : "client");
  }
  return data;
}

// ---------- Мелочи интерфейса ----------
let toastTimer = null;
function toast(text, ms = 3500) {
  let t = $(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
/** Ошибка не молчит: что случилось + кнопка «Повторить». */
function errorBox(target, err, retry, title = "Не получилось") {
  const box = document.createElement("div");
  box.className = "error-box"; box.setAttribute("role", "alert");
  box.innerHTML = `<b>${esc(title)}</b><div>${esc(err && err.message ? err.message : err)}</div>` +
    (retry ? `<button class="btn small" type="button">Повторить</button>` : "");
  if (retry) $("button", box).onclick = () => { box.remove(); retry(); };
  if (typeof target === "string") target = $(target);
  target.innerHTML = ""; target.appendChild(box);
  return box;
}
function modal(html, cls = "") {
  const shade = document.createElement("div");
  shade.className = "modal-shade";
  shade.innerHTML = `<div class="modal ${cls}" role="dialog" aria-modal="true">${html}</div>`;
  shade.onclick = (e) => { if (e.target === shade || e.target.closest("[data-close]")) shade.remove(); };
  document.body.appendChild(shade);
  const btn = shade.querySelector("[data-close], button");
  if (btn) btn.focus({ preventScroll: true });
  return shade;
}
function confirmModal(text, okText = "Да", danger = false) {
  return new Promise((resolve) => {
    const shade = modal(`<p>${esc(text)}</p><div class="actions"><button class="btn secondary" data-a="no">Отмена</button>
      <button class="btn ${danger ? "danger" : ""}" data-a="yes">${esc(okText)}</button></div>`);
    shade.onclick = (e) => {
      const a = e.target.dataset && e.target.dataset.a;
      if (e.target === shade || a) { shade.remove(); resolve(a === "yes"); }
    };
  });
}
function setTitle(t) { $("#top-title").textContent = t || "BiletY"; document.title = t ? `${t} — BiletY` : "BiletY"; }
/** Анимация загрузки: пиксели по очереди «пишут» строчки. */
function loaderHTML(small = true, id = "") { return `<div class="pxload${small ? " small" : ""}" ${id ? `id="${id}"` : ""} aria-hidden="true">${"<i></i>".repeat(20)}</div>`; }
function loading(text = "загружаю…") { view.innerHTML = `<div class="center">${loaderHTML(false)}<div class="muted">${esc(text)}</div></div>`; }

const WAKE_TEXTS = [
  [0, "открываю…"], [1.5, "подключаюсь к серверу…"], [4, "сервер просыпается после перерыва…"],
  [9, "ещё чуть-чуть — первый запуск после долгого сна дольше обычного"], [20, "что-то долго… проверь интернет. Я продолжаю пытаться"],
];
/** Ожидание с «живым» текстом: texts — [[с какой секунды, текст], …]. Возвращает функцию «стоп». */
function waitTicker(textEl, secEl, texts) {
  const t0 = Date.now();
  let shown = "";
  const tick = () => {
    const sec = (Date.now() - t0) / 1000;
    const text = texts.filter(([from]) => sec >= from).pop()[1];
    if (text !== shown && textEl) {
      shown = text;
      textEl.style.opacity = 0;
      setTimeout(() => { textEl.textContent = text; textEl.style.opacity = 1; }, 150);
    }
    if (secEl) secEl.textContent = sec >= 2 ? `${Math.floor(sec)} с` : "";
  };
  tick();
  const timer = setInterval(tick, 250);
  return () => clearInterval(timer);
}
let stopBoot = () => {};
function applyPalette(p) {
  document.documentElement.setAttribute("data-palette", p);
  $('meta[name="theme-color"]').setAttribute("content", PALETTE_BG[p] || "#ffffff");
  try { localStorage.setItem("bilety.palette", p); } catch (e) {}
}
function store(key, val) { try { if (val === undefined) return JSON.parse(localStorage.getItem("bilety." + key) || "null"); localStorage.setItem("bilety." + key, JSON.stringify(val)); } catch (e) { return null; } return val; }
function funcIcon(f) { const i = FUNC_ICON[f]; return Pixel.second({ name_ru: i.title, portrait: { hair: "curly", beard: "long", hair_color: "#777777" }, second: i }); }
function topicTag(t, show) { return show && t ? `<span class="topic">${esc(t)}</span>` : ""; }

// ---------- Меню ----------
function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  $("#menu-btn").setAttribute("aria-expanded", String(open));
}
$("#menu-btn").onclick = () => setMenu(!document.body.classList.contains("menu-open"));
$("#drawer-shade").onclick = () => setMenu(false);
$$("#drawer a").forEach((a) => (a.onclick = () => setMenu(false)));

// ---------- Роутер ----------
const routes = [
  [/^\/?$/, pageHello], [/^\/home$/, pageHome], [/^\/create(?:\?(.*))?$/, pageCreate], [/^\/work\/(\w+)$/, pageWork],
  [/^\/r\/(\w+)$/, pageResult], [/^\/play\/(\w+)$/, pagePlay], [/^\/history$/, pageHistory], [/^\/settings$/, pageSettings],
];
let leaveHooks = [];
function onLeave(fn) { leaveHooks.push(fn); }
function go(path, replace = false) {
  if (location.hash === "#" + path) { route(); return; }
  if (replace) location.replace("#" + path); else location.hash = path;
}
async function route() {
  leaveHooks.forEach((f) => { try { f(); } catch (e) { /* страница уже ушла */ } }); leaveHooks = [];
  $$(".modal-shade").forEach((m) => m.remove());  // окна прошлой страницы закрываем
  setMenu(false);
  if (!S.ready) return;
  const path = location.hash.replace(/^#/, "") || "/";
  const nav = { "": "hello", home: "home", create: "create", work: "create", r: "history", play: "history", history: "history", settings: "settings" }[path.split(/[/?]/)[1] || ""];
  $$("#drawer a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  window.scrollTo(0, 0);
  for (const [re, fn] of routes) {
    const m = path.match(re);
    if (m) { try { await fn(...m.slice(1)); } catch (e) { if (e.kind !== "auth") errorBox(view, e, route); } return; }
  }
  pageHello();
}
window.addEventListener("hashchange", route);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  $$(".modal-shade").forEach((m) => m.remove());
  setMenu(false);
});

// ---------- Запуск ----------
async function boot() {
  const bootEl = $("#boot");
  bootEl.hidden = false;
  $("#boot-logo").innerHTML = Pixel.logo();
  stopBoot = waitTicker($("#boot-text"), $("#boot-sec"), WAKE_TEXTS);
  try {
    S.boot = await api("/api/bootstrap");
    applyPalette(S.boot.palette || "classic");
    S.ready = true;
    stopBoot();
    bootEl.hidden = true;
    route();
  } catch (e) {
    if (e.kind === "auth") return;
    stopBoot();
    bootEl.hidden = true;
    S.ready = false;
    errorBox(view, e, boot, "Не удалось подключиться");
  }
}
function showLogin() {
  S.ready = false;
  stopBoot();
  $("#boot").hidden = true;
  setTitle("Вход");
  view.innerHTML = `<div class="hello"><div class="px-logo">${Pixel.logo()}</div><div class="logo">BiletY</div>
    <p class="muted">Введи пароль один раз — дальше этот iPhone или iPad будет входить сам.</p></div>
    <form id="login" class="stack" autocomplete="on">
      <input type="text" name="username" autocomplete="username" value="owner" hidden>
      <label class="field"><span>Пароль</span><input type="password" name="password" autocomplete="current-password" required></label>
      <button class="btn block" type="submit">Войти</button><div id="login-err"></div></form>`;
  const f = $("#login");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("button", f); btn.disabled = true;
    try {
      await api("/api/login", { method: "POST", json: { password: f.password.value }, noAuthRedirect: true });
      boot();
    } catch (err) { errorBox("#login-err", err, null, "Не вошли"); btn.disabled = false; }
  };
}

// ======================= СТРАНИЦЫ =======================

function pageHello() {
  setTitle("Привет");
  const h = new Date().getHours();
  const hello = h < 6 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
  view.innerHTML = `<div class="hello"><div class="px-logo">${Pixel.logo()}</div><h1>${hello}!</h1>
    <p>Это BiletY — помощник к экзамену. Загрузи лекции, слайды или фото конспекта, и я составлю билеты, конспект,
    отвечу на вопрос по твоим файлам или сделаю вопросы для самопроверки.</p>
    <p class="muted">Меню страниц — стрелка слева сверху.</p></div>
    <div class="stack"><a class="btn block" href="#/create">Создать</a><a class="btn secondary block" href="#/home">Главная</a></div>`;
}

// ---------- Главная: философы ----------
async function loadPhilosophers() {
  if (S.phil) return S.phil;
  const r = await fetch("/static/philosophers.json", { credentials: "same-origin" });
  if (!r.ok) throw new ApiError("Не открылся список философов (static/philosophers.json).", r.status, "client");
  let d;
  try { d = await r.json(); } catch (e) { throw new ApiError("В файле philosophers.json ошибка: это не правильный JSON. Проверь запятые и кавычки.", 0, "client"); }
  S.phil = (Array.isArray(d) ? d : d.philosophers || []).filter((p) => p && (p.name_ru || p.name_native));
  return S.phil;
}
async function pageHome() {
  setTitle("Главная");
  view.innerHTML = `<div class="center">${loaderHTML(false)}<div class="muted">собираю философов…</div></div>`;
  const list = await loadPhilosophers();
  if (!list.length) { view.innerHTML = `<h1>Главная</h1><p class="muted">Список философов пуст. Заполни файл static/philosophers.json.</p>`; return; }
  view.innerHTML = `<h1>Философы</h1><p class="muted small">Нажми на портрет — откроется мини-биография.</p>
    <div class="phil-grid" id="phil">${list.map((p, i) => `<button type="button" class="phil enter" data-i="${i}" style="animation-delay:${Math.min(i, 16) * 40}ms">
      ${Pixel.portrait(p)}<span class="nn">${esc(p.name_native || p.name_ru)}</span><span class="nr">${esc(p.name_ru || "")}</span></button>`).join("")}</div>`;
  $("#phil").onclick = (e) => { const b = e.target.closest(".phil"); if (b) showBio(list[+b.dataset.i]); };
}
function showBio(p) {
  const sec = p.second || {};
  const secCaption = sec.title || (sec.kind === "bust" || !sec.kind ? "бюст" : "");
  modal(`<div class="bio"><h2>${esc(p.name_ru || p.name_native)}</h2><div class="native">${esc(p.name_native || "")}${p.years ? " · " + esc(p.years) : ""}</div>
    <div class="pair"><figure>${Pixel.portrait(p)}<figcaption>портрет</figcaption></figure><figure>${Pixel.second(p)}<figcaption>${esc(secCaption)}</figcaption></figure></div>
    <p>${esc(p.bio || "Биография пока не написана.")}</p>
    <div class="actions"><button class="btn secondary" data-close>Закрыть</button></div></div>`, "wide");
}

// ---------- Создать ----------
const IMG_RE = /\.(jpe?g|png|webp|heic|heif|gif|bmp|tiff?)$/i;
const OK_RE = /\.(pdf|docx|pptx|txt|md|html?|csv|rtf|jpe?g|png|webp|heic|heif|gif|bmp|tiff?)$/i;
/** Фото → JPEG не больше 2000 px (быстрее грузится, HEIC с iPhone становится JPEG). Не вышло — отправляем как есть. */
async function shrinkImage(file) {
  if (!(file.type.startsWith("image/") || IMG_RE.test(file.name)) || file.type === "image/gif") return file;
  try {
    const url = URL.createObjectURL(file);
    const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; });
    const k = Math.min(1, 2000 / Math.max(img.naturalWidth, img.naturalHeight));
    const c = document.createElement("canvas");
    c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    URL.revokeObjectURL(url);
    const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.85));
    if (!blob) return file;
    return new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch (e) { return file; }
}
function quizLine() {
  const q = S.boot.quiz, l = levels()[q.difficulty] || {};
  return `${q.count} вопросов · ${esc(l.name || "")} · таймер ${q.timer ? `вкл (${secText(l.timer_sec)})` : "выкл"} · подсказки ${q.hints ? "вкл" : "выкл"}`;
}

async function pageCreate(query = "") {
  setTitle("Создать");
  const qs = new URLSearchParams(query || "");
  const lim = S.boot.limits;
  const files = [];
  let from = null;
  if (qs.get("from")) {
    loading();
    try { from = await api(`/api/results/${qs.get("from")}`); } catch (e) { from = null; toast("Тот материал уже удалён — загрузи файлы заново."); }
  }
  let func = qs.get("func") || store("func") || "tickets";
  if (!funcs()[func]) func = "tickets";
  const fromNames = from ? (from.sources || []).map((s) => s.name).join(", ") : "";
  view.innerHTML = `<h1>Создать</h1>
  <form id="cf" autocomplete="off">
    <section class="panel"><h2><span class="step-n">1</span>Материал</h2>
      ${from ? `<div class="from-box"><span><b>Тот же материал:</b> ${esc(fromNames || from.title)}</span><a class="btn secondary small" href="#/create">Другой</a></div>
        <p class="muted small">Файлы второй раз грузить не нужно — текст уже есть.</p>` : `
      <label class="drop" id="drop"><b>Выбрать файлы или фото</b><span class="muted small">до ${lim.max_files} файлов, до ${lim.max_file_mb} МБ каждый:
        PDF, Word, PowerPoint, фото конспекта, .txt</span>
        <input type="file" id="file-in" multiple accept=".pdf,.docx,.pptx,.txt,.md,.html,.htm,.csv,.rtf,image/*,.heic,.heif" hidden></label>
      <label class="btn secondary block" style="margin-top:10px">Сфотографировать конспект<input type="file" id="cam-in" accept="image/*" capture="environment" hidden></label>
      <ul class="files" id="file-list"></ul>
      <label class="field"><span>Или вставь текст</span><textarea name="text" placeholder="Конспект, выдержки, вопросы к экзамену…"></textarea></label>
      <label class="field"><span>Ссылка на страницу</span><textarea name="urls" rows="2" style="min-height:60px" placeholder="https://… (можно несколько, по одной в строке)"></textarea></label>`}
      <label class="field"><span>Откуда и докуда читать <small class="muted">(необязательно)</small></span>
        <input type="text" name="range" value="${esc(qs.get("range") || "")}" placeholder="например: со слайда 5 до 12 · страницы 3–7 · только 2-й файл"></label>
    </section>
    <section class="panel"><h2><span class="step-n">2</span>Что сделать</h2>
      <div class="funcs" id="funcs" role="radiogroup">${FUNC_ORDER.map((f) => `<button type="button" role="radio" class="func ${f === func ? "on" : ""}" data-f="${f}" aria-checked="${f === func}">
        <span class="ic">${funcIcon(f)}</span><span><b>${esc(funcs()[f].name)}</b><small>${esc(funcs()[f].about)}</small></span></button>`).join("")}</div>
      <p class="level-note" id="quiz-line" ${func === "questions" ? "" : "hidden"}>${quizLine()} · <a href="#/settings">изменить</a></p>
    </section>
    <section class="panel"><h2><span class="step-n">3</span><span id="note-title"></span></h2>
      <textarea name="note" id="note" placeholder="">${esc(qs.get("note") || "")}</textarea><small class="help" id="note-help"></small>
    </section>
    <div id="cf-err"></div>
    <button class="btn block" type="submit" id="go-btn">Создать</button>
  </form>`;
  const form = $("#cf");
  const showFunc = () => {
    const isAns = func === "answer";
    $("#note-title").innerHTML = isAns ? "Твой вопрос" : `Уточни, что нужно <small class="muted" style="font-weight:400">&nbsp;(необязательно)</small>`;
    $("#note").placeholder = isAns ? "Например: чем электролиз расплава отличается от электролиза раствора?" :
      func === "tickets" ? "Например: 25 билетов, по 2 вопроса; только теория" : func === "summary" ? "Например: короче, только определения и даты" :
        "Например: больше вопросов про даты";
    $("#note-help").textContent = isAns ? "Ответ строится только по загруженным файлам. Если в них ответа нет — так и напишу." : "";
    $("#quiz-line").hidden = func !== "questions";
    $("#go-btn").textContent = { tickets: "Составить билеты", summary: "Сделать конспект", answer: "Найти ответ", questions: "Создать вопросы" }[func];
  };
  showFunc();
  $("#funcs").onclick = (e) => {
    const b = e.target.closest(".func"); if (!b) return;
    func = b.dataset.f; store("func", func);
    $$(".func").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-checked", String(x === b)); });
    showFunc();
  };
  if (!from) {
    const renderFiles = () => {
      $("#file-list").innerHTML = files.map((fl, i) => `<li><span>${esc(fl.name)}</span><small class="muted">${fl.size >= 1048576 ? num(fl.size / 1048576) + " МБ" : Math.max(1, Math.round(fl.size / 1024)) + " КБ"}</small><button type="button" class="x-btn" data-i="${i}" aria-label="Убрать">×</button></li>`).join("");
    };
    const addFiles = async (list) => {
      for (const raw of list) {
        if (files.length >= lim.max_files) { toast(`Не больше ${lim.max_files} файлов`); break; }
        if (!OK_RE.test(raw.name) && !raw.type.startsWith("image/")) {
          toast(/\.(doc|ppt|xls)$/i.test(raw.name) ? `«${raw.name}»: старый формат — сохрани как .docx / .pptx` : `«${raw.name}»: такой тип не читаю. Сохрани как PDF`, 5000); continue;
        }
        const fl = await shrinkImage(raw);
        if (fl.size > lim.max_file_mb * 1048576) { toast(`«${fl.name}» весит ${num(fl.size / 1048576)} МБ — больше ${lim.max_file_mb} МБ. Раздели файл на части.`, 6000); continue; }
        files.push(fl);
      }
      renderFiles();
    };
    $("#file-in").onchange = async (e) => { await addFiles(Array.from(e.target.files)); e.target.value = ""; };
    $("#cam-in").onchange = async (e) => { await addFiles(Array.from(e.target.files)); e.target.value = ""; };
    $("#file-list").onclick = (e) => { const b = e.target.closest("button[data-i]"); if (b) { files.splice(+b.dataset.i, 1); renderFiles(); } };
    const drop = $("#drop");
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); addFiles(Array.from(e.dataTransfer.files || [])); };
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    $("#cf-err").innerHTML = "";
    const note = form.note.value.trim();
    if (func === "answer" && !note) { errorBox("#cf-err", "Напиши вопрос в третьем окошке.", null, "Нет вопроса"); return; }
    const body = { func, note, source_range: form.range.value, text: "", urls: [], uploads: [], from_result: from ? from.id : "" };
    if (!from) {
      body.text = form.text.value;
      body.urls = form.urls.value.split(/\s+/).map((u) => u.trim()).filter(Boolean);
      if (!files.length && !body.text.trim() && !body.urls.length) { errorBox("#cf-err", "Добавь материал: файл, фото, текст или ссылку.", null, "Нет материала"); return; }
    }
    runCreation(body, files.slice());
  };
}

const TIPS = {
  tickets: [[0, "ИИ читает материал…"], [5, "делит его на темы…"], [12, "составляет билеты по всем темам…"], [30, "проверяю, что ни одна тема не забыта…"], [55, "большой материал — ещё немного…"]],
  summary: [[0, "ИИ читает материал…"], [5, "выделяет главное…"], [14, "собирает разделы и термины…"], [35, "ещё немного — конспект почти готов…"]],
  answer: [[0, "ищу в материале нужные места…"], [6, "сверяю ответ с текстом…"], [20, "ИИ думает чуть дольше обычного…"]],
  questions: [[0, "ИИ читает материал…"], [6, "придумывает вопросы…"], [16, "пишет объяснения и подсказки…"], [30, "программа проверяет варианты ответов…"], [50, "ещё немного…"]],
};
function workShell(title, func) {
  view.innerHTML = `<h1>${esc(title)}</h1><div class="panel"><div class="work-head">${loaderHTML(false, "cr-spin")}<div><b id="cr-now">готовлю…</b>
      <div class="cr-tip"><span id="cr-tip"></span> <span id="cr-sec" class="muted small"></span></div></div></div>
    <ul class="status-lines" id="cr-lines"></ul><div id="cr-warn"></div></div><div id="cr-err"></div>
    <p class="muted small">${esc(funcs()[func] ? funcs()[func].name : "")}. Не закрывай приложение, пока идёт работа: обычно это 15–90 секунд.</p>`;
  const lines = $("#cr-lines");
  const step = (text) => {
    $$("li.now", lines).forEach((li) => { li.classList.remove("now"); li.classList.add("done"); });
    const li = document.createElement("li"); li.className = "now"; li.textContent = text; lines.appendChild(li);
    $("#cr-now").textContent = text;
  };
  const fail = (err, retry) => { const sp = $("#cr-spin"); if (sp) sp.hidden = true; errorBox("#cr-err", err, retry); };
  return { step, fail };
}

/** Создание: загрузка файлов кусками → чтение → ИИ (с живыми статусами). */
async function runCreation(body, files) {
  setTitle("Делаю");
  const { step, fail } = workShell(funcs()[body.func].name, body.func);
  try {
    if (files.length) {
      const part = S.boot.limits.part_bytes;
      for (let i = 0; i < files.length; i++) {
        step(files.length > 1 ? `загружаю файлы… (${i + 1} из ${files.length})` : "загружаю файл…");
        let uploadId = "";
        const fl = files[i];
        for (let n = 0, off = 0; off < fl.size || (fl.size === 0 && n === 0); n++, off += part) {
          const fd = new FormData();
          fd.append("upload_id", uploadId); fd.append("n", String(n)); fd.append("filename", fl.name);
          fd.append("part", fl.slice(off, off + part), fl.name);
          uploadId = (await api("/api/uploads", { method: "POST", form: fd })).upload_id;
          if (fl.size === 0) break;
        }
        body.uploads.push(uploadId);
      }
    }
    step(files.some((f) => IMG_RE.test(f.name)) ? "читаю файлы и распознаю фото…" : "читаю материал…");
    const made = await api("/api/results", { method: "POST", json: body });
    const warn = [...(made.warnings || [])];
    if (made.range_note) warn.push(`Взял только часть: ${made.range_note}.`);
    if (made.range_rest) warn.push(`Указание «${made.range_rest}» передал ИИ.`);
    $("#cr-warn").innerHTML = warn.map((w) => `<div class="warn-box">${esc(w)}</div>`).join("");
    history.replaceState(null, "", `#/work/${made.result_id}`);
    await runWithStatus(made.result_id, body.func, step, fail);
  } catch (err) {
    body.uploads = [];
    fail(err, () => runCreation(body, files));
  }
}

/** Шаг «ИИ работает»: долгий запрос + опрос статуса раз в 1,5 с (меняется только строка статуса). */
async function runWithStatus(id, func, step, fail) {
  step("ИИ работает…");
  const stopTips = waitTicker($("#cr-tip"), $("#cr-sec"), TIPS[func] || TIPS.summary);
  onLeave(stopTips);
  const fail0 = fail;
  fail = (...a) => { stopTips(); $("#cr-tip").textContent = ""; $("#cr-sec").textContent = ""; fail0(...a); };
  let done = false, last = "";
  const poll = async () => {
    while (!done) {
      await sleep(1500);
      if (done) break;
      try {
        const st = await api(`/api/results/${id}/status`);
        if (st.status_text && st.status_text !== last && st.status === "running") { last = st.status_text; step(st.status_text); }
        if (st.status === "error" && !done) { done = true; fail(new Error(st.error || "Ошибка"), () => go(`/work/${id}`)); }
      } catch (e) { /* связь мигнула — попробуем на следующем круге */ }
    }
  };
  poll();
  try {
    const res = await api(`/api/results/${id}/run`, { method: "POST" });
    done = true; stopTips();
    if (res.warnings && res.warnings.length) toast(res.warnings.join(" "), 6000);
    step("готово!");
    go(func === "questions" ? `/play/${id}` : `/r/${id}`, true);
  } catch (err) {
    if (done) return;
    if (err.kind === "network") {  // связь оборвалась, а работа продолжается — ждём по статусу
      for (let i = 0; i < 200 && !done; i++) {
        await sleep(2000);
        try {
          const st = await api(`/api/results/${id}/status`);
          if (st.status === "ready") { done = true; go(`/r/${id}`, true); return; }
          if (st.status === "error") break;
        } catch (e) { /* ещё нет связи */ }
      }
    }
    done = true;
    fail(err, () => go(`/work/${id}`));
  }
}

async function pageWork(id) {
  setTitle("Делаю");
  loading();
  const r = await api(`/api/results/${id}`);
  if (r.status === "ready") { go(`/r/${id}`, true); return; }
  const { step, fail } = workShell(funcs()[r.func].name, r.func);
  if (r.status === "running") {
    step("ИИ работает…");
    const stop = waitTicker($("#cr-tip"), $("#cr-sec"), TIPS[r.func] || TIPS.summary);
    onLeave(stop);
    for (;;) {
      await sleep(2000);
      if (!location.hash.includes(id)) return;
      const s2 = await api(`/api/results/${id}/status`);
      if (s2.status === "ready") { stop(); go(`/r/${id}`, true); return; }
      if (s2.status === "error") { stop(); fail(new Error(s2.error), () => pageWork(id)); return; }
      if (s2.status_text) $("#cr-now").textContent = s2.status_text;
    }
  }
  await runWithStatus(id, r.func, step, fail);
}

// ---------- Результат ----------
function headHTML(r) {
  const srcs = (r.sources || []).map((s) => esc(s.name)).join(", ");
  return `<p class="muted small">${esc(funcs()[r.func].name)} · ${fmtDate(r.created_at)}${r.cost_rub ? ` · ${rub(r.cost_rub)}` : ""}</p>
    <h1>${r.func === "answer" ? "Ответ по файлам" : esc(r.title)}</h1>
    ${srcs ? `<p class="muted small">Материал: ${srcs}${r.range_note ? ` (${esc(r.range_note)})` : ""}</p>` : ""}
    ${r.note && r.func !== "answer" ? `<p class="small">Уточнение: ${esc(r.note)}</p>` : ""}
    ${(r.warnings || []).map((w) => `<div class="warn-box">${esc(w)}</div>`).join("")}`;
}
function sameMaterialHTML(r) {
  return `<h2>По этому же материалу</h2><div class="chip-row">${FUNC_ORDER.map((f) =>
    `<a class="btn secondary small" href="#/create?from=${r.id}&func=${f}">${esc(funcs()[f].short)}</a>`).join("")}</div>
    <div class="actions"><a class="btn secondary" href="#/history">К истории</a><button class="btn danger" id="del">Удалить</button></div>`;
}
function bindDelete(r) {
  $("#del").onclick = async () => {
    if (!(await confirmModal("Удалить результат вместе с извлечённым текстом? Вернуть будет нельзя.", "Удалить", true))) return;
    await api(`/api/results/${r.id}`, { method: "DELETE" });
    toast("Удалено"); go("/history", true);
  };
}
async function askFromTicket(r, question) {
  setTitle("Делаю");
  const { step, fail } = workShell(funcs().answer.name, "answer");
  try {
    step("беру тот же материал…");
    const made = await api("/api/results", { method: "POST", json: { func: "answer", note: question, from_result: r.id, uploads: [], urls: [] } });
    history.replaceState(null, "", `#/work/${made.result_id}`);
    await runWithStatus(made.result_id, "answer", step, fail);
  } catch (err) { fail(err, () => askFromTicket(r, question)); }
}

async function pageResult(id) {
  setTitle("Результат");
  loading();
  const r = await api(`/api/results/${id}`);
  if (r.status !== "ready") { go(`/work/${id}`, true); return; }
  setTitle(funcs()[r.func].short);
  const c = r.content || {};
  const multi = (c.topics || []).length > 1;
  const topicsLine = (c.topics || []).length ? `<div class="topics-line">${multi ? c.topics.map((t) => `<span class="topic">${esc(t)}</span>`).join("") : `<span class="muted small">Тема: ${esc(c.topics[0])}</span>`}</div>` : "";
  let body = "";
  if (r.func === "tickets") {
    body = `${topicsLine}<p class="muted small">Билетов: ${c.tickets.length}. Нажми «ответ по файлам» под вопросом — найду ответ в твоём материале.</p>
      <div class="tickets">${c.tickets.map((t) => `<article class="ticket"><h3>Билет № ${t.n}</h3><ol>${t.questions.map((q) =>
        `<li>${esc(q.text)}<br>${topicTag(q.topic, multi)} <button class="ask" data-q="${esc(q.text)}">ответ по файлам</button></li>`).join("")}</ol></article>`).join("")}</div>`;
  } else if (r.func === "summary") {
    let lastTopic = null;
    body = topicsLine + c.sections.map((s) => {
      const head = multi && s.topic !== lastTopic ? `<div class="topic-head">${esc(s.topic)}</div>` : "";
      lastTopic = s.topic;
      return `${head}<section class="sum-sec"><h3>${esc(s.title)}</h3><ul>${s.points.map((p) => `<li>${md(p).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ul>
        ${s.terms && s.terms.length ? `<dl>${s.terms.map((t) => `<dt>${esc(t.term)}</dt><dd>${esc(t.definition)}</dd>`).join("")}</dl>` : ""}</section>`;
    }).join("");
  } else if (r.func === "answer") {
    body = `<div class="panel"><p class="q-box">${esc(c.question)}</p><div class="answer ${c.found ? "" : "none"}">${md(c.answer)}</div>
      ${c.quotes && c.quotes.length ? `<h3>Из материала</h3>${c.quotes.map((q) => `<blockquote class="quote">${esc(q)}</blockquote>`).join("")}` : ""}
      ${c.topic || c.source ? `<p class="muted small">${c.topic ? `Тема: ${esc(c.topic)}` : ""}${c.topic && c.source ? " · " : ""}${c.source ? `Файл: ${esc(c.source)}` : ""}</p>` : ""}</div>
      <a class="btn block" href="#/create?from=${r.id}&func=answer">Задать ещё вопрос по этим файлам</a>`;
  } else if (r.func === "questions") {
    const la = r.last_attempt;
    body = `${topicsLine}<div class="panel"><p><b>${c.questions.length} вопросов</b> · ${esc((levels()[(c.settings || {}).difficulty] || {}).name || "")}</p>
      <p class="muted small">Сейчас в Настройках: таймер ${S.boot.quiz.timer ? "вкл" : "выкл"}, подсказки ${S.boot.quiz.hints ? "вкл" : "выкл"}.</p>
      ${la ? `<p>Прошлый раз: <b>${la.ok} из ${la.total}</b> (${num(la.pct)} %)</p>` : ""}
      <a class="btn block" href="#/play/${r.id}">${la ? "Пройти ещё раз" : "Начать"}</a></div>
      ${la ? `<h2>Разбор прошлого раза</h2>${reviewHTML(c.questions, la.answers, multi)}` : ""}`;
  }
  view.innerHTML = headHTML(r) + body + sameMaterialHTML(r);
  bindDelete(r);
  $$(".ask").forEach((b) => (b.onclick = () => askFromTicket(r, b.dataset.q)));
  bindReview();
}

// ---------- Прохождение вопросов (карточки по одной, как в Duolingo) ----------
async function pagePlay(id) {
  setTitle("Вопросы");
  loading();
  const r = await api(`/api/results/${id}`);
  if (r.status !== "ready") { go(`/work/${id}`, true); return; }
  const qs = (r.content || {}).questions || [];
  if (!qs.length) { errorBox(view, "Вопросов нет.", null); return; }
  const multi = (r.content.topics || []).length > 1;
  const st = S.boot.quiz;
  const timerSec = st.timer ? ((levels()[(r.content.settings || {}).difficulty] || {}).timer_sec || 40) : 0;
  const answers = {};
  let i = 0, ok = 0, streak = 0, timer = null;
  const t0 = Date.now();
  onLeave(() => clearInterval(timer));
  view.innerHTML = `<div class="play-top"><div class="progress"><i id="pg"></i></div><span class="counter" id="cnt"></span><span class="streak" id="stk"></span></div>
    ${timerSec ? `<div class="timer" id="tm"><i></i></div><div class="timer-text" id="tmt"></div>` : ""}
    <div class="card-stage" id="stage"></div>`;
  const show = () => {
    const q = qs[i];
    $("#pg").style.width = `${(i / qs.length) * 100}%`;
    $("#cnt").textContent = `${i + 1} из ${qs.length}`;
    $("#stk").textContent = streak >= 2 ? `серия ${streak}` : "";
    const sq = q.type === "multi";
    $("#stage").innerHTML = `<div class="card enter" id="card"><div class="q-meta"><span class="tag">${TYPE_NAME[q.type]}</span>${topicTag(q.topic, multi)}</div>
      <div class="q-text">${esc(q.text)}</div>
      <div class="opts">${q.options.map((o, k) => `<button type="button" class="opt ${sq ? "square" : ""}" data-k="${k}"><span class="mark"></span><span>${esc(o)}</span></button>`).join("")}</div>
      ${st.hints && q.hint ? `<button type="button" class="btn secondary small hint-btn" id="hint">Подсказка</button><div class="hint-box" id="hint-box" hidden>${esc(q.hint)}</div>` : ""}
      <div id="verdict"></div>
      <div class="actions">${sq ? `<button class="btn" id="check" disabled>Проверить</button>` : ""}<button class="btn" id="next" hidden>${i + 1 < qs.length ? "Дальше" : "Итог"}</button></div></div>`;
    const card = $("#card");
    const chosen = new Set();
    let locked = false;
    const finish = (timeout = false) => {
      if (locked) return;
      locked = true; clearInterval(timer);
      const pick = [...chosen].sort((a, b) => a - b);
      const good = !timeout && JSON.stringify(pick) === JSON.stringify([...q.correct].sort((a, b) => a - b));
      answers[q.id] = timeout ? [] : pick;
      if (good) { ok++; streak++; } else streak = 0;
      card.classList.add("locked", good ? "is-ok" : "is-bad");
      $$(".opt", card).forEach((b) => {
        const k = +b.dataset.k;
        if (q.correct.includes(k)) b.style.borderColor = "var(--ok)";
        else if (chosen.has(k)) b.style.borderColor = "var(--bad)";
      });
      $("#verdict").innerHTML = `<div class="verdict"><span class="ic">${good ? "✓" : "✕"}</span>${good ? "Верно" : timeout ? "Время вышло" : "Неверно"}</div>
        ${good ? "" : `<p class="small">Верный ответ: ${q.correct.map((k) => `«${esc(q.options[k])}»`).join(", ")}. Объяснение — в конце.</p>`}`;
      if ($("#check")) $("#check").hidden = true;
      $("#next").hidden = false; $("#next").focus();
    };
    $(".opts", card).onclick = (e) => {
      const b = e.target.closest(".opt"); if (!b || locked) return;
      const k = +b.dataset.k;
      if (sq) { chosen.has(k) ? chosen.delete(k) : chosen.add(k); b.classList.toggle("on", chosen.has(k)); $("#check").disabled = !chosen.size; return; }
      chosen.clear(); chosen.add(k); b.classList.add("on"); finish();
    };
    if ($("#check")) $("#check").onclick = () => finish();
    if ($("#hint")) $("#hint").onclick = () => { $("#hint-box").hidden = false; $("#hint").hidden = true; };
    $("#next").onclick = () => {
      card.classList.remove("enter"); card.classList.add("leave");
      setTimeout(() => { i++; if (i < qs.length) show(); else end(); }, 200);
    };
    if (timerSec) {
      let left = timerSec;
      const tm = $("#tm"), bar = $("i", tm);
      const tickT = () => {
        bar.style.width = `${(left / timerSec) * 100}%`; tm.classList.toggle("low", left <= 5);
        $("#tmt").textContent = `${left} с`;
        if (left <= 0) finish(true);
        left--;
      };
      clearInterval(timer); tickT(); timer = setInterval(tickT, 1000);
    }
  };
  const end = async () => {
    clearInterval(timer);
    $("#pg").style.width = "100%";
    view.innerHTML = `<div class="center">${loaderHTML(false)}<div class="muted">считаю итог…</div></div>`;
    let res;
    try { res = await api(`/api/results/${id}/attempt`, { method: "POST", json: { answers, duration_sec: Math.round((Date.now() - t0) / 1000) } }); }
    catch (e) { res = { ok, total: qs.length, pct: Math.round(ok * 1000 / qs.length) / 10, answers: Object.fromEntries(qs.map((q) => [q.id, { chosen: answers[q.id], ok: JSON.stringify((answers[q.id] || []).slice().sort()) === JSON.stringify(q.correct.slice().sort()) }])) }; toast("Итог не сохранился: нет связи. Показываю его здесь."); }
    setTitle("Итог");
    view.innerHTML = `<h1>Итог</h1><div class="big-pct">${num(res.pct)} %</div><p><b>${res.ok} из ${res.total}</b> верно</p>
      <h2>Все вопросы</h2><p class="muted small">Нажми на карточку — откроется объяснение.</p>${reviewHTML(qs, res.answers, multi)}
      <div class="stack" style="margin-top:16px"><a class="btn" href="#/play/${id}" id="again">Пройти ещё раз</a><a class="btn secondary" href="#/create?from=${id}&func=questions">Новые вопросы по этому материалу</a>
      <a class="btn secondary" href="#/r/${id}">К результату</a></div>`;
    bindReview();
    $("#again").onclick = (e) => { e.preventDefault(); pagePlay(id); };
  };
  show();
}
function reviewHTML(qs, ans, multi) {
  return `<div class="list sum-cards">${qs.map((q) => {
    const a = (ans || {})[q.id] || {};
    const chosen = a.chosen || [];
    return `<div class="item ${a.ok ? "ok" : "bad"}" role="button" tabindex="0"><span class="chev">›</span><div class="t">${a.ok ? "✓" : "✕"} ${esc(q.text)}</div>
      <div class="s">${topicTag(q.topic, multi)}<span>${TYPE_NAME[q.type]}</span></div>
      <div class="why"><p><b>Верно:</b> ${q.correct.map((k) => esc(q.options[k])).join("; ")}</p>
        ${a.ok ? "" : `<p><b>Твой ответ:</b> ${chosen.length ? chosen.map((k) => esc(q.options[k] ?? "?")).join("; ") : "нет ответа"}</p>`}
        <p>${esc(q.explanation)}</p>${q.quote ? `<blockquote class="quote">${esc(q.quote)}</blockquote>` : ""}</div></div>`;
  }).join("")}</div>`;
}
function bindReview() {
  $$(".sum-cards .item").forEach((it) => {
    it.onclick = () => it.classList.toggle("open");
    it.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); it.classList.toggle("open"); } };
  });
}

// ---------- История ----------
async function pageHistory() {
  setTitle("История");
  loading();
  const { items } = await api("/api/results");
  if (!items.length) {
    view.innerHTML = `<h1>История</h1><p class="muted">Пока пусто. Здесь будут все билеты, конспекты, ответы и вопросы.</p><a class="btn block" href="#/create">Создать</a>`;
    return;
  }
  view.innerHTML = `<h1>История</h1><ul class="list" id="hist">${items.map((r) => `<li class="row" style="flex-wrap:nowrap" data-id="${r.id}">
    <a class="item" style="flex:1;min-width:0" href="${r.status === "ready" ? `#/r/${r.id}` : `#/work/${r.id}`}"><div class="t">${esc(r.title)}</div>
      <div class="s"><span class="tag">${esc((funcs()[r.func] || {}).short || r.func)}</span><span>${fmtDate(r.created_at)}</span>
        ${r.status === "error" ? `<span class="status-chip error">ошибка</span>` : r.status !== "ready" ? `<span class="status-chip running">не готово</span>` : ""}
        ${r.last_pct !== null && r.last_pct !== undefined ? `<span>${num(r.last_pct)} %</span>` : ""}${r.cost_rub ? `<span>${rub(r.cost_rub)}</span>` : ""}
        ${(r.topics || []).length > 1 ? `<span>тем: ${r.topics.length}</span>` : ""}</div></a>
    <button class="x-btn" data-del="${r.id}" aria-label="Удалить «${esc(r.title)}»">×</button></li>`).join("")}</ul>`;
  $("#hist").onclick = async (e) => {
    const b = e.target.closest("[data-del]"); if (!b) return;
    if (!(await confirmModal("Удалить результат вместе с извлечённым текстом? Вернуть будет нельзя.", "Удалить", true))) return;
    try { await api(`/api/results/${b.dataset.del}`, { method: "DELETE" }); b.closest("li").remove(); toast("Удалено"); }
    catch (err) { toast(err.message, 5000); }
    if (!$("#hist li")) pageHistory();
  };
}

// ---------- Настройки ----------
async function pageSettings() {
  setTitle("Настройки");
  loading();
  const [aiSt, usage] = await Promise.all([api("/api/settings/ai"), api("/api/usage")]);
  const q = S.boot.quiz, lim = S.boot.limits;
  const pal = S.boot.palette || "classic";
  view.innerHTML = `<h1>Настройки</h1>
  <h2>Вопросы</h2><div class="panel" id="qs">
    <div class="field"><b>Число вопросов</b><div class="seg" id="count-seg" style="margin-top:6px">${lim.choices.map((c) => `<button type="button" data-v="${c}" class="${c === q.count ? "on" : ""}">${c}</button>`).join("")}
      <button type="button" data-v="custom" class="${lim.choices.includes(q.count) ? "" : "on"}">своё</button></div>
      <input type="number" id="count-custom" min="1" max="${lim.max_questions}" value="${q.count}" ${lim.choices.includes(q.count) ? "hidden" : ""} style="margin-top:8px"></div>
    <div class="field"><b>Сложность</b><div class="seg" id="diff-seg" style="margin-top:6px">${Object.entries(levels()).map(([k, v]) => `<button type="button" data-v="${k}" class="${k === q.difficulty ? "on" : ""}">${esc(v.name)}</button>`).join("")}</div></div>
    <label class="toggle"><span>Таймер <small class="muted" id="timer-note"></small></span><input type="checkbox" id="timer" ${q.timer ? "checked" : ""}><span class="sw"></span></label>
    <label class="toggle"><span>Подсказки</span><input type="checkbox" id="hints" ${q.hints ? "checked" : ""}><span class="sw"></span></label>
  </div>
  <h2>Палитра</h2>${PALETTE_GROUPS.map(([g, list]) => `<p class="muted small" style="margin:10px 0 6px">${g}</p><div class="palettes">${list.map((p) => `<button type="button" class="pal ${p === pal ? "on" : ""}" data-p="${p}" data-palette="${p}" style="background:var(--bg);color:var(--text)">${esc(PALETTE_NAMES[p])}
    <span class="sw"><i style="background:var(--text)"></i><i style="background:var(--accent)"></i><i style="background:var(--accent-2)"></i><i style="background:var(--ok)"></i></span></button>`).join("")}</div>`).join("")}
  <h2>ИИ</h2><div class="panel" id="ai-box"></div>
  <h2>Пароль</h2><form class="panel" id="pw" autocomplete="off">
    <label class="field"><span>Старый пароль</span><input type="password" name="old" autocomplete="current-password" required></label>
    <label class="field"><span>Новый пароль</span><input type="password" name="new" autocomplete="new-password" minlength="6" required><small>Хотя бы 6 символов. Другие устройства выйдут, это — останется.</small></label>
    <button class="btn secondary block" type="submit">Сменить пароль</button><div id="pw-msg"></div></form>
  <button class="btn secondary block" id="logout">Выйти на этом устройстве</button>
  <section class="costs" id="costs"></section>`;
  // вопросы — сохраняются сразу
  const saveQuiz = async (patch) => {
    try { S.boot.quiz = await api("/api/settings/quiz", { method: "PUT", json: patch }); toast("Сохранено", 1500); showTimer(); }
    catch (e) { toast(e.message, 5000); }
  };
  const showTimer = () => { const l = levels()[S.boot.quiz.difficulty]; $("#timer-note").textContent = l ? `(${secText(l.timer_sec)} на вопрос)` : ""; };
  showTimer();
  const seg = (el, fn) => { el.onclick = (e) => { const b = e.target.closest("button"); if (!b) return; $$("button", el).forEach((x) => x.classList.toggle("on", x === b)); fn(b.dataset.v); }; };
  seg($("#count-seg"), (v) => { $("#count-custom").hidden = v !== "custom"; if (v !== "custom") saveQuiz({ count: +v }); else $("#count-custom").focus(); });
  $("#count-custom").onchange = (e) => { const v = parseInt(e.target.value, 10); if (v >= 1 && v <= lim.max_questions) saveQuiz({ count: v }); else toast(`От 1 до ${lim.max_questions}`); };
  seg($("#diff-seg"), (v) => saveQuiz({ difficulty: v }));
  $("#timer").onchange = (e) => saveQuiz({ timer: e.target.checked });
  $("#hints").onchange = (e) => saveQuiz({ hints: e.target.checked });
  // палитра
  $$(".pal").forEach((b) => (b.onclick = async () => {
    applyPalette(b.dataset.p);
    $$(".pal").forEach((x) => x.classList.toggle("on", x === b));
    try { await api("/api/settings/palette", { method: "PUT", json: { palette: b.dataset.p } }); S.boot.palette = b.dataset.p; }
    catch (e) { toast(e.message, 5000); }
  }));
  renderAI(aiSt);
  $("#pw").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    try { await api("/api/password", { method: "POST", json: { old: f.old.value, new: f.new.value } }); f.reset(); $("#pw-msg").innerHTML = `<p class="ok-text">Пароль сменён ✓</p>`; }
    catch (err) { errorBox("#pw-msg", err, null, "Не сменился"); }
  };
  $("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); showLogin(); };
  renderCosts(usage);
}

function renderAI(st, provider = st.provider) {
  const box = $("#ai-box");
  const p = st.providers[provider];
  const secretText = (s) => (s.broken ? "ключ сохранён, но не расшифровывается (сменили KEY_SECRET) — впиши заново"
    : s.set ? (s.source === "db" ? "задан ✓ (сохранён в приложении)" : "задан ✓ (из переменной окружения)") : "не задан");
  const modelField = p.models.length
    ? `<select name="model">${p.models.map(([v, t]) => `<option value="${esc(v)}" ${v === p.fields.model ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`
    : `<input type="text" name="model" value="${esc(p.fields.model || "")}" placeholder="название модели">`;
  box.innerHTML = `<label class="field"><span>Провайдер</span><select id="prov">${Object.entries(st.providers).map(([k, v]) => `<option value="${k}" ${k === provider ? "selected" : ""}>${esc(v.title)}${k === st.provider ? " — сейчас работает" : ""}</option>`).join("")}</select>
      <small>Можно переключаться, чтобы сравнить. Работает тот, что сохранён последним.</small></label>
    <form id="ai-form" autocomplete="off">
      ${"model" in p.fields && provider !== "mock" ? `<label class="field"><span>Модель</span>${modelField}</label>` : ""}
      ${Object.keys(p.fields).filter((f) => f !== "model").map((f) => `<label class="field"><span>${esc(p.labels[f] || f)}</span><input type="text" name="f_${f}" value="${esc(p.fields[f] || "")}" autocapitalize="off" spellcheck="false"></label>`).join("")}
      ${Object.entries(p.secrets).map(([f, s]) => `<div class="field"><b>${esc(p.labels[f] || f)}</b><div class="secret-row" style="margin-top:6px"><input type="password" name="s_${f}" placeholder="${esc(secretText(s))}" autocomplete="off" autocapitalize="off" spellcheck="false">
        ${s.source === "db" ? `<button type="button" class="btn secondary small" data-delkey="${f}">Удалить</button>` : ""}</div>
        <small class="help">${esc(secretText(s))}. Ключ хранится зашифрованным и обратно на экран не показывается.</small></div>`).join("")}
      ${provider !== "mock" ? `<details><summary class="muted small">Цена (₽ за 1000 токенов) — для подсчёта расходов</summary><div class="row" style="margin-top:8px">
        <input type="number" step="0.01" min="0" name="pin" placeholder="ввод" value="${p.price ? p.price.in : ""}" style="flex:1"><input type="number" step="0.01" min="0" name="pout" placeholder="вывод" value="${p.price ? p.price.out : ""}" style="flex:1"></div>
        <small class="help">Пусто — цена по умолчанию из кода.</small></details>` : ""}
      ${!st.key_secret_set ? `<p class="warn-box">На сервере не задан KEY_SECRET: ключи можно задать только переменными окружения.</p>` : ""}
      <div class="actions"><button class="btn" type="submit">Сохранить и включить</button><button class="btn secondary" type="button" id="check">Проверить связь</button></div>
      <div id="ai-msg"></div></form>`;
  $("#prov").onchange = (e) => renderAI(st, e.target.value);
  const form = $("#ai-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = { provider, fields: {}, secrets: {} };
    if (form.model) body.fields.model = form.model.value;
    Object.keys(p.fields).filter((f) => f !== "model").forEach((f) => { body.fields[f] = form["f_" + f].value; });
    Object.keys(p.secrets).forEach((f) => { const v = form["s_" + f].value.trim(); if (v) body.secrets[f] = v; });
    if (form.pin) body.price = { in: form.pin.value, out: form.pout.value };
    try { const ns = await api("/api/settings/ai", { method: "PUT", json: body }); S.boot.provider = provider; renderAI(ns, provider); toast("Сохранено. Сейчас работает: " + ns.providers[provider].title); }
    catch (err) { errorBox("#ai-msg", err, null, "Не сохранилось"); }
  };
  $("#check").onclick = async () => {
    $("#ai-msg").innerHTML = `<div class="row">${loaderHTML()}<span class="muted">проверяю (сохранённые настройки)…</span></div>`;
    try {
      const r = await api(`/api/settings/ai/${provider}/check`, { method: "POST" });
      $("#ai-msg").innerHTML = r.ok ? `<p class="ok-text">${esc(r.message)}</p>` : `<div class="error-box"><b>${esc(r.message)}</b><div class="small mono">${esc(r.details || "")}</div></div>`;
    } catch (err) { errorBox("#ai-msg", err, null); }
  };
  $$("[data-delkey]", form).forEach((b) => (b.onclick = async () => {
    if (!(await confirmModal("Удалить сохранённый ключ?", "Удалить", true))) return;
    renderAI(await api(`/api/settings/ai/${provider}/${b.dataset.delkey}`, { method: "DELETE" }), provider);
  }));
}

function renderCosts(u) {
  const m = u.month, pct = u.budget ? Math.min(100, (m.rub / u.budget) * 100) : 0;
  $("#costs").innerHTML = `<h2>Расходы</h2><div class="panel">
    <p><b>Этот месяц: ≈ ${rub(m.rub)}</b> из ${rub(u.budget)}</p>
    <div class="budget-bar ${m.rub > u.budget ? "over" : ""}"><i style="width:${pct}%"></i></div>
    ${m.rub > u.budget ? `<p class="bad-text small">Бюджет месяца превышен.</p>` : ""}
    <div class="kv" style="margin-top:12px"><span>Токены: ввод</span><span class="v">${big(m.tokens_in)}</span><span>Токены: вывод</span><span class="v">${big(m.tokens_out)}</span>
      <span>Запросов</span><span class="v">${big(m.calls)}</span></div>
    ${u.by_purpose.length ? `<h3>На что</h3><div class="kv">${u.by_purpose.map((x) => `<span>${esc(PURPOSE[x.purpose] || x.purpose)}</span><span class="v">${rub(x.rub)}</span>`).join("")}</div>` : ""}
    ${u.by_model.length ? `<h3>Модели</h3><div class="kv">${u.by_model.map((x) => `<span>${esc(x.model || x.provider)}</span><span class="v">${big(x.tokens)} ток. · ${rub(x.rub)}</span>`).join("")}</div>` : ""}
    <p class="muted small" style="margin-top:12px">За всё время: ${big(u.total.tokens_in + u.total.tokens_out)} токенов, ≈ ${rub(u.total.rub)}. Суммы примерные: по ценам из Настроек → ИИ → Цена.</p>
    ${u.last.length ? `<details><summary class="muted small">Последние запросы</summary><div class="kv" style="margin-top:8px">${u.last.map((x) => `<span class="small">${fmtDate(x.at)} · ${esc(PURPOSE[x.purpose] || x.purpose)}${x.title ? ` · ${esc(x.title)}` : ""}</span><span class="v small">${rub(x.rub)}</span>`).join("")}</div></details>` : ""}
    <form class="row" id="budget-form" style="margin-top:12px"><input type="number" min="0" step="50" name="b" value="${u.budget}" style="flex:1" aria-label="Бюджет в месяц, ₽"><button class="btn secondary small" type="submit">Бюджет, ₽</button></form></div>`;
  $("#budget-form").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/settings/budget", { method: "PUT", json: { budget: +e.target.b.value } }); renderCosts(await api("/api/usage")); toast("Бюджет сохранён"); }
    catch (err) { toast(err.message, 5000); }
  };
}

// ---------- Старт ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => { /* без офлайна — не страшно */ });
document.addEventListener("click", (e) => { const a = e.target.closest("a[href^='http']"); if (a) a.target = "_blank"; });
boot();
