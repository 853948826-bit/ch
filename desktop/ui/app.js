// 赛博诊所桌面小精灵 · 渲染与交互（与 macOS 原生版 sprite/Sprite.swift 行为一一对应）
// 坐标约定：#stage 内全部使用皮肤 viewBox 坐标；macOS Core Animation 是“y 向上、逆时针为正”，
// 这里统一换算为 CSS 的“y 向下、顺时针为正”。
"use strict";
const T = window.__TAURI__;
const invoke = T.core.invoke, listen = T.event.listen;
const win = T.window.getCurrentWindow();
const { LogicalSize, PhysicalPosition } = T.dpi;
window.addEventListener("error", (e) => invoke("js_log", { msg: `sprite: ${e.message} @${e.lineno}` }));
window.addEventListener("unhandledrejection", (e) => invoke("js_log", { msg: `sprite: ${e.reason}` }));
const _log = console.log; console.log = (...a) => { _log(...a); invoke("js_log", { msg: a.join(" ") }).catch(() => {}); };
const $ = (id) => document.getElementById(id);
const api = (method, path, body) => invoke("api", { method, path, body: body ?? null });
const later = (s, f) => setTimeout(f, s * 1000);
const pick = (a) => a[Math.floor(Math.random() * a.length)];
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

// ---------- 窗口布局 ----------
const CELL = 4, SIDE = 248, TOP = 232, BOTTOM = 8, DBL = 0.4;
let scale = store.get("scale", 0.72);
let WW = 0, WH = 0, stageRect = [SIDE, TOP, 0, 0], geo = null, shown = false;

// ---------- 皮肤 ----------
const LAYER_NAMES = ["hair_back", "body", "arm_l", "arm_r", "face", "blush", "eyes", "mouth", "braid", "bangs", "ahoge", "cap", "pose", "holo"];
const L = {}, IMG = {};
for (const n of LAYER_NAMES) { L[n] = document.querySelector(`[data-l="${n}"]`); IMG[n] = L[n].querySelector("img"); }
const head = $("head"), root = $("root"), stage = $("stage"), scaler = $("scaler"), shade = $("shade"), led = $("led");
let S = { ver: 0, W: 200, H: 320, urls: {}, anchors: {}, lines: {}, motions: {}, sounds: {}, whole: new Set(), tts: false,
          showLED: false, eyeTrack: false, jiggle: true, zones: {}, pzones: {}, variants: {}, ipose: {}, gaze: {}, shared: new Set(), accent: "#2fd4e6" };
let eyes = "eyes_open", mouth = "mouth_smile", activePose = null, poseGen = 0;
const line = (k, fb = "") => (S.lines[k] && S.lines[k].length ? pick(S.lines[k]) : fb);

function isBlank(svg) {
  return !svg || svg.replace(/^\s*(<\?xml[^>]*\?>)?\s*<svg[^>]*>/, "").replace(/<\/svg>\s*$/, "").trim() === "";
}

function loadSkin(sk) {
  for (const u of Object.values(S.urls)) URL.revokeObjectURL(u);
  const vb = Array.isArray(sk.viewBox) && sk.viewBox[0] > 0 && sk.viewBox[1] > 0 ? sk.viewBox : [200, 250];
  const urls = {};
  for (const [k, v] of Object.entries(sk.layers || {})) {
    if (!isBlank(v)) urls[k] = URL.createObjectURL(new Blob([v], { type: "image/svg+xml" }));
  }
  return {
    ver: sk.ver || 0, name: sk.name || "", W: vb[0], H: vb[1], urls, anchors: sk.anchors || {}, lines: sk.lines || {}, motions: sk.motions || {},
    sounds: sk.sounds || {}, whole: new Set(sk.whole_states || []), tts: !!sk.tts, showLED: sk.show_led ?? true,
    eyeTrack: sk.eye_track ?? true, zones: sk.interaction_zones || {}, pzones: sk.pose_interaction_zones || {},
    variants: sk.interaction_variants || {}, ipose: sk.interaction_poses || {},
    gaze: sk.gaze_poses || {}, jiggle: sk.jiggle ?? true, shared: new Set(sk.shared_pose_names || []), accent: (sk.palette || {}).accent || "#2fd4e6",
  };
}

function setSrc(n, url) { if (url) IMG[n].src = url; else IMG[n].removeAttribute("src"); }
function pivot(el, p) { el.style.transformOrigin = `${p[0]}px ${p[1]}px`; }
const A = (k, d) => S.anchors[k] || d;

function applySkin(next) {
  softStop(); softMatch.clear();
  clearPose();
  S = next;
  stage.style.width = S.W + "px"; stage.style.height = S.H + "px";
  const foot = A("body", [S.W / 2, S.H - 10])[1];
  Object.assign(shade.style, { left: S.W / 2 - 30 + "px", width: "60px", top: Math.min(S.H - 12, foot - 5) + "px" });
  for (const n of ["hair_back", "body", "arm_l", "arm_r", "holo", "face", "blush", "bangs", "cap", "ahoge", "braid"]) setSrc(n, S.urls[n]);
  pivot(L.ahoge, A("ahoge", [100, 40])); pivot(L.braid, A("braid", [150, 100]));
  pivot(head, A("head", [100, 140])); pivot(L.hair_back, A("head", [100, 140]));
  pivot(L.arm_l, A("arm_l", [80, 150])); pivot(L.arm_r, A("arm_r", [120, 150])); pivot(L.body, A("body", [100, 240]));
  const lp = A("led", [134, 32]);
  Object.assign(led.style, { left: lp[0] + "px", top: lp[1] + "px" });
  led.classList.toggle("hidden", !S.showLED);
  document.documentElement.style.setProperty("--accent", S.accent);
  L.eyes.style.transform = "";
  setFace(eyes, mouth);
  startLoops();
}

function setFace(e, m) {
  if (e) { eyes = e; setSrc("eyes", S.urls[e] || S.urls.eyes_open); L.body.style.visibility = S.whole.has(e) ? "hidden" : ""; }
  if (m) { mouth = m; setSrc("mouth", S.urls[m] || S.urls.mouth_smile); }
  maskSoon();
}

// ---------- 动画原语（Web Animations，叠加到元素自身变换上，渲染线程执行） ----------
const running = new WeakMap();
function play(el, key, frames, dur, o = {}) {
  if (!el) return null;
  let m = running.get(el);
  if (!m) running.set(el, (m = {}));
  if (m[key]) m[key].cancel();
  const a = el.animate(frames, { duration: dur * 1000, easing: o.easing || "ease-in-out", iterations: o.iterations || 1,
                                 direction: o.direction || "normal", composite: "add" });
  m[key] = a;
  a.onfinish = () => { if (m[key] === a) delete m[key]; };
  return a;
}
const deg = (v) => (-v * 180) / Math.PI; // CA 弧度（逆时针为正）→ CSS 角度（顺时针为正）
const frames = (vals, f, times) => vals.map((v, i) => (times ? { transform: f(v), offset: times[i] } : { transform: f(v) }));
function rotate(el, vals, dur, key, times) { play(el, key, frames(vals, (v) => `rotate(${deg(v)}deg)`, times), dur); }

let loops = [];
function startLoops() {
  loops.forEach((a) => a.cancel());
  const inf = { iterations: Infinity, direction: "alternate" };
  loops = [
    play(root, "float", frames([0, 4], (v) => `translateY(${-v}px)`), 1.7, inf),
    play(shade, "float", frames([1, 0.88], (v) => `scaleX(${v})`), 1.7, inf),
    play(L.body, "breath", frames([1, 1.018], (v) => `scaleY(${v})`), 2.1, inf),
    play(L.ahoge, "sway", frames([-0.14, 0.14], (v) => `rotate(${deg(v)}deg)`), 1.3, inf),
    play(L.braid, "sway", frames([-0.07, 0.07], (v) => `rotate(${deg(v)}deg)`), 1.9, inf),
  ].filter(Boolean);
}
function setFloat(speed) { loops.slice(0, 3).forEach((a) => a.updatePlaybackRate(speed)); }
function boing() { rotate(L.ahoge, [0, 0.5, -0.35, 0.2, -0.1, 0], 0.8, "boing"); }
function tilt(v, d) { rotate(head, v, d, "tilt"); rotate(L.hair_back, v, d, "tilt"); }
function wave() { rotate(L.arm_r, [0, 2.5, 2.1, 2.5, 2.1, 2.5, 0], 1.8, "wave"); tilt([0, -0.08, -0.08, 0], 1.8); }
function stretch() { rotate(L.arm_l, [0, -2.6, -2.6, 0], 2.0, "s"); rotate(L.arm_r, [0, 2.6, 2.6, 0], 2.0, "s"); hop(4, 2.0); }
function hop(h = 16, dur = 0.45) {
  play(stage, "hop", frames([0, h, 0, h * 0.25, 0], (v) => `translateY(${-v}px)`, [0, 0.4, 0.75, 0.88, 1]), dur);
  boing();
  softKick("both", h * 1.6, rnd(h * 0.4));                      // 起跳：身体上冲，软组织滞后下沉
  later(dur * 0.75, () => softKick("both", h * 4.5, rnd(h)));   // 落地：主冲击
}
function squash() { play(L.body, "squash", frames([1, 0.86, 1.06, 1], (v) => `scaleY(${v})`), 0.35); softKick("both", 55, rnd(15)); }
function jiggle() {
  const x = [1, 1.08, 0.94, 1.04, 0.98, 1], y = [1, 0.92, 1.06, 0.97, 1.02, 1];
  const f = x.map((v, i) => ({ transform: `scale(${v}, ${y[i]})` }));
  play(L.body, "jiggle", f, 0.7, { easing: "ease-out" }); play(head, "jiggle", f, 0.7, { easing: "ease-out" });
  softKick("both", 45, rnd(30));
}
function spin() { play(root, "spin", [{ transform: "rotateY(0deg)" }, { transform: "rotateY(360deg)" }], 0.9); }
function rub() { rotate(L.arm_l, [0, -0.22, 0.08, -0.22, 0.08, 0], 1.5, "rub"); rotate(L.arm_r, [0, 0.22, -0.08, 0.22, -0.08, 0], 1.5, "rub"); }
function cheer() { rotate(L.arm_l, [0, -2.7, -2.4, -2.7, 0], 1.4, "cheer"); rotate(L.arm_r, [0, 2.7, 2.4, 2.7, 0], 1.4, "cheer"); }
function shake(dur) {
  const n = Math.max(4, Math.floor(dur * 14)), idx = [...Array(n + 1).keys()];
  const r = idx.map((i) => (i === 0 || i === n ? 0 : (i % 2 === 0 ? 0.05 : -0.05) * (1 - (i / n) * 0.5)));
  play(stage, "shake", frames(r, (v) => `rotate(${deg(v)}deg)`), dur, { easing: "linear" });
  play(L.body, "shakeY", frames(idx.map((i) => (i % 2 ? 2.5 : 0)), (v) => `translateY(${-v}px)`), dur, { easing: "linear" });
}
function collapse() {
  const t = [0, 0.15, 0.3, 0.75, 0.9, 1];
  play(L.body, "collapse", frames([1, 0.78, 0.8, 0.8, 1.05, 1], (v) => `scaleY(${v})`, t), 2.2);
  play(stage, "collapse", frames([0, 0.12, 0.1, 0.1, 0, 0], (v) => `rotate(${deg(v)}deg)`, t), 2.2);
}
const POSE_HIDE = ["body", "arm_l", "arm_r", "hair_back"];
function showPose(name, secs) {
  const url = S.urls["pose_" + name];
  if (!url) return;
  if (secs == null && activePose === name) return;
  const g = ++poseGen;
  activePose = name;
  setSrc("pose", url); L.pose.classList.remove("hidden");
  for (const n of POSE_HIDE) L[n].style.opacity = 0;
  head.style.opacity = 0;
  maskSoon();
  softKick("both", 22, rnd(8));
  if (secs != null) later(secs, () => { if (poseGen === g) clearPose(); });
}
function clearPose() {
  poseGen++; activePose = null;
  L.pose.classList.add("hidden"); setSrc("pose", null);
  for (const n of POSE_HIDE) L[n].style.opacity = "";
  head.style.opacity = "";
  maskSoon();
}
function gaze(dir) {
  const key = dir ? S.gaze[dir] : null;
  setSrc("body", (key && S.urls["pose_" + key]) || S.urls.body);
  maskSoon();
}
let holoOn = false;
function holo(on) { holoOn = on; L.holo.style.opacity = on ? 1 : 0; maskSoon(); }
function blush(v) { L.blush.style.opacity = v; }
function ledColor(c) { led.style.background = c; }
function lookAt(dx, dy) { L.eyes.style.transform = `translate(${dx}px, ${-dy}px)`; }
function dangle(a) { root.style.transform = a ? `rotate(${deg(a)}deg)` : ""; }
function particle(s, dx = 0) {
  if (quiet()) return;
  const p = document.createElement("div");
  p.className = "particle"; p.textContent = s;
  Object.assign(p.style, { left: S.W * 0.6 + dx + "px", top: S.H * 0.2 - 30 + "px" });
  $("particles").appendChild(p);
  p.animate([{ transform: "translateY(0)", opacity: 1 }, { transform: "translateY(-34px)", opacity: 0 }], { duration: 1600, fill: "forwards" });
  later(1.7, () => p.remove());
}

// ---------- 软弹：胸部/臀部的局部弹簧形变 ----------
// 当前整图放到细分网格上，部位中心附近按衰减权重位移，弹簧阻尼驱动“过冲→回弹→静止”。
// 只在被触发时运行（静止即停止渲染并换回原图），不占常驻 CPU。
// 部位位置：常态图用皮肤的 click_chest / click_skirt 区域；其他姿势首次出现时在常态图上取模板做匹配定位，
// 匹配不上就不晃（宁缺毋滥）；姿势自带区域（pose_interaction_zones，如坐姿）直接使用。
// 肉感（chest/hip）：上下弹、带体积挤压；hip 只在露出臀部的姿势里启用（皮肤 jiggle.hip_poses 登记）。
// 布料（skirt）：只在拖动时轻摆，腰部固定、越往裙摆越明显，以横向摆动为主，不做挤压。
const SOFT_PARTS = { chest: { zone: "click_chest", k: 200, c: 5, hang: true, flesh: true },
  hip: { zone: "click_skirt", k: 240, c: 6.5, flesh: true }, skirt: { zone: "click_skirt", k: 70, c: 3.2, hem: true } };
function hipExposed() { const hp = S.jiggle && Array.isArray(S.jiggle.hip_poses) ? S.jiggle.hip_poses : []; return !!activePose && hp.includes(activePose); }
const SOFT_GAIN = 1.6; // 整体力度（皮肤 manifest 的 jiggle.strength 再乘在上面）
const soft = { gl: null, canvas: null, n: 0, pos: null, posBuf: null, tex: null, srcKey: "", host: null, img: null,
               raf: 0, timer: 0, last: 0, still: 0, state: { chest: { x: 0, y: 0, vx: 0, vy: 0 }, hip: { x: 0, y: 0, vx: 0, vy: 0 }, skirt: { x: 0, y: 0, vx: 0, vy: 0 } } };
const softMatch = new Map(); // 图片 url → {chest:{dx,dy}|null, hip:...}；"pending" 表示计算中
const GX = 30, GY = 45;

function softOn() {
  return S.jiggle !== false && !!S.urls.body && !!(S.zones.click_chest || Object.keys(S.pzones).length);
}
function softStrength() { return SOFT_GAIN * ((S.jiggle && typeof S.jiggle === "object" && +S.jiggle.strength) || 1); }
// 当前画面上真正显示的整图
function softSource() {
  if (activePose) return { img: IMG.pose, host: L.pose, key: "pose_" + activePose };
  if (S.whole.has(eyes)) return { img: IMG.eyes, host: L.eyes, key: eyes };
  return { img: IMG.body, host: L.body, key: "body" };
}
function zoneRect(z) { return z && z.length === 4 ? [z[0] * S.W, z[1] * S.H, z[2] * S.W, z[3] * S.H] : null; }
// 返回当前图里各部位的椭圆 {cx, cy, ax, ay}；还没算出来返回 null
function softRegions(src) {
  const out = {};
  const own = activePose && S.pzones[activePose];
  const url = src.img.getAttribute("src");
  let shift = null;
  if (!own && url !== S.urls.body) {
    shift = softMatch.get(url);
    if (shift === undefined) { softLocate(url); return null; }
    if (shift === "pending") return null;
  }
  for (const [name, part] of Object.entries(SOFT_PARTS)) {
    const r = zoneRect((own || S.zones)[part.zone]);
    if (!r) continue;
    const d = shift ? shift[name] : { dx: 0, dy: 0 };
    if (!d) continue;
    out[name] = { cx: (r[0] + r[2]) / 2 + d.dx, cy: (r[1] + r[3]) / 2 + d.dy, ax: ((r[2] - r[0]) / 2) * 1.15, ay: ((r[3] - r[1]) / 2) * 1.35 };
  }
  return out;
}
async function lumaOf(url, w, h) {
  const img = new Image(); img.src = url; await img.decode();
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h); ctx.drawImage(img, 0, 0, w, h);
  const d = ctx.getImageData(0, 0, w, h).data, g = new Float32Array(w * h);
  for (let i = 0; i < g.length; i++) g[i] = 0.3 * d[i * 4] + 0.59 * d[i * 4 + 1] + 0.11 * d[i * 4 + 2];
  return g;
}
// 在常态图上取部位模板，到目标姿势图里 ±24 个单位内找最相似的位置（归一化互相关）
async function softLocate(url) {
  softMatch.set(url, "pending");
  try {
    const R = 0.5, w = Math.round(S.W * R), h = Math.round(S.H * R);
    const [ref, tgt] = await Promise.all([lumaOf(S.urls.body, w, h), lumaOf(url, w, h)]);
    const res = {}, byZone = {};
    for (const [name, part] of Object.entries(SOFT_PARTS)) {
      if (part.zone in byZone) { res[name] = byZone[part.zone]; continue; }
      const r = zoneRect(S.zones[part.zone]);
      if (!r) { res[name] = null; continue; }
      const x0 = Math.round(r[0] * R), y0 = Math.round(r[1] * R), x1 = Math.round(r[2] * R), y1 = Math.round(r[3] * R);
      const tw = x1 - x0, th = y1 - y0, n = tw * th;
      let mt = 0; for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) mt += ref[y * w + x];
      mt /= n;
      let st = 0; for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) st += (ref[y * w + x] - mt) ** 2;
      let best = -2, bx = 0, by = 0;
      const S0 = 12;
      for (let oy = -S0; oy <= S0; oy++) for (let ox = -S0; ox <= S0; ox++) {
        if (x0 + ox < 0 || y0 + oy < 0 || x1 + ox > w || y1 + oy > h) continue;
        let mi = 0; for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) mi += tgt[(y + oy) * w + x + ox];
        mi /= n;
        let cov = 0, si = 0;
        for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
          const a = ref[y * w + x] - mt, b = tgt[(y + oy) * w + x + ox] - mi;
          cov += a * b; si += b * b;
        }
        const ncc = cov / Math.sqrt(st * si + 1e-6);
        if (ncc > best) { best = ncc; bx = ox; by = oy; }
      }
      res[name] = byZone[part.zone] = best >= 0.55 ? { dx: bx / R, dy: by / R, score: +best.toFixed(2) } : null;
    }
    softMatch.set(url, res);
    console.log(`[soft] 定位 ${Object.entries(res).map(([k, v]) => `${k}=${v ? `${v.dx},${v.dy}@${v.score}` : "无"}`).join(" ")}`);
  } catch (e) { softMatch.set(url, {}); }
}
function softInit() {
  if (soft.gl) return true;
  const c = document.createElement("canvas");
  c.className = "soft";
  const gl = c.getContext("webgl", { premultipliedAlpha: true, alpha: true, antialias: false });
  if (!gl) return false;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const p = gl.createProgram();
  gl.attachShader(p, sh(gl.VERTEX_SHADER, "attribute vec2 p;attribute vec2 t;varying vec2 v;void main(){v=t;gl_Position=vec4(p,0.,1.);}"));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, "precision mediump float;varying vec2 v;uniform sampler2D s;void main(){gl_FragColor=texture2D(s,v);}"));
  gl.linkProgram(p); gl.useProgram(p);
  const n = (GX + 1) * (GY + 1), uv = new Float32Array(n * 2), idx = new Uint16Array(GX * GY * 6);
  for (let j = 0, i = 0; j <= GY; j++) for (let k = 0; k <= GX; k++, i++) { uv[i * 2] = k / GX; uv[i * 2 + 1] = j / GY; }
  for (let j = 0, q = 0; j < GY; j++) for (let k = 0; k < GX; k++) {
    const a = j * (GX + 1) + k, b = a + 1, d = a + GX + 1, e = d + 1;
    idx.set([a, b, d, b, e, d], q); q += 6;
  }
  const tb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, tb); gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
  const tl = gl.getAttribLocation(p, "t"); gl.enableVertexAttribArray(tl); gl.vertexAttribPointer(tl, 2, gl.FLOAT, false, 0, 0);
  soft.posBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, soft.posBuf); gl.bufferData(gl.ARRAY_BUFFER, n * 8, gl.DYNAMIC_DRAW);
  const pl = gl.getAttribLocation(p, "p"); gl.enableVertexAttribArray(pl); gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer()); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
  soft.tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, soft.tex);
  for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
  gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  Object.assign(soft, { gl, canvas: c, n, pos: new Float32Array(n * 2) });
  return true;
}
// 把当前整图接管到画布上（同一图层里，继承呼吸/倾斜等动画）
function softAttach(src) {
  const key = src.img.getAttribute("src");
  if (soft.srcKey === key && soft.img === src.img) return true;
  if (!src.img.complete || !src.img.naturalWidth) return false;
  softDetach();
  const k = Math.min(3, scale * (window.devicePixelRatio || 1)) * 1.0, w = Math.round(S.W * k), h = Math.round(S.H * k);
  const gl = soft.gl, c = soft.canvas;
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  const off = document.createElement("canvas"); off.width = w; off.height = h;
  off.getContext("2d").drawImage(src.img, 0, 0, w, h);
  gl.bindTexture(gl.TEXTURE_2D, soft.tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, off);
  gl.viewport(0, 0, w, h);
  src.host.appendChild(c);
  src.img.style.visibility = "hidden";
  Object.assign(soft, { srcKey: key, img: src.img, host: src.host });
  return true;
}
function softDetach() {
  if (soft.img) soft.img.style.visibility = "";
  if (soft.canvas && soft.canvas.parentNode) soft.canvas.remove();
  soft.img = null; soft.host = null; soft.srcKey = "";
}
function softDraw(regs) {
  const { gl, pos } = soft, st = soft.state;
  for (let j = 0, i = 0; j <= GY; j++) for (let k = 0; k <= GX; k++, i++) {
    const px = (k / GX) * S.W, py = (j / GY) * S.H;
    let dx = 0, dy = 0;
    for (const [name, r] of Object.entries(regs)) {
      const s = st[name], u = (px - r.cx) / r.ax, v = (py - r.cy) / r.ay, d2 = u * u + v * v;
      if (d2 >= 1) continue;
      const P = SOFT_PARTS[name], low = Math.min(1, Math.max(0, (v + 1) / 2));
      const w = (1 - d2) * (1 - d2) * (P.hang ? 0.45 + 0.55 * low : P.hem ? low * low : 1);
      const sq = P.flesh ? Math.max(-0.25, Math.min(0.25, s.y / r.ay)) : 0; // 肉感：下坠时纵向拉长、横向收窄，保持“体积感”
      dx += w * (s.x - (px - r.cx) * 0.5 * sq);
      dy += w * (s.y + (py - r.cy) * 0.6 * sq);
    }
    pos[i * 2] = ((px + dx) / S.W) * 2 - 1;
    pos[i * 2 + 1] = 1 - ((py + dy) / S.H) * 2;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, soft.posBuf); gl.bufferSubData(gl.ARRAY_BUFFER, 0, pos);
  gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
  gl.drawElements(gl.TRIANGLES, GX * GY * 6, gl.UNSIGNED_SHORT, 0);
}
function softCancelFrame() {
  cancelAnimationFrame(soft.raf); soft.raf = 0;
  clearTimeout(soft.timer); soft.timer = 0;
}
function softSchedule() {
  const step = (t) => { softCancelFrame(); softFrame(t); };
  soft.raf = requestAnimationFrame(step);
  // macOS 悬浮窗口可能暂时收不到画面逐帧回调。仅在软弹期间补帧。
  soft.timer = setTimeout(() => step(performance.now()), 34);
}
function softStop() {
  softCancelFrame();
  for (const s of Object.values(soft.state)) Object.assign(s, { x: 0, y: 0, vx: 0, vy: 0 });
  softDetach();
}
function softFrame(t) {
  if (t - soft.last > 250) return softStop();
  const dt = Math.min(1 / 30, (t - soft.last) / 1000 || 1 / 60); soft.last = t;
  let energy = 0;
  for (const [name, s] of Object.entries(soft.state)) {
    const { k, c } = SOFT_PARTS[name];
    for (let i = 0; i < 2; i++) { // 两个子步，保证弹簧稳定
      const h = dt / 2;
      s.vx += (-k * s.x - c * s.vx) * h; s.vy += (-k * s.y - c * s.vy) * h;
      s.x += s.vx * h; s.y += s.vy * h;
    }
    energy = Math.max(energy, Math.abs(s.x), Math.abs(s.y), Math.abs(s.vx) / 20, Math.abs(s.vy) / 20);
  }
  if (energy < 0.04) { if (++soft.still > 6) return softStop(); } else soft.still = 0;
  const src = softSource(), regs = softRegions(src);
  if (regs && Object.keys(regs).length && softAttach(src)) softDraw(regs);
  else softDetach(); // 这张姿势图还没定位好或定位失败：显示原图
  softSchedule();
}
// 触发一次软弹：part 为 "chest" / "hip" / "both"，vx/vy 为初速度（viewBox 单位/秒，正 y 向下）
function softKick(part, vy, vx = 0) {
  if (!softOn() || !softInit()) return;
  const f = softStrength();
  for (const name of part === "both" ? ["chest", "hip"] : [part]) {
    const s = soft.state[name];
    if (!s || (name === "hip" && !hipExposed())) continue;
    const cloth = name === "skirt"; // 裙摆轻摆：横向为主、幅度小
    s.vy += vy * f * (name === "hip" ? 0.75 : cloth ? 0.15 : 1); s.vx += vx * f * (cloth ? 0.4 : 1);
    const cap = (cloth ? 45 : 140) * f; // 连续施力（如拖动）时限制速度，避免幅度失控
    s.vy = Math.max(-cap, Math.min(cap, s.vy)); s.vx = Math.max(-cap, Math.min(cap, s.vx));
  }
  if (!soft.raf) { soft.last = performance.now(); soft.still = 0; softSchedule(); }
}
const rnd = (a) => (Math.random() * 2 - 1) * a;

// ---------- 鼠标穿透遮罩：只有角色/气泡的不透明像素接收鼠标 ----------
const grids = new Map();
let lastMask = null;
let maskTimer = 0;
function gridFor(url, cols, rows) {
  const key = `${url}|${cols}x${rows}`;
  const g = grids.get(key);
  if (g) return g === "pending" ? null : g;
  grids.set(key, "pending");
  const img = new Image();
  img.src = url;
  img.decode().then(() => {
    const c = document.createElement("canvas");
    c.width = cols; c.height = rows;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, cols, rows);
    const d = ctx.getImageData(0, 0, cols, rows).data, bits = new Uint8Array(cols * rows);
    for (let i = 0; i < bits.length; i++) bits[i] = d[i * 4 + 3] > 24 ? 1 : 0;
    grids.set(key, bits);
    maskSoon();
  }).catch(() => grids.set(key, new Uint8Array(cols * rows)));
  return null;
}
function visibleUrls() {
  const out = [];
  if (activePose) out.push(IMG.pose.getAttribute("src"));
  else {
    for (const n of ["hair_back", "body", "arm_l", "arm_r"]) if (L[n].style.visibility !== "hidden") out.push(IMG[n].getAttribute("src"));
    for (const n of ["face", "eyes", "mouth", "braid", "bangs", "ahoge", "cap"]) out.push(IMG[n].getAttribute("src"));
  }
  if (holoOn) out.push(IMG.holo.getAttribute("src"));
  return out.filter(Boolean);
}
function maskSoon() { clearTimeout(maskTimer); maskTimer = setTimeout(updateMask, 16); }
function updateMask() {
  if (!WW) return;
  const cols = WW / CELL, rows = WH / CELL, bits = new Uint8Array(cols * rows);
  const [sx, sy, sw, sh] = stageRect, sc = sw / CELL, sr = sh / CELL, ox = sx / CELL, oy = sy / CELL;
  for (const url of visibleUrls()) {
    const g = gridFor(url, sc, sr);
    if (!g) continue;
    for (let r = 0; r < sr; r++) for (let c = 0; c < sc; c++) {
      if (!g[r * sc + c]) continue;
      for (let dr = -2; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) { // 向上多扩一格，覆盖漂浮/跳跃位移
        const R = oy + r + dr, C = ox + c + dc;
        if (R >= 0 && R < rows && C >= 0 && C < cols) bits[R * cols + C] = 1;
      }
    }
  }
  const b = $("bubble");
  if (!b.classList.contains("hidden")) {
    const x0 = Math.max(0, Math.floor(b.offsetLeft / CELL)), y0 = Math.max(0, Math.floor(b.offsetTop / CELL));
    const x1 = Math.min(cols, Math.ceil((b.offsetLeft + b.offsetWidth) / CELL)), y1 = Math.min(rows, Math.ceil((b.offsetTop + b.offsetHeight) / CELL));
    for (let r = y0; r < y1; r++) bits.fill(1, r * cols + x0, r * cols + x1);
  }
  lastMask = { cols, rows, bits };
  invoke("set_mask", { cols, rows, cell: CELL, bits: Array.from(bits) }).catch(() => {});
}

// ---------- 窗口定位：保存“角色脚底中心”的屏幕坐标，换尺寸/换皮肤时脚底不动 ----------
async function refreshGeo() {
  try {
    const [p, sf, m] = await Promise.all([win.outerPosition(), win.scaleFactor(), T.window.currentMonitor()]);
    geo = { x: p.x, y: p.y, sf, mon: m };
  } catch {}
}
async function defaultAnchor() {
  const m = await T.window.primaryMonitor();
  if (!m) return { x: 1200, y: 800 };
  const wa = m.workArea || { position: m.position, size: m.size };
  return { x: wa.position.x + wa.size.width - 150 * m.scaleFactor, y: wa.position.y + wa.size.height - 24 * m.scaleFactor };
}
async function validAnchor(a) {
  if (!a) return false;
  const ms = await T.window.availableMonitors();
  return ms.some((m) => a.x >= m.position.x && a.x < m.position.x + m.size.width && a.y > m.position.y && a.y <= m.position.y + m.size.height);
}
async function layout(why = "") {
  const sw = Math.ceil((S.W * scale) / CELL) * CELL, sh = Math.ceil((S.H * scale) / CELL) * CELL;
  WW = SIDE * 2 + sw; WH = TOP + sh + BOTTOM;
  stageRect = [SIDE, TOP, sw, sh];
  Object.assign(scaler.style, { left: SIDE + (sw - S.W * scale) / 2 + "px", top: TOP + (sh - S.H * scale) + "px", transform: `scale(${scale})` });
  const saved = store.get("anchor", null);
  let a = saved;
  if (!(await validAnchor(a))) a = await defaultAnchor();
  const sf = await win.scaleFactor();
  console.log(`[pos] layout(${why}) saved=${JSON.stringify(saved)} anchor=${Math.round(a.x)},${Math.round(a.y)} sf=${sf} win=${WW}x${WH}`);
  await win.setSize(new LogicalSize(WW, WH));
  await win.setPosition(new PhysicalPosition(Math.round(a.x - (SIDE + sw / 2) * sf), Math.round(a.y - (TOP + sh) * sf)));
  grids.clear();
  maskSoon();
  await refreshGeo();
  if (!shown) { shown = true; await win.show(); }
}
async function saveAnchor() {
  await refreshGeo();
  if (!geo) return;
  const [sx, sy, sw, sh] = stageRect;
  store.set("anchor", { x: geo.x + (sx + sw / 2) * geo.sf, y: geo.y + (sy + sh) * geo.sf });
  console.log(`[pos] saveAnchor win=${geo.x},${geo.y}`);
}

// ---------- 气泡 ----------
const bubble = $("bubble"), btext = $("bubble-text"), bform = $("bubble-form"), binput = $("bubble-input"), bacts = $("bubble-actions");
let bubbleTimer = 0, onSubmit = null;
let asking = false, pendingProtection = null;
const composing = () => !bform.classList.contains("hidden");
function placeBubble() {
  const [sx, sy, sw] = stageRect, bw = bubble.offsetWidth, bh = bubble.offsetHeight;
  let left = sx - bw + 30, top = Math.max(0, sy - bh + 18);
  if (geo && geo.mon) {
    const sf = geo.sf, monL = geo.mon.position.x, monT = geo.mon.position.y;
    if (geo.x + left * sf < monL) left = sx + sw - 30;            // 左边放不下就放右边
    const minTop = (monT - geo.y) / sf;
    if (top < minTop) top = Math.min(minTop, WH - bh);            // 贴屏幕顶时往下压，保证看得见
  }
  bubble.style.left = left + "px"; bubble.style.top = top + "px";
}
function showBubble(text, hold = 5, typing = false, actions = null) {
  if (composing()) {
    if (typing) binput.focus();
    return false;
  }
  clearTimeout(bubbleTimer);
  btext.textContent = text;
  bacts.replaceChildren(...(actions || []).map((a) => {
    const b = document.createElement("button");
    b.textContent = a.label; if (a.kind) b.className = a.kind;
    b.addEventListener("click", (e) => { e.stopPropagation(); hideBubble(); a.on(); });
    return b;
  }));
  bacts.classList.toggle("hidden", !(actions && actions.length));
  bform.classList.toggle("hidden", !typing);
  bubble.classList.remove("hidden");
  placeBubble();
  maskSoon();
  if (typing) {
    binput.value = "";
    win.setFocus().catch(() => {}).finally(() => binput.focus());
  } else if (hold > 0) bubbleTimer = setTimeout(hideBubble, hold * 1000);
  return true;
}
function hideBubble() {
  clearTimeout(bubbleTimer);
  bubble.classList.add("hidden"); bform.classList.add("hidden"); bacts.replaceChildren(); onSubmit = null;
  maskSoon();
  if (pendingProtection) setTimeout(showProtectionNotice, 0);
}
bform.addEventListener("submit", (e) => {
  e.preventDefault();
  const t = binput.value.trim();
  if (!t) return;
  const f = onSubmit; hideBubble();
  (f || askClinic)(t);
});
binput.addEventListener("keydown", (e) => { if (e.key === "Escape") { hideBubble(); settle(0.3); } });
bubble.addEventListener("pointerdown", (e) => {
  if (!composing() && !e.target.closest("button")) { e.stopPropagation(); hideBubble(); }
});

// ---------- 情绪 ----------
let mood = "normal", muted = store.get("muted", false), focus = false, sleeping = false, online = true;
// 专注模式（工作/游戏）：手动开启，或全屏/游戏时自动进入。期间不弹气泡、不出声、不做小动作、不抢置顶；
// 只有严重异常，或用户主动点它/叫它（之后 60 秒内）才出来服务
let focusManual = store.get("focusManual", false), focusAuto = store.get("focusAuto", true), focusWhy = "", engagedUntil = 0, peekUntil = 0, onTop = null;
const deferred = new Set(); // 专注期间暂缓的智能体修复申请，退出专注或用户唤醒时再弹
const engaged = () => Date.now() < engagedUntil, quiet = () => focus && !engaged();
function applyQuiet() {
  const peek = Date.now() < peekUntil || composing() || asking;
  document.body.classList.toggle("focus", quiet() && !peek);
  const top = !quiet() || peek;
  if (top !== onTop) { onTop = top; invoke("set_on_top", { on: top }).catch(() => {}); }
}
function engage(s = 60) { engagedUntil = Date.now() + s * 1000; applyQuiet(); flushDeferred(); }
const open = new Map();
const FACE = { normal: ["eyes_open", "mouth_smile"], happy: ["eyes_happy", "mouth_cat"], worried: ["eyes_worried", "mouth_worried"],
               alert: ["eyes_surprised", "mouth_o"], sleep: ["eyes_closed", "mouth_smile"], listening: ["eyes_open", "mouth_o"],
               thinking: ["eyes_open", "mouth_cat"], dragging: ["eyes_surprised", "mouth_open"] };
let gazeDir = null;
function setMood(m) {
  mood = m;
  if (m !== "normal" && gazeDir != null) { gazeDir = null; gaze(null); }
  setFace(...FACE[m]);
  setFloat(m === "sleep" ? 0.45 : 1);
  if (m === "thinking" && S.urls.pose_think) { holo(false); showPose("think", 8); } // 有手持全息板姿势就用皮肤自己的画
  else holo(m === "thinking");
}
function baseMood() {
  if (sleeping) return "sleep";
  const now = Date.now() / 1000;
  for (const e of open.values()) if (e.severity === "crit" || (e.severity === "warn" && now - (e.t_detect || 0) < 60)) return "worried";
  return "normal";
}
function settle(s) {
  later(s, () => { if (!dragging && mood !== "listening" && mood !== "thinking") setMood(baseMood()); });
}
function refreshLED() {
  const sev = [...open.values()].map((e) => e.severity);
  ledColor(!online ? "#8e8e93" : sev.includes("crit") ? "#ff3b30" : sev.includes("warn") ? "#ffcc00" : "#34c759");
}
let flap = 0;
function say(text, { voice = false, urgent = false, hold = 4.5, actions = null, answer = false } = {}) {
  if ((asking && !answer) || (quiet() && !urgent) || !showBubble(text, hold, false, actions)) return;
  if (voice && S.tts && !muted && window.speechSynthesis) { // 默认纯文字；皮肤开启 tts 才朗读
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN"; u.pitch = 1.25; u.rate = 1.05;
    u.onstart = () => { let o = false; flap = setInterval(() => { o = !o; setFace(null, o ? "mouth_open" : "mouth_smile"); }, 130); };
    u.onend = () => { clearInterval(flap); setMood(mood); };
    speechSynthesis.speak(u);
  }
}

// ---------- 音效：跟随动作时长；动作结束暂停并记住位置，再次互动从中断处继续，播完回到开头 ----------
const players = new Map();
let soundGen = 0;
const MIME = { wav: "audio/wav", mp3: "audio/mpeg", m4a: "audio/mp4", aiff: "audio/aiff", caf: "audio/x-caf", ogg: "audio/ogg" };
async function player(path) {
  if (players.has(path)) return players.get(path);
  const buf = await invoke("sound_data", { path });
  const ext = path.split(".").pop().toLowerCase();
  const a = new Audio(URL.createObjectURL(new Blob([buf instanceof ArrayBuffer ? buf : new Uint8Array(buf)], { type: MIME[ext] || "audio/mpeg" })));
  a.preload = "auto";
  players.set(path, a);
  return a;
}
function stopPlayers() { for (const a of players.values()) { a.pause(); URL.revokeObjectURL(a.src); } players.clear(); }
async function playSound(name, dur) {
  if (muted) return console.log(`[sound] 跳过 ${name}：静音中（双击可取消）`);
  if (quiet()) return console.log(`[sound] 跳过 ${name}：专注模式`);
  const path = S.sounds[name];
  if (!path) return console.log(`[sound] 跳过 ${name}：皮肤没有该音效`);
  const g = ++soundGen;
  let a;
  try { a = await player(path); } catch (e) { return console.log(`[sound] 失败 ${name}：${e}`); }
  for (const [k, o] of players) if (k !== path && !o.paused) o.pause();
  if (a.paused) a.play().catch((e) => console.log(`[sound] 播放失败 ${name}：${e}`));
  later(dur, () => { if (soundGen === g) a.pause(); });
}

// ---------- 皮肤定义的动作组合 ----------
function motionDuration(steps) {
  const fixed = { cheer: 1.4, boing: 0.8, blush: 1.5, wave: 1.8, hop: 0.45, squash: 0.35, jiggle: 0.7, spin: 0.9, stretch: 2.0, rub: 1.5, collapse: 2.2, side_eye: 2.0 };
  let d = 0;
  for (const st of steps) {
    const p = st.split(":");
    if (p[0] === "pose") d = Math.max(d, p.length > 2 ? +p[2] || 2 : 2);
    else if (p[0] === "shake" || p[0] === "holo") d = Math.max(d, p.length > 1 ? +p[1] || 1.6 : 1.6);
    else if (p[0] === "tilt") d = Math.max(d, p.length > 2 ? +p[2] || 1 : 1);
    else d = Math.max(d, fixed[p[0]] || 0);
  }
  return d > 0 ? d : 1.5;
}
let eyeHoldUntil = 0;
function perform(key) {
  const opts = S.motions[key];
  if (!opts || !opts.length) return false;
  const steps = pick(opts), dur = motionDuration(steps);
  for (const st of steps) {
    const p = st.split(":"), a = p.slice(1);
    const num = (i, d) => (i < a.length && a[i] !== "" && !isNaN(+a[i]) ? +a[i] : d);
    switch (p[0]) {
      case "tilt": { const r = num(0, 0.12); tilt([0, r, r, 0], num(1, 1.0)); break; }
      case "wave": wave(); break;
      case "hop": hop(num(0, 16)); break;
      case "squash": squash(); break;
      case "jiggle": jiggle(); boing(); break;
      case "boing": boing(); break;
      case "cheer": cheer(); break;
      case "blush": blush(1); later(num(0, 1.5), () => blush(0.35)); break;
      case "spin": spin(); break;
      case "stretch": stretch(); break;
      case "rub": rub(); break;
      case "side_eye": eyeHoldUntil = Date.now() + 2000; lookAt(num(0, -4), 0); break;
      case "holo": holo(true); later(num(0, 2.5), () => holo(false)); break;
      case "particle": particle(a[0] || "✨"); break;
      case "face": setFace(a[0] || null, a[1] || null); break;
      case "say": if (key !== "idle") say(line(a[0] || "idle"), { hold: 3 }); break;
      case "shake": shake(num(0, 1.6)); break;
      case "collapse": collapse(); break;
      case "pose": showPose(a[0] || "", num(1, 2.0)); break;
      case "sound": playSound(a[0] || "", dur); break;
    }
  }
  if (steps.some((s) => s.startsWith("face"))) settle(2.2); // 表情动作结束后回到常态
  return true;
}

// ---------- 手势 ----------
function zoneAt(cx, cy) {
  const [sx, sy, sw, sh] = stageRect, x = (cx - sx) / sw, y = (cy - sy) / sh;
  const zones = (activePose && S.pzones[activePose]) || S.zones; // 坐姿等姿势有自己的可点区域
  for (const [k, b] of Object.entries(zones)) if (b.length === 4 && x >= b[0] && x <= b[2] && y >= b[1] && y < b[3]) return k;
  return null;
}
const variantAt = {};
// 各交互触发的软弹：[部位, 纵向初速度, 横向随机幅度]
const SOFT_KICK = { click_chest: ["chest", 80, 25], click_skirt: ["hip", 75, 40], click_leg: ["hip", 60, 30],
  click_abdomen: ["both", 35, 10], click_cheek: ["chest", 25, 8], click_foot: ["both", 30, 10], press_hold: ["chest", 30, 6],
  rapid_taps: ["both", 70, 30], drag_release: ["both", 85, 25] };
function feedback(key, seconds = 0.75) {
  const choices = (S.variants[key] || []).filter((n) => S.urls["pose_" + n]);
  let name;
  if (choices.length) { // 同一部位多种反应：依次轮换
    const i = (variantAt[key] || 0) % choices.length;
    name = choices[i]; variantAt[key] = (i + 1) % choices.length;
  } else {
    name = S.ipose[key];
    if (!name || !S.urls["pose_" + name]) return false;
  }
  showPose(name, seconds);
  const kick = SOFT_KICK[key];
  if (kick) softKick(kick[0], kick[1], rnd(kick[2]));
  if (seconds != null && S.shared.has(name)) {
    if (key === "click_foot" || key === "drag_release") hop(4, 0.32);
    else rotate(stage, [0, 0.055, 0], 0.35, "sharedFeedback");
  }
  return true;
}
let downAt = 0, downPt = null, dragging = false, patted = false, longPress = 0, pokeWork = 0, downZone = null;
let lastX = 0, lastUpAt = 0, clicks = 0;
scaler.addEventListener("pointerdown", (e) => {
  lensHide();
  if (e.button !== 0) return;
  scaler.setPointerCapture(e.pointerId);
  downAt = Date.now(); downPt = { x: e.screenX, y: e.screenY }; dragging = false; patted = false;
  downZone = zoneAt(e.clientX, e.clientY); lastX = e.screenX;
  clearTimeout(longPress);
  longPress = setTimeout(() => { if (feedback("press_hold", null)) patted = true; else pat(); }, 550);
});
scaler.addEventListener("pointermove", (e) => {
  if (!downPt) return;
  const dx = e.screenX - downPt.x, dyUp = downPt.y - e.screenY;
  if (!dragging && Math.hypot(dx, dyUp) > 4) {
    dragging = true; clearTimeout(longPress); setMood("dragging");
    if (!Object.keys(S.ipose).length) perform("drag");
    say(line("drag", "哇啊——"), { hold: 1.5 });
    hideMenu();
    console.log(`[pos] drag start dist=${Math.hypot(dx, dyUp).toFixed(1)}`);
    invoke("drag_begin", { stage: stageRect }).catch(() => {});
  }
  if (!dragging) return;
  const vx = e.screenX - lastX; lastX = e.screenX;
  if (Math.abs(vx) > 2) { softKick("both", Math.abs(vx) * 0.8, -vx * 3); softKick("skirt", Math.abs(vx) * 0.5, -vx * 3); }
  const key = Math.abs(dyUp) > Math.abs(dx) ? "drag_lift" : dx < 0 ? "drag_sleeve_left" : "drag_sleeve_right";
  if (!feedback(key, null)) dangle(Math.max(-0.45, Math.min(0.45, -vx * 0.03)));
  else if (S.shared.has(S.ipose[key])) dangle(Math.abs(dyUp) > Math.abs(dx) ? 0 : dx < 0 ? 0.12 : -0.12);
  else dangle(0);
});
function endDrag() {
  console.log(`[pos] endDrag dragging=${dragging}`);
  if (!dragging) return;
  dragging = false; downPt = null;
  invoke("drag_end").catch(() => {});
  dangle(0); setMood("happy");
  if (!feedback("drag_release") && !perform("drop")) squash();
  say(line("drop", "到啦！"), { hold: 1.5 }); settle(1.5);
  later(0.05, saveAnchor);
}
scaler.addEventListener("pointerup", (e) => {
  if (e.button !== 0 || !downPt) return;
  clearTimeout(longPress);
  if (dragging) return endDrag();
  downPt = null;
  if (patted) {
    if (S.ipose.press_hold) clearPose();
    blush(0.35); settle(1.2); patted = false;
    return;
  }
  const now = Date.now();
  clicks = now - lastUpAt < DBL * 1000 ? clicks + 1 : 1;
  lastUpAt = now;
  clearTimeout(pokeWork);
  if (clicks >= 2) {
    if (clicks >= 3 && feedback("rapid_taps")) return;
    if (S.ipose.rapid_taps) pokeWork = setTimeout(toggleMute, DBL * 1000);
    else toggleMute();
  } else {
    const zone = downZone;
    pokeWork = setTimeout(() => { if (zone && feedback(zone)) wakeUp(); else poke(zone); }, DBL * 1000);
  }
});
scaler.addEventListener("pointercancel", () => { clearTimeout(longPress); if (dragging) endDrag(); downPt = null; });
document.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  if (scaler.contains(e.target)) openMenu();
});

function poke(zone) {
  wakeUp(); setMood("happy");
  const [part, vy, spread] = zone === "click_chest" ? SOFT_KICK.click_chest : ["both", 45, 20];
  softKick(part, vy, rnd(spread));
  if (!perform("poke")) {
    const r = Math.floor(Math.random() * 3);
    if (r === 0) { setMood("alert"); hop(); } else if (r === 1) tilt([0, 0.12, 0], 0.5); else wave();
  }
  const status = open.size ? `\n（有 ${open.size} 个异常待处理，右键可体检）` : "";
  if (bubble.classList.contains("hidden") || status) say(line("poke", "在呢！") + status, { hold: 3 });
  settle(1.6);
}
function pat() {
  patted = true; wakeUp(); setMood("happy"); blush(1);
  if (!perform("pat")) {
    tilt([0, -0.1, 0.06, -0.06, 0], 1.2);
    particle("💗", -30); later(0.4, () => particle("💗", -10));
  }
  say(line("pat", "嘿嘿…"), { voice: true, hold: 2.5 });
}
function toggleMute() {
  muted = !muted; store.set("muted", muted);
  setMood("happy"); particle(muted ? "🔇" : "🔊", -20);
  say(line(muted ? "mute_on" : "mute_off", muted ? "安静模式" : "我回来啦"), { hold: 2 });
  if (muted) { window.speechSynthesis && speechSynthesis.cancel(); for (const a of players.values()) a.pause(); }
  settle(1.5);
}

// ---------- 右键菜单（独立弹窗 menu.html，选择结果以事件回传） ----------
function openMenu() {
  lensHide();
  const trust = [], fixes = [], seen = new Set();
  for (const e of open.values()) {
    const ev = e.evidence || {}, name = ev.name || ev.parent || ev.app;
    if (name && e.kind && !seen.has(name + e.kind)) { seen.add(name + e.kind); trust.push({ name, kind: e.kind, title: e.title || e.kind }); }
    for (const f of e.fixes || []) fixes.push({ id: e.id, action: f.id, label: f.label });
  }
  invoke("open_menu", { ctx: { trust, fixes, openCount: open.size, muted, scale, focus, focusManual, focusAuto, focusWhy, selftest: !!window.__selftestMenu } }).catch((err) => console.log("[menu]", err));
}
function hideMenu() { invoke("menu_hide").catch(() => {}); }
listen("defense://error", () => say("防护窗口打开失败", { urgent: true }));
// 专注模式下只有“请诊所做事”的操作算唤醒；戳、摸、双击、开关设置都不解除静默
const SERVICE_ACTS = new Set(["checkup", "type", "fix", "log", "account", "skin", "wear", "learn", "defense"]);
listen("menu://action", ({ payload: a }) => {
  if (SERVICE_ACTS.has(a.act)) engage();
  switch (a.act) {
    case "focus_toggle": focusManual = !focusManual; store.set("focusManual", focusManual);
      say(focusManual ? "🎮 专注模式已开启：我不弹窗、不出声、不置顶，只有严重问题或你按快捷键/用菜单找我时才出来～" : "专注模式已关闭",
        { urgent: true, hold: 3 });
      later(3.2, checkFocusAndIdle); break;
    case "focus_auto": focusAuto = !focusAuto; store.set("focusAuto", focusAuto);
      say(focusAuto ? "好的，全屏或玩游戏时我会自动进入专注模式" : "好的，不再自动进入专注模式", { urgent: true, hold: 3 });
      later(3.2, checkFocusAndIdle); break;
    case "checkup": askClinic("帮我全面体检一下"); break;
    case "defense": invoke("open_defense").catch(() => say("防护窗口打开失败", { urgent: true })); break;
    case "type": wake(false, true); break;
    case "skin": switchSkin(a); break;
    case "wear": wearItem(a); break;
    case "account": invoke("open_defense").catch(() => accountMenu()); break;
    case "log": openLog(); break;
    case "learn": learnFlow(false); break;
    case "trust": trust(a.name, a.kind); break;
    case "fix": confirmFix(open.get(a.id), a.action); break;
    case "autostart": api("POST", "/autostart", { enabled: a.on }).then((r) => say(r.enabled ? "好的，以后开机我会自动上岗～" : "已关闭开机自动启动")).catch(() => say("设置失败：守护进程未运行")); break;
    case "dismiss_all": for (const id of [...open.keys()]) dismiss(id, true); say("好的，这些我都记下了～"); break;
    case "mute": toggleMute(); break;
    case "scale": scale = a.v; store.set("scale", scale); layout("scale"); break;
    case "quit": invoke("quit"); break;
  }
});
function lockedHint(a) {
  say(`「${a.name}」需要皮肤权益。我带你查看套餐。`, { hold: 4 });
  invoke("open_defense").catch(() => accountMenu());
}
async function switchSkin(a) {
  if (a.locked) return lockedHint(a);
  try { const r = await api("POST", "/skin", { pack: a.id }); if (!r.ok) say(r.error || "换装失败", { urgent: true }); }
  catch { say("换装失败：守护进程未运行", { urgent: true }); }
}
async function wearItem(a) {
  if (a.locked) return lockedHint(a);
  try {
    const r = await api("POST", "/wardrobe", { equip: a.id });
    if (!r.ok) say(r.error || "换装失败", { urgent: true }); else { setMood("happy"); particle("✨"); settle(1.5); }
  } catch { say("换装失败：守护进程未运行", { urgent: true }); }
}
// 账户与订阅：门户 OAuth 登录（浏览器完成），权益由门户签名下发；未配置门户时提示即将开放
async function accountMenu(prefix = "") {
  let s;
  try { s = await api("GET", "/account"); } catch { return say("暂时查不到账户：守护进程未运行", { urgent: true }); }
  const head = prefix ? prefix + "\n" : "";
  if (!s.configured) return say(head + (s.paywall ? "会员订阅即将开放，届时用门户账号登录即可解锁～" : "测试期全部款式免费开放，正式订阅即将上线～"), { hold: 8 });
  if (!s.logged_in) {
    return say(head + "登录门户账号后，订阅的套餐会自动同步解锁～", { hold: 15, actions: [
      { label: "👤 登录门户", kind: "primary", on: accountLogin }, { label: "以后再说", on: () => {} }] });
  }
  const exp = s.expires ? new Date(s.expires * 1000).toLocaleDateString() : "长期";
  const acts = [{ label: "🔄 同步权益", kind: "primary", on: accountRefresh }];
  if (s.manage_url) acts.push({ label: "账户中心 · 兑换与订阅", on: () => api("POST", "/account/manage", {}).catch(() => {}) });
  acts.push({ label: "退出登录", on: async () => { await api("POST", "/account/logout", {}).catch(() => {}); say("已退出门户账号"); } });
  say(head + `👤 ${s.user || "已登录"}\n套餐：${s.plan_name || s.plan || "免费"}（到期：${exp}）` + (s.error ? `\n⚠️ ${s.error}` : ""), { hold: 15, actions: acts });
}
async function accountLogin() {
  let r;
  try { r = await api("POST", "/account/login", {}); } catch { return say("登录失败：守护进程未运行", { urgent: true }); }
  if (!r.ok) return say(r.error || "暂时无法登录", { urgent: true });
  say("已在浏览器打开门户登录页，登录完成后回来就能用啦～", { hold: 8 });
  const t0 = Date.now(); // 等浏览器登录完成：轮询账户状态（最多 3 分钟）
  const poll = setInterval(async () => {
    const s = await api("GET", "/account").catch(() => null);
    if (s && s.logged_in && s.fetched) { clearInterval(poll); setMood("happy"); particle("🎉"); say(`登录成功！套餐：${s.plan_name || s.plan || "免费"}`, { urgent: true }); }
    else if (Date.now() - t0 > 180000) clearInterval(poll);
  }, 3000);
}
async function accountRefresh() {
  const r = await api("POST", "/account/refresh", {}).catch(() => ({ ok: false, msg: "守护进程未运行" }));
  say(r.ok ? `✅ ${r.msg}` : `⚠️ ${r.msg}`, { hold: 6 });
}
async function openLog() {
  try {
    const r = await api("POST", "/history/report", { days: 7 });
    if (!r.path) throw 0;
    await invoke("open_report", { path: r.path });
  } catch { say("暂时打不开日志：守护进程未运行", { urgent: true }); }
}
// ---------- 修复：先说清楚要做什么，用户确认后执行，再报告结果 ----------
function confirmFix(e, action) {
  const f = e && (e.fixes || []).find((x) => x.id === action);
  if (!f) return say("这个异常已经结束了～");
  wakeUp(); setMood("worried");
  say(`要${f.label}吗？\n${f.detail}`, { urgent: true, hold: 0, actions: [
    { label: f.irreversible ? "确定结束" : "确定", kind: f.irreversible ? "danger" : "primary", on: () => runFix(e, f) },
    { label: "取消", on: () => settle(0.3) }] });
}
async function runFix(e, f) {
  setMood("thinking"); showBubble(`正在${f.label}…`, 0);
  let r;
  try { r = await api("POST", "/fix", { id: e.id, action: f.id }); } catch { r = { ok: false, msg: "守护进程未运行" }; }
  if (r.ok && r.pending) { setMood("thinking"); say(`🧑‍🔧 ${r.msg}`, { urgent: true, hold: 6 }); return; }
  if (r.ok && r.guide) { setMood("happy"); say(r.msg, { urgent: true, hold: 6 }); }
  else if (r.ok) { setMood("happy"); particle("✨"); say(`修好啦：${r.msg}`, { urgent: true, hold: 5 }); }
  else { setMood("worried"); say(`没修成：${r.msg}`, { urgent: true, hold: 6 }); }
  settle(3);
}
async function dismiss(id, quiet) {
  try { await api("POST", "/dismiss", { id }); } catch {}
  if (!quiet) { say("好的，记下了～"); settle(1); }
}
// 智能体申请修复：请用户确认（专注模式下暂缓，退出专注或用户唤醒时再弹）
function showRequest(e) {
  const rq = e.request, f = rq && (e.fixes || []).find((x) => x.id === rq.action);
  if (!f) return false;
  if (quiet()) { deferred.add(e.id); return true; }
  deferred.delete(e.id);
  wakeUp(); setMood("worried"); particle("🙋");
  say(`🤖 ${rq.by} 申请：${f.label}${rq.reason ? "\n理由：" + rq.reason : ""}\n${f.detail}`, { urgent: true, hold: 0, actions: [
    { label: f.irreversible ? "允许（结束）" : "允许", kind: f.irreversible ? "danger" : "primary", on: () => runFix(open.get(e.id), f) },
    { label: "拒绝", on: () => { api("POST", "/fix/reject", { id: e.id }).catch(() => {}); say("好的，已拒绝～"); settle(1); } }] });
  return true;
}
function flushDeferred() {
  if (quiet()) return;
  for (const id of [...deferred]) {
    const e = open.get(id);
    deferred.delete(id);
    if (e && e.request && e.request.state === "pending") return void showRequest(e); // 一次只弹一个
  }
}
// ---------- 规则沉淀：反复出现的问题逐条请用户采纳/忽略；已学会的规则可以撤销 ----------
async function learnFlow(auto) {
  let r;
  try { r = await api("GET", "/learn"); } catch { if (!auto) say("暂时查不到：守护进程未运行", { urgent: true }); return; }
  const list = (r.suggestions || []).slice(0, 3), rules = r.rules || []; // 一次最多问 3 条，其余下次再问
  if (list.length) return showSuggestion(list, 0);
  if (auto) return;
  say(rules.length ? `暂时没有新的可沉淀规则，已学会 ${rules.length} 条。` : "暂时没有可沉淀的规则：同一个问题反复出现后我会提议～",
    { urgent: true, hold: 8, actions: rules.length ? [{ label: "查看已学会的", kind: "primary", on: () => showRule(rules, 0) }] : null });
}
function showSuggestion(list, i) {
  if (i >= list.length) return say("这些都处理完了～", { urgent: true, hold: 3 });
  const s = list[i];
  say(`📚 规则沉淀（${i + 1}/${list.length}）\n${s.title}\n${s.detail}`, { urgent: true, hold: 0, actions: [
    { label: "采纳", kind: "primary", on: async () => {
      const r = await api("POST", "/learn/apply", { id: s.id }).catch(() => ({ msg: "守护进程未运行" }));
      say(r.msg, { urgent: true, hold: 3 }); later(3, () => showSuggestion(list, i + 1)); } },
    { label: "忽略", on: () => { api("POST", "/learn/dismiss", { id: s.id }).catch(() => {}); showSuggestion(list, i + 1); } },
    { label: "以后再说", on: () => settle(0.5) }] });
}
function showRule(rules, i) {
  if (i >= rules.length) return say("就这些～", { urgent: true, hold: 2 });
  const x = rules[i];
  say(`📚 已学会（${i + 1}/${rules.length}）\n「${x.sample}」\n→ ${x.cause}`, { urgent: true, hold: 0, actions: [
    { label: "下一条", kind: "primary", on: () => showRule(rules, i + 1) },
    { label: "撤销这条", on: async () => {
      await api("POST", "/learn/remove", { id: x.id }).catch(() => {});
      say("已撤销，以后这个报错重新交给模型分析", { urgent: true, hold: 3 }); later(3, () => showRule(rules.filter((y) => y.id !== x.id), i)); } },
    { label: "关闭", on: () => settle(0.5) }] });
}
// 每天第一次见面：用一句话复盘昨天到现在
let proactive = "alert";
async function syncAgent() { try { proactive = (await api("GET", "/agent")).settings.proactive; } catch {} }
setInterval(syncAgent, 60000); syncAgent();
async function dailyRecap() {
  await syncAgent();
  if (quiet()) return;
  if (proactive === "quiet") return;
  const today = new Date().toDateString();
  if (store.get("recapDay", "") === today) return;
  // 每周第一次见面：先播本周复盘（模型汇总；没有密钥时为本地结论）
  const wk = await api("GET", "/history/weekly").catch(() => null);
  if (wk && wk.week && store.get("recapWeek", "") !== wk.week) {
    store.set("recapWeek", wk.week); store.set("recapDay", today);
    if (wk.total) say("🗓 本周复盘\n" + (wk.llm || wk.lines.join("\n")), { hold: 14 });
    later(wk.total ? 15 : 1, () => { if (!quiet()) learnFlow(true); }); // 复盘之后：有可沉淀的规则就逐条请你确认
    return;
  }
  let r;
  try { r = await api("GET", "/history/summary?days=1"); } catch { return; }
  if (!r || !r.lines) return;
  store.set("recapDay", today);
  if (r.total) say("📋 过去 24 小时复盘\n" + r.lines.join("\n"), { hold: 10 });
}
async function trust(name, kind) {
  try { await api("POST", "/trust", { name, kinds: [kind] }); } catch {}
  say(`好的，以后不再提醒「${name}」这一项～`); setMood("happy"); settle(1.5);
}

// ---------- 唤醒 / 提需求（Ctrl+Alt+Space：轻按打字，按住说话） ----------
let hotIsDown = false, hotAt = 0, rec = null, heard = "";
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
function hotDown() {
  if (hotIsDown || composing() || asking) return;
  hotIsDown = true; hotAt = Date.now(); engage(); wakeUp(); setMood("listening");
  later(0.35, () => {
    if (!hotIsDown || !SR) return;
    heard = "";
    try {
      rec = new SR(); rec.lang = "zh-CN"; rec.interimResults = true; rec.continuous = true;
      rec.onresult = (ev) => { heard = [...ev.results].map((r) => r[0].transcript).join(""); showBubble("🎙 " + heard, 0); };
      rec.onerror = () => { rec = null; };
      rec.start();
      showBubble(line("listen", "我在听…"), 0);
    } catch { rec = null; }
  });
}
function hotUp() {
  if (!hotIsDown) return;
  hotIsDown = false;
  if (Date.now() - hotAt < 350) return wake(false, true);
  if (!rec) { // 这台电脑的网页内核不支持语音识别：直接转为打字
    wake(false, true); btext.textContent = "这台电脑暂不支持语音输入，请直接打字～"; return;
  }
  const r = rec; rec = null;
  r.onend = () => { if (heard) askClinic(heard); else { say("没听清，再说一次？"); settle(1); } };
  r.stop();
}
function wake(voice, typing = false) {
  if (composing()) { if (typing) binput.focus(); return; }
  if (asking) return;
  wakeUp(); setMood("happy");
  if (!perform("wake")) wave();
  const greet = line("wake", "我在！要优化什么？");
  if (typing) { onSubmit = null; showBubble(greet, 0, true); } else say(greet, { voice });
  settle(2);
}
// 流式问答：模型边生成边显示；新一轮（查完工具再回答）时清掉上一轮草稿；拿到最终结果再补上建议和按钮
let streamSeq = 0;
function askStream(text, onDraft) {
  const sid = ++streamSeq;
  return new Promise((resolve) => {
    let draft = "", result = null, unlisten = null;
    listen("clinic://stream", ({ payload: p }) => {
      if (p.sid !== sid) return;
      if (p.end) { if (unlisten) unlisten(); return resolve(result); }
      const d = p.data || {};
      if ("round" in d) draft = "";
      else if (d.delta) { draft += d.delta; showBubble(draft, 0); if (onDraft) onDraft(draft); }
      else if (d.result) result = d.result;
    }).then((u) => {
      unlisten = u;
      invoke("api_stream", { path: "/ask", body: { text, stream: true }, sid }).catch(() => { u(); resolve(null); });
    }).catch(() => resolve(null));
  });
}
async function askClinic(text) {
  if (asking || composing()) return;
  asking = true;
  try {
    if (/套餐|订阅|激活|授权/.test(text) && !/Claude|阻断|解锁|锁定|日志|记录/i.test(text)) {
      const defense = await api("GET", "/defense/status").catch(() => null);
      const guide = defense?.authorized ? "我可以带你检查环境、查看状态和启动防护。" : "我可以带你选套餐并激活这台终端。";
      say(guide, { urgent: true, answer: true, hold: 12,
        actions: [{ label: defense?.authorized ? "跟露米继续" : "让露米引导", kind: "primary", on: () => invoke("open_defense", {assistant:true}) }] });
      return;
    }
    setMood("thinking"); tilt([0, 0.1, 0.1, 0], 1.0);
    showBubble(`收到：「${text}」\n正在体检…`, 0);
    const r = await askStream(text);
    if (!r || typeof r.reply !== "string") {
      setMood("worried"); say("连不上诊所守护进程，请先启动诊所服务", { urgent: true, answer: true }); return;
    }
    const adv = (r.advice || []).map((x) => "• " + x).join("\n"), det = (r.details || []).join("\n");
    // 模型提议的处方：只渲染白名单动作，点了仍要二次确认
    const acts = (r.actions || []).map((f) => ({ label: "🛠 " + f.label, kind: "primary", on: () => confirmFix({ id: f.event_id, fixes: [f] }, f.id) }));
    if (r.intent === "protection") acts.push(protectionLogAction());
    setMood(adv ? "worried" : "happy");
    if (!adv && !acts.length) particle("✨");
    if (r.source) console.log(`[ask] ${r.source} ${r.model || ""} ${r.ms}ms ${r.fallback ? "回退：" + r.fallback : ""}`);
    say([r.reply, det, adv, r.note || ""].filter(Boolean).join("\n"), { urgent: true, answer: true, hold: acts.length ? 15 : 9, actions: acts.length ? acts : null });
    settle(3);
  } finally { asking = false; }
}

// ---------- 空闲行为 ----------
function blinkLoop() {
  later(2.5 + Math.random() * 3.5, () => {
    if ((eyes === "eyes_open" || eyes === "eyes_worried") && !S.whole.has("eyes_closed")) {
      const prev = eyes;
      setFace("eyes_closed");
      later(0.12, () => { if (eyes === "eyes_closed" && mood !== "sleep") setFace(prev); });
    }
    blinkLoop();
  });
}
function idleLoop() {
  later(14 + Math.random() * 16, () => {
    if (!composing() && !asking && bubble.classList.contains("hidden")
        && mood === "normal" && !quiet() && !sleeping && !dragging && !perform("idle")) {
      switch (Math.floor(Math.random() * 6)) {
        case 0: tilt([0, 0.14, 0.14, -0.14, -0.14, 0], 2.6); break;
        case 1: wave(); break;
        case 2: stretch(); setFace("eyes_closed", "mouth_open"); settle(2); break;
        case 3: holo(true); setFace(null, "mouth_cat"); later(3, () => { holo(false); settle(0); }); break;
        case 4: for (let i = 0; i < 3; i++) later(i * 0.6, () => particle("♪", i * 8 - 30)); setFace("eyes_happy", "mouth_cat"); settle(2); break;
        default: hop(10, 0.5);
      }
    }
    idleLoop();
  });
}
let lastEye = "0,0";
// ---------- 悬停放大（仅露米）：在角色身上停留 3 秒，光标处出现放大镜 ----------
const MAG = { delay: 3000, size: 132, zoom: 2.4, jitter: 6 };
const lens = document.createElement("div"), lensImg = document.createElement("img");
lens.id = "lens"; lens.className = "hidden"; lensImg.alt = ""; lens.appendChild(lensImg); document.body.appendChild(lens);
let hoverAt = 0, hoverPt = null, hoverTimer = 0, lensOn = false;
const isLumi = () => /露米|lumi/i.test(S.name || "");
function overCharacter(x, y) { // 光标是否在角色不透明像素上（用当前鼠标穿透遮罩判断）
  if (!lastMask) return false;
  const c = Math.floor(x / CELL), r = Math.floor(y / CELL);
  return c >= 0 && r >= 0 && c < lastMask.cols && r < lastMask.rows && !!lastMask.bits[r * lastMask.cols + c]
    && !(bubble && !bubble.classList.contains("hidden") && x >= bubble.offsetLeft && x <= bubble.offsetLeft + bubble.offsetWidth
         && y >= bubble.offsetTop && y <= bubble.offsetTop + bubble.offsetHeight);
}
function lensHide() { clearTimeout(hoverTimer); hoverTimer = 0; hoverPt = null; if (lensOn) { lensOn = false; lens.classList.add("hidden"); } }
function lensShow(x, y) {
  const src = softSource().img.getAttribute("src");
  if (!src) return lensHide();
  const [sx, sy] = stageRect, sw = S.W * scale, sh = S.H * scale;
  const ox = sx + parseFloat(scaler.style.left || 0) - SIDE, oy = sy + parseFloat(scaler.style.top || 0) - TOP; // 画面实际左上角
  const R = MAG.size / 2, z = MAG.zoom;
  // 镜片整体放在窗口内；镜片中心对准光标，内容以光标点为中心放大
  const cx = Math.max(R + 2, Math.min(WW - R - 2, x)), cy = Math.max(R + 2, Math.min(WH - R - 2, y));
  Object.assign(lens.style, { left: cx - R + "px", top: cy - R + "px", width: MAG.size + "px", height: MAG.size + "px" });
  if (lensImg.getAttribute("src") !== src) lensImg.src = src;
  Object.assign(lensImg.style, { width: sw * z + "px", height: sh * z + "px",
    left: R - (x - ox) * z + (cx - x) + "px", top: R - (y - oy) * z + (cy - y) + "px" });
  if (!lensOn) { lensOn = true; lens.classList.remove("hidden"); }
}
function hoverTrack(x, y) {
  if (!isLumi() || dragging || downPt || mood === "sleep" || quiet() || !overCharacter(x, y)) return lensHide();
  if (lensOn) return lensShow(x, y);              // 已放大：跟随光标
  if (!hoverPt || Math.hypot(x - hoverPt.x, y - hoverPt.y) > MAG.jitter) { // 移动超过容差：重新计时
    hoverPt = { x, y }; clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => { if (hoverPt) lensShow(hoverPt.x, hoverPt.y); }, MAG.delay);
  }
}

let lensTesting = false; // 自检放大镜时屏蔽真实光标，避免测试被用户鼠标干扰
listen("clinic://cursor", ({ payload: p }) => {
  if (!lensTesting) hoverTrack(p.x, p.y);
  if (mood === "sleep" || dragging || Date.now() < eyeHoldUntil) return;
  const [sx, sy, sw, sh] = stageRect;
  if (Object.keys(S.gaze).length) {
    if (mood !== "normal" || patted || activePose) return;
    const dx = (p.x - sx) / sw - 0.516, dy = (p.y - sy) / sh - 0.203;
    const inside = p.x >= sx && p.x <= sx + sw && p.y >= sy && p.y <= sy + sh;
    const dir = inside && Math.hypot(dx, dy) > 0.052 ? (Math.abs(dx) > Math.abs(dy) * 1.3 ? (dx < 0 ? "left" : "right") : dy > 0 ? "down" : "up") : null;
    if (dir !== gazeDir) { gazeDir = dir; gaze(dir); }
    return;
  }
  if (!S.eyeTrack) return;
  const cx = sx + sw / 2, cy = sy + sh / 2 - 30 * scale, dx = p.x - cx, dy = cy - p.y;
  const q = (v, m) => Math.round(Math.max(-m, Math.min(m, v / 60)) * 2) / 2;
  const t = Math.hypot(dx, dy) > 700 ? [0, 0] : [q(dx, 3), q(dy, 2.5)];
  if (t.join() !== lastEye) { lastEye = t.join(); lookAt(t[0], t[1]); }
});
function wakeUp() { if (sleeping) { sleeping = false; setMood(baseMood()); } }
async function checkFocusAndIdle() {
  if (mood === "worried" && !dragging && baseMood() === "normal") setMood("normal"); // 普通警告 60 秒后恢复常态
  const [idle, busy] = await Promise.all([invoke("idle_seconds").catch(() => 0), invoke("busy_fullscreen").catch(() => false)]);
  if (idle > 300 && !sleeping && mood === "normal") { sleeping = true; setMood("sleep"); particle("💤"); }
  else if (idle < 5 && sleeping) wakeUp();
  if (sleeping && Math.random() < 1 / 3) particle("💤");
  const hint = focusAuto && !busy ? await api("GET", "/focus").catch(() => ({})) : {};
  const want = focusManual || (focusAuto && (busy || !!hint.game));
  focusWhy = focusManual ? "手动开启" : busy ? "检测到全屏（游戏/视频/演示）" : hint.game ? `检测到游戏：${hint.game}` : "";
  if (want !== focus) {
    focus = want;
    if (quiet() && !composing() && !asking) hideBubble();
    else if (!quiet()) flushDeferred();
  }
  applyQuiet();
}

// ---------- 守护进程事件 ----------
function handle(e) {
  if (typeof e.id !== "number") return;
  api("POST", "/ack", { id: e.id }).catch(() => {});
  const sev = e.severity || "info";
  if (e.state === "open") {
    const prev = open.get(e.id);
    let isNew = !prev;
    open.set(e.id, e); refreshLED();
    if (e.autofix === "pending") return; // 免确认自动修复进行中：只亮状态灯，等结果
    const rq = e.request, prq = prev && prev.request;
    if (rq && rq.state === "pending" && !(prq && prq.state === "pending" && prq.t === rq.t) && showRequest(e)) return;
    const rp = e.repair, prp = prev && prev.repair;
    if (rp && rp.state === "failed" && prp && prp.state === "running") { // DSH 修复没成功：如实告知
      setMood("worried"); say(`🧑‍🔧 ${rp.agent || "智能体"}没修成：${rp.msg}`, { urgent: !quiet(), hold: 10 }); settle(3); return;
    }
    if (prev && prev.autofix !== "skipped" && e.autofix === "skipped") { // 自动修复被暂停或失败：改回请用户确认
      if (prev.autofix === "pending") isNew = true;
      else { say(`⏸ 自动修复没有执行：${e.autofix_note || ""}`, { hold: 8 }); return; }
    }
    // 模型补充诊断到达：已经提醒过的异常再用一句话补充（勿扰时不打扰）
    if (prev && e.diagnosis_llm && !prev.diagnosis_llm && sev !== "info" && !quiet() && proactive !== "quiet") {
      const dl = e.diagnosis_llm, more = (dl.advice || []).slice(0, 2).map((x) => "• " + x).join("\n");
      const acts = (e.fixes || []).map((f) => ({ label: "🛠 " + f.label, kind: "primary", on: () => confirmFix(open.get(e.id), f.id) }));
      say(`🔎 补充诊断：${dl.cause}\n${more}`, { hold: 12, actions: acts.length ? acts : null });
      return;
    }
    if (!isNew || sev === "info") return;
    if (proactive === "quiet" && sev !== "crit") return; // “只在被叫时说话”：普通警告只亮状态灯
    if (quiet()) { if (sev !== "crit") return; peekUntil = Date.now() + 20000; applyQuiet(); later(20.5, applyQuiet); } // 专注模式：普通警告只亮灯；严重异常才出来（临时置顶 20 秒）
    wakeUp();
    const d = e.diagnosis_llm || e.diagnosis || {};
    const adv = (d.advice || []).slice(0, 2).map((x) => "• " + x).join("\n"), crit = sev === "crit";
    setMood(crit ? "alert" : "worried"); if (crit) hop(); particle(crit ? "❗" : "💦");
    const ev = e.evidence || {}, who = ev.name || ev.parent || ev.app, acts = [];
    for (const f of e.fixes || []) acts.push({ label: "🛠 " + f.label, kind: "primary", on: () => confirmFix(open.get(e.id), f.id) });
    acts.push({ label: "知道了", on: () => dismiss(e.id) });
    if (who) acts.push({ label: "以后不提醒", on: () => trust(who, e.kind) });
    const note = e.autofix === "skipped" ? `\n⏸ 自动修复没有执行：${e.autofix_note || ""}` : "";
    say(`${e.title || ""}\n病因：${d.cause || ""}\n${adv}${note}`, { voice: crit, urgent: crit, hold: 15, actions: acts });
    settle(2.5);
  } else if (open.delete(e.id)) {
    refreshLED();
    if (e.resolved_by === "autofix") { // 免确认自动修复完成：事后告知做了什么
      setMood("happy"); particle("🤖");
      say(`🤖 已自动处理：${(e.title || "").replace(/^复发：/, "")}\n${e.autofix_note || ""}`, { hold: 10 });
      settle(2); return;
    }
    if ((e.resolved_by === "dsh" || e.resolved_by === "codex") && e.repair) { // DSH/Codex 修好代码：说明改了哪些文件
      const ch = e.repair.changed || [], files = ch.slice(0, 4).join("、") + (ch.length > 4 ? ` 等 ${ch.length} 个文件` : "");
      setMood("happy"); particle("🧑‍🔧");
      say(`🧑‍🔧 ${e.repair.agent} 修好了（${e.repair.secs}s）：${e.repair.msg}\n改动：${files}`, { urgent: !quiet(), hold: 12 });
      settle(2); return;
    }
    if (!quiet()) { setMood("happy"); if (!perform("resolved")) particle("✨"); say(line("resolved", "恢复啦！"), { hold: 2.5 }); settle(2); }
  }
}

listen("clinic://skin", async ({ payload }) => {
  const first = S.ver === 0;
  stopPlayers(); gazeDir = null;
  applySkin(loadSkin(payload));
  refreshLED();
  await layout("skin");
  if (first) later(4, dailyRecap);
});

const protectionLogAction = () => ({label: "查看防护日志", kind: "primary",
  on: () => invoke("open_defense", { journal: true }).catch(() => say("防护日志打开失败", { urgent: true }))});
let guardPauseSince = null, guardReadySince = null, guardPauseShown = false, guardCriticalShown = false;
const guardCritical = new Set(["guard_network_changed", "guard_rule_error", "guard_policy_blocking"]);
const guardInterrupted = new Set(["guard_waiting_tunnel", "guard_checking", "guard_stabilizing",
  "guard_probe_unavailable", "guard_verifying", "guard_https_wait"]);

function showProtectionNotice() {
  if (!pendingProtection || asking || composing() || !bubble.classList.contains("hidden")) return;
  const notice = pendingProtection;
  if (quiet() && !notice.urgent) return;
  pendingProtection = null;
  if (notice.system) {
    guardPauseShown = !notice.recovered;
    if (notice.recovered || notice.urgent) guardCriticalShown = !notice.recovered;
  }
  say(`${notice.title} · ${notice.scope}\n${notice.detail}`,
    { urgent: notice.urgent, hold: 15, actions: [protectionLogAction()] });
}

function updateGuardNotice(report, now) {
  const state = report.state;
  const status = report.system_guard?.recovery;
  if (state === "guard_ready") {
    guardPauseSince = null;
    guardReadySince ??= now;
    if (pendingProtection?.system && !pendingProtection.recovered) pendingProtection = null;
    if (guardPauseShown && status && now - guardReadySince >= 12000) {
      pendingProtection = {...status, system: true, recovered: true, scope: "macOS 系统防护"};
    }
    return;
  }
  guardReadySince = null;
  if (pendingProtection?.system) pendingProtection = null;
  const critical = guardCritical.has(state);
  if (!critical && !guardInterrupted.has(state)) {
    guardPauseSince = null;
    return;
  }
  guardPauseSince ??= now;
  if ((critical && !guardCriticalShown) || (!guardPauseShown && now - guardPauseSince >= 12000)) {
    const notice = status || {title:"防护需要检查", detail:"请打开防护日志查看系统防护规则。"};
    pendingProtection = {...notice, system: true, urgent: critical, scope: "macOS 系统防护"};
  }
}
let protectionCursor, protectionPolling = false;
async function checkProtection() {
  if (protectionPolling || protectionCursor === false || !online) return;
  protectionPolling = true;
  try {
    const report = await api("GET", "/defense/protection");
    if (report?.http_status === 404) { protectionCursor = false; return; }
    if (!Array.isArray(report?.events)) return;
    const now = Date.now();
    updateGuardNotice(report, now);
    const local = report.events.find(event => event.id && event.kind);
    if (local && local.id !== protectionCursor) {
      const active = (local.kind === "tcp_wait" && report.state === "tcp_wait")
        || (local.kind === "gateway_locked" && report.state === "gateway_locked");
      if (!report.state?.startsWith("guard_") && (protectionCursor !== undefined || active || now - local.time * 1000 < 30000)) {
        pendingProtection = {...local, urgent: local.kind === "gateway_locked"};
      }
      protectionCursor = local.id;
    }
    showProtectionNotice();
  } catch { /* The daemon can be restarting. */ }
  finally { protectionPolling = false; }
}
listen("clinic://events", ({ payload }) => payload.forEach(handle));
listen("clinic://online", async ({ payload: on }) => {
  if (on === online) return;
  online = on; refreshLED();
  if (!on) { if (!shown) await layout("offline"); say("等待诊所守护进程启动…", { urgent: !quiet() }); }
});
listen("clinic://dragend", () => endDrag());
listen("clinic://hotkey", ({ payload: down }) => (down ? hotDown() : hotUp()));

setInterval(checkFocusAndIdle, 4000);
setInterval(checkProtection, 4000);
blinkLoop(); idleLoop();
invoke("ui_ready").catch(() => {});

// ---------- 自检（仅 CLINIC_SELFTEST=1 时运行，用于无人值守验收） ----------
invoke("selftest").then(async (on) => {
  if (!on) return;
  const wait = (s) => new Promise((r) => setTimeout(r, s * 1000));
  const log = (...a) => console.log("[selftest]", ...a);
  while (!S.ver) await wait(0.3);
  await wait(1.5);
  const maskCells = () => { const [sx, sy, sw, sh] = stageRect; return visibleUrls().length + " layers, stage " + sw + "x" + sh; };
  log("skin", S.ver, S.W + "x" + S.H, "layers", Object.keys(S.urls).length, maskCells(), "zones", Object.keys(S.zones).join(","));
  if (softOn()) {
    log("soft demo begin");
    softKick("chest", 90, 10);
    for (let i = 0; i < 8; i++) { await wait(0.1); const c = soft.state.chest; log(`soft t=${(i + 1) / 10} y=${c.y.toFixed(2)} vy=${c.vy.toFixed(1)} raf=${!!soft.raf} attached=${!!soft.img} canvas=${soft.canvas ? soft.canvas.width + "x" + soft.canvas.height + "@" + (soft.canvas.parentNode && soft.canvas.parentNode.dataset.l) : "none"} hidden=${soft.img ? soft.img.style.visibility : "-"}`); }
    await wait(0.8);
    log("soft regions", JSON.stringify(softRegions(softSource())));
    softKick("hip", 85, 20); await wait(0.3); log("soft hip (正面不应晃)", soft.state.hip.vy.toFixed(1), "exposed=" + hipExposed());
    softKick("skirt", 30, 60); await wait(0.3); log("soft skirt", soft.state.skirt.x.toFixed(2)); await wait(1.3);
    hop(); await wait(1.6);
    log("soft demo end", "running=" + !!soft.raf);
    await wait(1);
  }
  { // 悬停放大
    const z = S.zones.click_chest || [0.42, 0.3, 0.58, 0.38];
    const x = parseFloat(scaler.style.left) + ((z[0] + z[2]) / 2) * S.W * scale, y = parseFloat(scaler.style.top) + ((z[1] + z[3]) / 2) * S.H * scale;
    log("lens test begin", "isLumi=" + isLumi(), "over=" + overCharacter(x, y));
    lensTesting = true; hoverTrack(x, y); await wait(1.5); log("lens at 1.5s", lensOn);
    await wait(2); log("lens at 3.5s", lensOn);
    await wait(1.2); hoverTrack(x + 20, y + 30); log("lens follow", lens.style.left, lens.style.top); await wait(1.2);
    hoverTrack(2, 2); log("lens off-character", lensOn);
    lensTesting = false; log("lens test end");
  }
  log("poke"); poke(); await wait(3);
  log("pat"); downPt = null; pat(); await wait(2.5); patted = false; settle(0);
  for (const k of Object.keys(S.zones)) { log("zone", k, feedback(k)); await wait(1.2); }
  for (const [k, v] of Object.entries(S.variants)) for (let i = 0; i < v.length; i++) { const ok = feedback(k); log("variant", k, activePose, ok); await wait(1.0); }
  for (const p of Object.keys(S.pzones)) { showPose(p, null); log("pose zones", p, Object.keys(S.pzones[p]).join(",")); await wait(1.0); }
  clearPose();
  for (const k of ["press_hold", "drag_lift", "drag_sleeve_left", "drag_release", "rapid_taps"]) { log("feedback", k, feedback(k)); await wait(1.2); }
  clearPose();
  for (const d of Object.keys(S.gaze)) { gaze(d); log("gaze", d, IMG.body.getAttribute("src") !== S.urls.body); await wait(0.8); }
  gaze(null);
  for (const k of Object.keys(S.motions)) { log("motion", k, perform(k)); await wait(2.4); }
  setMood("worried"); log("mood worried", eyes); await wait(1.5); setMood("thinking"); log("mood thinking pose", activePose); await wait(1.5);
  setMood("normal"); clearPose();
  for (const n of Object.keys(S.sounds)) { await playSound(n, 1.5); await wait(0.5); const a = players.get(S.sounds[n]); log("sound", n, a ? `paused=${a.paused} t=${a.currentTime.toFixed(2)}` : "none"); }
  showBubble("自检：气泡显示与遮罩", 3); await wait(0.5); log("bubble at", bubble.style.left, bubble.style.top);
  { // 告警气泡 → 修复确认 → 取消（假事件，不会真的执行修复）
    const fake = { id: 9e15, state: "open", severity: "warn", kind: "cpu_hog", title: "CPU 异常占用：demo", t_detect: Date.now() / 1000,
      evidence: { name: "demo", pid: 1 }, diagnosis: { cause: "自检用假事件", advice: ["确认是否为预期任务"] },
      fixes: [{ id: "terminate", label: "结束「demo」", detail: "自检：不会执行", irreversible: true }] };
    handle(fake); await wait(0.6);
    log("alert actions", [...bacts.children].map((b) => b.textContent).join("|"));
    await wait(2.5);
    confirmFix(open.get(fake.id), "terminate"); await wait(0.4);
    log("confirm actions", [...bacts.children].map((b) => b.textContent + ":" + b.className).join("|"));
    await wait(2.5);
    bacts.children[1].dispatchEvent(new PointerEvent("pointerdown")); await wait(0.3);
    log("after cancel hidden", bubble.classList.contains("hidden"));
    open.delete(fake.id); refreshLED(); settle(0);
  }
  window.__selftestMenu = true; openMenu(); await wait(2); log("menu opened"); window.__selftestMenu = false;
  await wait(4); hideMenu();
  await invoke("open_settings").catch((e) => log("settings error", e)); log("settings opened"); await wait(4);
  { // 流式问答：记录首字时间与增量次数（有密钥时走模型，无密钥时为本地回复、没有增量）
    const t0 = Date.now(); let first = 0, n = 0;
    const r = await askStream("自检：用一句话说说现在电脑状态", () => { n++; if (!first) first = Date.now() - t0; });
    log("ask stream", "first_ms=" + first, "deltas=" + n, "total_ms=" + (Date.now() - t0), "source=" + (r && r.source), "reply=" + (r && r.reply || "").slice(0, 40));
    await wait(3); hideBubble();
  }
  { // 规则沉淀：弹出建议/已学会列表（只展示，不点按钮）
    await learnFlow(false); await wait(1);
    log("learn bubble", (bubble.textContent || "").replace(/\s+/g, " ").slice(0, 120), "| buttons", [...bacts.children].map((b) => b.textContent).join("|"));
    await wait(4); hideBubble();
  }
  log("done");
});
