// 右键菜单弹窗：只负责展示与悬停预览，选中后把动作发回角色窗口执行
"use strict";
const T = window.__TAURI__, invoke = T.core.invoke;
const api = (method, path, body) => invoke("api", { method, path, body: body ?? null });
window.addEventListener("error", (e) => invoke("js_log", { msg: `menu: ${e.message} @${e.lineno}` }));
window.addEventListener("unhandledrejection", (e) => invoke("js_log", { msg: `menu: ${e.reason}` }));
const _log = console.log; console.log = (...a) => { _log(...a); invoke("js_log", { msg: a.join(" ") }).catch(() => {}); };
const $ = (id) => document.getElementById(id);
const wrap = $("wrap"), menu = $("menu"), sub = $("sub"), preview = $("preview"), pimg = preview.querySelector("img");
const H = 480, W = 660, MAC = /Mac/.test(navigator.platform);
let ctx = null, lists = null, cache = new Map(), hoverGen = 0;

function act(a) { T.event.emitTo("sprite", "menu://action", a); close(); }
function close() { sub.classList.add("hidden"); preview.classList.add("hidden"); invoke("menu_hide"); }
window.addEventListener("blur", () => { if (!(ctx && ctx.selftest)) close(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
document.addEventListener("contextmenu", (e) => e.preventDefault());
document.addEventListener("mousedown", (e) => { if (!wrap.contains(e.target)) close(); });

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function item(text, { on, checked, arrow, lock, extra, enter } = {}) {
  const r = el("div", "item");
  r.appendChild(el("span", "ck", checked ? "✓" : ""));
  r.appendChild(el("span", "tx", text));
  if (extra) r.appendChild(el("span", "ex", extra));
  if (lock) r.appendChild(el("span", "lock", "🔒 " + lock));
  if (arrow) r.appendChild(el("span", "ar", "›"));
  if (on) r.addEventListener("click", on);
  r.addEventListener("mouseenter", () => enter ? enter(r) : null);
  return r;
}
const sep = () => el("div", "sep");

function clampTop(node, want) { node.style.top = Math.max(0, Math.min(want, H - node.offsetHeight - 4)) + "px"; }

function render() {
  const at = ctx.at || { x: 0, y: 0, rtl: false };
  wrap.classList.toggle("rtl", !!at.rtl);
  wrap.style.left = at.rtl ? "" : at.x + "px";
  wrap.style.right = at.rtl ? W - at.x + "px" : "";
  wrap.style.top = "0px";
  sub.classList.add("hidden"); preview.classList.add("hidden");
  menu.replaceChildren();
  const plain = () => { sub.classList.add("hidden"); preview.classList.add("hidden"); mark(null); };
  const add = (n) => menu.appendChild(n);
  if (ctx.defenseAvailable) add(item("🛡 AI Defense 防护助手…", { on: () => { invoke("open_defense").catch(console.error); close(); }, enter: plain }));
  add(item("🩺 立即体检", { on: () => act({ act: "checkup" }), enter: plain }));
  for (const f of ctx.fixes || []) add(item(`🛠 ${f.label}…`, { on: () => act({ act: "fix", id: f.id, action: f.action }), enter: plain }));
  if (ctx.openCount) add(item(`✅ 这些异常我知道了（${ctx.openCount}）`, { on: () => act({ act: "dismiss_all" }), enter: plain }));
  for (const t of ctx.trust || []) add(item(`🤝 信任「${t.name}」的${t.title}，不再提醒`, { on: () => act({ act: "trust", name: t.name, kind: t.kind }), enter: plain }));
  add(item("💬 提需求…", { extra: MAC ? "⌃⌥Space" : "Ctrl+Alt+Space", on: () => act({ act: "type" }), enter: plain }));
  add(item("👗 换装", { arrow: true, enter: (r) => openSub(r, "skins") }));
  const wardrobe = item("🧥 衣橱（单件搭配）", { arrow: true, enter: (r) => openSub(r, "wardrobe") });
  wardrobe.id = "wardrobe-entry";
  wardrobe.classList.toggle("hidden", !(lists && lists.wardrobe && lists.wardrobe.modular));
  add(wardrobe);
  add(item("⚙️ 智能体设置…", { on: () => { invoke("open_settings"); close(); }, enter: plain }));
  add(item("📋 诊断日志（近 7 天）", { on: () => act({ act: "log" }), enter: plain }));
  add(item("📚 规则沉淀…", { on: () => act({ act: "learn" }), enter: plain }));
  add(sep());
  const auto = item("🚀 开机自动启动", { checked: !!(lists && lists.autostart), on: () => act({ act: "autostart", on: !(lists && lists.autostart) }), enter: plain });
  auto.id = "auto-entry";
  add(auto);
  add(item(ctx.focus && !ctx.focusManual ? `🎮 专注模式（自动：${ctx.focusWhy || "已开启"}）` : "🎮 专注模式（工作/游戏）",
    { checked: !!ctx.focusManual, on: () => act({ act: "focus_toggle" }), enter: plain }));
  add(item("　全屏或玩游戏时自动专注", { checked: ctx.focusAuto !== false, on: () => act({ act: "focus_auto" }), enter: plain }));
  add(item("🔇 静音（双击角色切换）", { checked: ctx.muted, on: () => act({ act: "mute" }), enter: plain }));
  for (const [t, v] of [["小", 0.55], ["中", 0.72], ["大", 0.9]]) {
    add(item(`尺寸：${t}`, { checked: Math.abs((ctx.scale || 0.72) - v) < 0.01, on: () => act({ act: "scale", v }), enter: plain }));
  }
  add(sep());
  add(item("退出", { on: () => act({ act: "quit" }), enter: plain }));
  clampTop(menu, at.y);
}
function mark(r) { for (const n of menu.children) n.classList.toggle("on", n === r); }

async function loadLists() {
  const [skins, wardrobe, auto] = await Promise.all([api("GET", "/skins").catch(() => null), api("GET", "/wardrobe").catch(() => null),
    api("GET", "/autostart").catch(() => null)]);
  lists = { skins, wardrobe, autostart: !!(auto && auto.enabled) };
  const a = $("auto-entry");
  if (a) a.querySelector(".ck").textContent = lists.autostart ? "✓" : "";
  const w = $("wardrobe-entry");
  if (w) w.classList.toggle("hidden", !(wardrobe && wardrobe.modular));
  if (openKind) fillSub(openKind);
}

let openKind = null, openRow = null;
function openSub(row, kind) {
  mark(row); openKind = kind; openRow = row;
  preview.classList.add("hidden");
  fillSub(kind);
}
function fillSub(kind) {
  sub.replaceChildren();
  const hide = () => preview.classList.add("hidden");
  if (!lists) sub.appendChild(el("div", "empty", "加载中…"));
  else if (kind === "skins") {
    const r = lists.skins;
    if (!r) sub.appendChild(el("div", "empty", "（守护进程未运行）"));
    else {
      const packs = r.packs || [];
      for (const p of packs.filter((p) => !p.group)) sub.appendChild(packRow(p, r.current));
      const groups = [...new Set(packs.filter((p) => p.group).map((p) => p.group))];
      for (const g of groups) {
        sub.appendChild(el("div", "group", "🎀 " + g));
        // 组内按款式分类（大类 › 子类）排列，未分类的放最后
        const inG = packs.filter((p) => p.group === g);
        const rank = (k) => { const p = inG.find((q) => (q.category_name || []).join(" › ") === k); return p && p.category_rank != null ? p.category_rank : 1e9; };
        const keys = [...new Set(inG.map((p) => (p.category_name || []).join(" › ")))].sort((x, y) => rank(x) - rank(y));
        for (const k of keys) {
          const ps = inG.filter((p) => (p.category_name || []).join(" › ") === k);
          if (k) sub.appendChild(el("div", "subgroup", `${k}（${ps.length}）`));
          for (const p of ps) sub.appendChild(packRow(p, r.current));
        }
      }
    }
    sub.appendChild(sep());
    sub.appendChild(item("👤 账户与订阅…", { on: () => act({ act: "account" }), enter: hide }));
  } else {
    const w = lists.wardrobe || {};
    const row = (it) => {
      const name = it.name || it.id, lock = it.locked ? it.price || "" : null;
      return item(name + (it.complete === false ? "（部分动作未适配）" : ""), {
        checked: it.equipped, lock,
        on: () => act({ act: "wear", id: it.id, locked: !!it.locked, name, price: it.price || "" }),
        enter: (r) => showPreview(r, "item=" + encodeURIComponent(it.id), name, lock),
      });
    };
    if (w.categories && w.categories.length) { // 款式分类：大类 › 子类
      for (const c of w.categories) {
        if (!c.count) continue;
        sub.appendChild(el("div", "group", `${c.name}（${c.count}）`));
        for (const sc of c.children) {
          const its = (w.items || []).filter((i) => i.category === `${c.id}/${sc.id}`);
          if (!its.length) continue;
          if (c.children.filter((x) => x.count).length > 1) sub.appendChild(el("div", "subgroup", `${sc.name}（${its.length}）`));
          for (const it of its) sub.appendChild(row(it));
        }
      }
    } else {
      for (const slot of w.slots || []) {
        sub.appendChild(el("div", "group", slot.name || slot.id));
        for (const it of (w.items || []).filter((i) => i.slot === slot.id)) sub.appendChild(row(it));
      }
    }
  }
  sub.classList.remove("hidden");
  const rowTop = openRow ? openRow.getBoundingClientRect().top - wrap.getBoundingClientRect().top - 5 : 0;
  clampTop(sub, menu.offsetTop + rowTop);
}
function packRow(p, current) {
  const name = p.name || p.id, lock = p.locked ? p.price || "" : null;
  return item(name, {
    checked: p.id === current, lock,
    on: () => act({ act: "skin", id: p.id, locked: !!p.locked, name, price: p.price || "" }),
    enter: (r) => showPreview(r, "pack=" + encodeURIComponent(p.id), name, lock),
  });
}
async function showPreview(row, key, title, lock) {
  const g = ++hoverGen;
  for (const n of sub.children) n.classList.toggle("on", n === row);
  if (!cache.has(key)) {
    try {
      const r = await api("GET", "/preview?" + key);
      cache.set(key, r.svg ? URL.createObjectURL(new Blob([r.svg], { type: "image/svg+xml" })) : null);
    } catch { cache.set(key, null); }
  }
  if (g !== hoverGen) return;
  const url = cache.get(key);
  if (!url) return preview.classList.add("hidden");
  pimg.src = url;
  $("ptitle").textContent = title + (lock != null ? "  🔒 " + lock : "");
  preview.classList.remove("hidden");
  const rowMid = row.getBoundingClientRect().top + row.offsetHeight / 2 - wrap.getBoundingClientRect().top;
  clampTop(preview, rowMid - preview.offsetHeight / 2);
}

async function openWith(c) {
  ctx = c; openKind = null; openRow = null; lists = null;
  for (const u of cache.values()) if (u) URL.revokeObjectURL(u);
  cache = new Map(); // 换装/解锁后预览可能变化，每次打开重新取
  render();
  await invoke("menu_show");
  await loadLists();
  if (c.selftest) { // 自检：依次展开“换装”和“衣橱”子菜单，便于截图验收
    const rows = [...menu.querySelectorAll(".item")];
    const skins = rows.find((r) => r.textContent.includes("换装"));
    if (skins) openSub(skins, "skins");
    const w = $("wardrobe-entry");
    if (w && !w.classList.contains("hidden")) setTimeout(() => openSub(w, "wardrobe"), 2500);
  }
}
T.event.listen("menu://open", ({ payload }) => openWith(payload));
if (window.__MENU_CTX__) openWith(window.__MENU_CTX__);
