/* BiletY — пиксельные портреты философов. Рисуются кодом (SVG 16×16), ничего не копируется из интернета.

   Как описать философа в static/philosophers.json (все поля портрета необязательны — без них портрет
   соберётся сам из имени):
   "portrait": {
     "skin":  "#e8b88f",            цвет кожи
     "hair":  "short",              bald | receding | short | long | curly | wig | topknot | none
     "hair_color": "#4a3526",
     "beard": "long",               none | mustache | short | long | goatee
     "beard_color": "#cfcfcf",      (по умолчанию как волосы)
     "clothes": "#efe6d2",          одежда
     "extra": "laurel"              none | glasses | laurel | hat | collar
   },
   "second": {                      второй портрет (в мини-биографии): сам философ или его труд
     "kind": "book",                book | scroll | bust | cup | lamp
     "title": "«Государство»",      подпись под картинкой
     "color": "#7a2e2e"             цвет обложки / свитка
   }
*/
"use strict";

const Pixel = (() => {
  const N = 16;
  const shade = (hex, k) => {
    const h = hex.replace("#", "");
    const c = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    const m = c.map((v) => Math.max(0, Math.min(255, Math.round(k < 1 ? v * k : v + (255 - v) * (k - 1)))));
    return "#" + m.map((v) => v.toString(16).padStart(2, "0")).join("");
  };
  const grey = (hex) => {
    const h = hex.replace("#", "");
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    const y = Math.round(0.3 * r + 0.59 * g + 0.11 * b * 1.0);
    const v = Math.round(120 + y * 0.45);
    return "#" + [v, v, v + 4].map((x) => Math.min(255, x).toString(16).padStart(2, "0")).join("");
  };
  function hash(s) { let h = 2166136261; for (const ch of String(s)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }

  const SKINS = ["#f1c7a1", "#e8b88f", "#d9a272", "#c68b59", "#a8714a", "#f3d2b3"];
  const HAIRS = ["#2b1d14", "#4a3526", "#6b4a2f", "#9a9a9a", "#d9d9d9", "#1d1d1d", "#8a5a2b"];
  const CLOTHES = ["#efe6d2", "#2f3e5c", "#5a3a2a", "#3c5a3c", "#6a2e3e", "#1f1f1f", "#7a6a4f"];
  const HAIR_STYLES = ["bald", "receding", "short", "long", "curly", "wig", "topknot"];
  const BEARDS = ["none", "mustache", "short", "long", "goatee"];

  /** Портрет, если владелец его не описал: собирается из имени, всегда одинаково для одного имени. */
  function auto(name) {
    const h = hash(name);
    const pick = (arr, sh) => arr[(h >>> sh) % arr.length];
    return { skin: pick(SKINS, 1), hair: pick(HAIR_STYLES, 4), hair_color: pick(HAIRS, 8), beard: pick(BEARDS, 12),
      clothes: pick(CLOTHES, 16), extra: (h >>> 20) % 5 === 0 ? "glasses" : "none" };
  }

  function grid() { return Array.from({ length: N }, () => Array(N).fill(null)); }
  function put(g, x, y, c) { if (x >= 0 && x < N && y >= 0 && y < N && c) g[y][x] = c; }
  function row(g, y, x0, x1, c) { for (let x = x0; x <= x1; x++) put(g, x, y, c); }

  /** Портрет 16×16: массив строк с цветами (null — фон). */
  function face(spec) {
    const s = { skin: "#e8b88f", hair: "short", hair_color: "#4a3526", beard: "none", clothes: "#efe6d2", extra: "none", ...spec };
    const g = grid();
    const skin = s.skin, skinD = shade(skin, 0.82), hairC = s.hair_color, beardC = s.beard_color || hairC;
    // одежда и плечи
    row(g, 13, 3, 12, s.clothes); row(g, 14, 1, 14, s.clothes); row(g, 15, 1, 14, s.clothes);
    put(g, 3, 13, shade(s.clothes, 0.85)); put(g, 12, 13, shade(s.clothes, 0.85));
    // шея
    row(g, 11, 6, 9, skinD); row(g, 12, 6, 9, skinD);
    // лицо
    row(g, 3, 6, 9, skin); row(g, 4, 5, 10, skin);
    for (let y = 5; y <= 9; y++) row(g, y, 4, 11, skin);
    row(g, 10, 5, 10, skin); row(g, 11, 6, 9, skin);
    put(g, 3, 7, skin); put(g, 3, 8, skinD); put(g, 12, 7, skin); put(g, 12, 8, skinD);
    // волосы
    const H = (y, a, b) => row(g, y, a, b, hairC);
    switch (s.hair) {
      case "bald": put(g, 4, 6, hairC); put(g, 11, 6, hairC); put(g, 4, 7, hairC); put(g, 11, 7, hairC); break;
      case "receding": H(3, 5, 5); H(3, 10, 10); H(4, 4, 5); H(4, 10, 11); put(g, 4, 5, hairC); put(g, 11, 5, hairC); put(g, 4, 6, hairC); put(g, 11, 6, hairC); break;
      case "long": H(2, 5, 10); H(3, 4, 11); H(4, 4, 5); H(4, 10, 11);
        for (let y = 4; y <= 12; y++) { put(g, 3, y, hairC); put(g, 12, y, hairC); }
        put(g, 4, 5, hairC); put(g, 11, 5, hairC); put(g, 2, 11, hairC); put(g, 13, 11, hairC); put(g, 2, 12, hairC); put(g, 13, 12, hairC); break;
      case "curly": H(1, 5, 10); H(2, 4, 11); H(3, 3, 12); row(g, 4, 3, 4, hairC); row(g, 4, 11, 12, hairC);
        put(g, 4, 1, hairC); put(g, 11, 1, hairC); put(g, 7, 0, hairC);
        for (let y = 5; y <= 8; y++) { put(g, 3, y, hairC); put(g, 12, y, hairC); }
        put(g, 6, 4, hairC); put(g, 8, 4, hairC); break;
      case "wig": { const w = s.hair_color; row(g, 2, 5, 10, w); row(g, 3, 4, 11, w); put(g, 4, 4, w); put(g, 11, 4, w);
        for (let y = 5; y <= 9; y++) { row(g, y, 2, 3, w); row(g, y, 12, 13, w); }
        put(g, 2, 6, shade(w, 0.85)); put(g, 13, 6, shade(w, 0.85)); put(g, 2, 8, shade(w, 0.85)); put(g, 13, 8, shade(w, 0.85)); break; }
      case "topknot": H(0, 7, 8); H(1, 7, 8); H(2, 5, 10); H(3, 4, 11); put(g, 4, 4, hairC); put(g, 11, 4, hairC); put(g, 4, 5, hairC); put(g, 11, 5, hairC); break;
      case "none": break;
      default: H(2, 5, 10); H(3, 4, 11); row(g, 4, 4, 6, hairC); row(g, 4, 10, 11, hairC); put(g, 4, 5, hairC); put(g, 11, 5, hairC); put(g, 4, 6, hairC); put(g, 11, 6, hairC);
    }
    // брови, глаза, нос, рот
    const brow = s.hair === "bald" || s.hair === "wig" || s.hair === "none" ? shade(beardC, 0.8) : hairC;
    put(g, 5, 6, brow); put(g, 6, 6, brow); put(g, 9, 6, brow); put(g, 10, 6, brow);
    put(g, 6, 7, "#1e1e1e"); put(g, 9, 7, "#1e1e1e");
    put(g, 7, 8, skinD); put(g, 7, 9, skinD);
    row(g, 10, 7, 8, "#9a4a42");
    // борода
    const B = (y, a, b) => row(g, y, a, b, beardC);
    switch (s.beard) {
      case "mustache": B(9, 6, 9); break;
      case "short": B(9, 6, 9); put(g, 4, 9, beardC); put(g, 11, 9, beardC); B(10, 4, 6); B(10, 9, 11); B(11, 5, 10); B(12, 6, 9); break;
      case "long": B(9, 6, 9); put(g, 4, 9, beardC); put(g, 11, 9, beardC); B(10, 4, 6); B(10, 9, 11); B(11, 4, 11); B(12, 5, 10); B(13, 6, 9); B(14, 7, 8);
        put(g, 6, 12, shade(beardC, 0.85)); put(g, 9, 12, shade(beardC, 0.85)); break;
      case "goatee": put(g, 7, 9, beardC); put(g, 8, 9, beardC); B(11, 7, 8); B(12, 7, 8); break;
      default: break;
    }
    // украшения
    switch (s.extra) {
      case "glasses": { const f = "#202020"; put(g, 5, 7, f); put(g, 7, 7, f); put(g, 8, 7, f); put(g, 10, 7, f); row(g, 6, 5, 7, f); row(g, 6, 8, 10, f); break; }
      case "laurel": { const l = "#3f7d3a", l2 = "#5fa055"; put(g, 3, 4, l); put(g, 4, 3, l2); put(g, 5, 2, l); put(g, 12, 4, l); put(g, 11, 3, l2); put(g, 10, 2, l); put(g, 3, 5, l2); put(g, 12, 5, l2); break; }
      case "hat": { const t = "#1f1f1f"; for (let y = 0; y <= 2; y++) row(g, y, 5, 10, t); row(g, 3, 3, 12, t); row(g, 2, 5, 10, "#6a2020"); break; }
      case "collar": row(g, 13, 6, 9, "#ffffff"); put(g, 5, 13, "#ffffff"); put(g, 10, 13, "#ffffff"); break;
      default: break;
    }
    return g;
  }

  function book(color, title) {
    const g = grid(), c = color || "#7a2e2e", d = shade(c, 0.7), p = "#f3ecd8", gold = "#d6b24a";
    for (let y = 1; y <= 14; y++) row(g, y, 3, 12, c);
    for (let y = 1; y <= 14; y++) { put(g, 3, y, d); put(g, 4, y, d); }
    for (let y = 2; y <= 14; y++) put(g, 13, y, p);
    row(g, 15, 4, 13, p); put(g, 13, 1, shade(c, 0.85));
    row(g, 4, 6, 11, gold); row(g, 6, 7, 10, gold); row(g, 7, 7, 10, gold); row(g, 9, 6, 11, gold);
    const h = hash(title || "");
    for (let i = 0; i < 4; i++) put(g, 6 + ((h >>> (i * 3)) % 6), 12, shade(gold, 0.9));
    return g;
  }

  function scroll(color, title) {
    const g = grid(), p = color || "#e9dcb5", d = shade(p, 0.78), ink = "#5a4632", rod = "#7a5530";
    for (let y = 3; y <= 12; y++) row(g, y, 2, 13, p);
    row(g, 2, 1, 14, rod); row(g, 13, 1, 14, rod); put(g, 0, 2, shade(rod, 0.7)); put(g, 15, 2, shade(rod, 0.7)); put(g, 0, 13, shade(rod, 0.7)); put(g, 15, 13, shade(rod, 0.7));
    row(g, 3, 2, 13, d); row(g, 12, 2, 13, d);
    const h = hash(title || "scroll");
    for (let y = 5; y <= 10; y++) { const len = 5 + ((h >>> y) % 6); row(g, y, 4, Math.min(11, 3 + len), y % 2 ? ink : null); }
    return g;
  }

  function cup() {
    const g = grid(), c = "#b8b8b8", d = "#8a8a8a", liq = "#4e6b3a";
    row(g, 4, 3, 12, d); row(g, 5, 3, 12, liq); row(g, 6, 3, 12, c); row(g, 7, 4, 11, c); row(g, 8, 5, 10, c); row(g, 9, 6, 9, d);
    for (let y = 10; y <= 12; y++) row(g, y, 7, 8, c);
    row(g, 13, 5, 10, d); row(g, 14, 4, 11, c);
    put(g, 2, 5, d); put(g, 13, 5, d); put(g, 2, 6, d); put(g, 13, 6, d);
    put(g, 6, 2, "#9ab38a"); put(g, 8, 1, "#9ab38a"); put(g, 9, 3, "#9ab38a");
    return g;
  }

  function lamp() {
    const g = grid(), m = "#c9a04a", d = "#8a6a2a", fl = "#ffcc33", fl2 = "#ff8a1a";
    put(g, 7, 1, fl); put(g, 7, 2, fl); put(g, 8, 2, fl2); put(g, 7, 3, fl2); put(g, 8, 3, fl);
    row(g, 4, 6, 9, d);
    for (let y = 5; y <= 10; y++) { row(g, y, 4, 11, "#fff2c4"); put(g, 4, y, m); put(g, 11, y, m); put(g, 7, y, m); }
    row(g, 5, 4, 11, m); row(g, 10, 4, 11, m); row(g, 11, 5, 10, d); row(g, 12, 6, 9, m);
    put(g, 7, 13, d); put(g, 8, 13, d); row(g, 14, 5, 10, d);
    return g;
  }

  function bust(spec) {
    const g = face({ ...spec, clothes: "#bdbdbd" });
    for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) if (g[y][x]) g[y][x] = grey(g[y][x]);
    row(g, 15, 4, 11, "#8c8c8c");
    return g;
  }

  /** SVG из сетки. bg — цвет фона (по умолчанию из палитры приложения). */
  function svg(g, label, bg = "var(--surface-2)") {
    let rects = "";
    for (let y = 0; y < N; y++) {
      let x = 0;
      while (x < N) {
        const c = g[y][x];
        if (!c) { x++; continue; }
        let w = 1; while (x + w < N && g[y][x + w] === c) w++;
        rects += `<rect x="${x}" y="${y}" width="${w}" height="1" fill="${c}"/>`;
        x += w;
      }
    }
    const t = String(label || "").replace(/[&<>"]/g, "");
    return `<svg class="px" viewBox="0 0 16 16" shape-rendering="crispEdges" role="img" aria-label="${t}"><rect width="16" height="16" fill="${bg}"/>${rects}</svg>`;
  }

  function portrait(ph) {
    const spec = ph.portrait && Object.keys(ph.portrait).length ? ph.portrait : auto(ph.name_ru || ph.name_native || "");
    return svg(face(spec), ph.name_ru);
  }

  function second(ph) {
    const s = ph.second || { kind: "bust" };
    const spec = ph.portrait && Object.keys(ph.portrait).length ? ph.portrait : auto(ph.name_ru || "");
    const g = s.kind === "book" ? book(s.color, s.title) : s.kind === "scroll" ? scroll(s.color, s.title)
      : s.kind === "cup" ? cup() : s.kind === "lamp" ? lamp() : bust(spec);
    return svg(g, s.title || ph.name_ru);
  }

  /** Маленький пиксельный «бюст» для логотипа и загрузки. */
  function logo() { return svg(bust({ hair: "curly", beard: "long", hair_color: "#777777" }), "BiletY", "transparent"); }

  return { portrait, second, logo, face, svg, auto };
})();
