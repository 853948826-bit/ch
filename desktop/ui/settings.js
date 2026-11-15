// 智能体设置面板：读写守护进程 /agent；密钥框留空表示不修改
"use strict";
const T = window.__TAURI__, invoke = T.core.invoke;
const api = (method, path, body) => invoke("api", { method, path, body: body ?? null });
window.addEventListener("error", (e) => invoke("js_log", { msg: `settings: ${e.message} @${e.lineno}` }));
const $ = (id) => document.getElementById(id);
let data = null;

function fill(d) {
  data = d;
  const s = d.settings;
  $("provider").replaceChildren(...Object.entries(d.presets).map(([k, p]) => new Option(p.label, k, false, k === s.provider)));
  $("base_url").value = s.base_url || "";
  $("base_url").placeholder = d.presets[s.provider].base_url || "https://…/v1";
  $("key").value = ""; $("key").placeholder = d.key_masked ? `已保存（${d.key_masked}），留空不修改` : "粘贴 API 密钥";
  $("models").replaceChildren(...d.presets[s.provider].models.map((m) => new Option(m, m)));
  for (const k of ["model_chat", "model_diag", "model_deep", "timeout_s", "proactive", "privacy"]) $(k).value = s[k];
  $("thinking").checked = !!s.thinking;
  for (const k of ["agent_loop", "resume_stopped", "dev_port"]) $("af_" + k).checked = !!(s.autofix || {})[k];
  $("calls_per_hour").value = s.budget.calls_per_hour; $("daily_usd").value = s.budget.daily_usd;
  for (const k of ["name", "self", "call_user", "tone", "length"]) $("p_" + k).value = s.persona[k] || (k === "length" ? "short" : "");
  $("price-note").textContent = d.presets[s.provider].models.length ? "金额按官方高峰价估算（偏保守）。"
    : "这个提供方的价格未知：金额上限不生效，只按每小时次数限制。";
  const u = d.usage || {};
  $("usage").textContent = `今天：调用 ${u.calls || 0} 次（失败 ${u.fail || 0}），输入 ${u.in || 0} / 输出 ${u.out || 0} Token，约 $${(u.usd || 0).toFixed(4)}。`
    + (d.online ? (d.note ? `  ⚠️ ${d.note}` : "  ✅ 已在线") : "  ⚪ 未配置密钥，当前使用本地规则");
}

function collect() {
  const num = (id) => Number($(id).value);
  return {
    provider: $("provider").value, base_url: $("base_url").value.trim(),
    model_chat: $("model_chat").value.trim(), model_diag: $("model_diag").value.trim(), model_deep: $("model_deep").value.trim(),
    thinking: $("thinking").checked, timeout_s: num("timeout_s"),
    budget: { calls_per_hour: num("calls_per_hour"), daily_usd: num("daily_usd") },
    persona: { name: $("p_name").value, self: $("p_self").value, call_user: $("p_call_user").value, tone: $("p_tone").value, length: $("p_length").value },
    proactive: $("proactive").value, privacy: $("privacy").value,
    autofix: { agent_loop: $("af_agent_loop").checked, resume_stopped: $("af_resume_stopped").checked, dev_port: $("af_dev_port").checked },
  };
}

async function save() {
  const body = { settings: collect() };
  if ($("key").value.trim()) body.key = $("key").value.trim();
  try { fill(await api("POST", "/agent", body)); flash("save-out", "已保存", true); }
  catch { flash("save-out", "保存失败：守护进程未运行", false); }
}
function flash(id, text, ok) { const e = $(id); e.textContent = text; e.className = ok ? "ok" : "bad"; }

$("provider").addEventListener("change", async () => {
  // 切换提供方：先保存当前选择，再按新提供方的预设刷新（各提供方的密钥分别保存）
  const p = $("provider").value, preset = data.presets[p];
  const s = { provider: p, base_url: "" };
  if (preset.models.length) Object.assign(s, { model_chat: preset.models[0], model_diag: preset.models[0], model_deep: preset.models.at(-1) });
  fill(await api("POST", "/agent", { settings: s }));
});
$("save").addEventListener("click", save);
$("test").addEventListener("click", async () => {
  await save();
  flash("test-out", "测试中…", true);
  try {
    const r = await api("POST", "/agent/test", {});
    if (r.ok) flash("test-out", `✅ 连通，${r.ms} 毫秒，模型 ${r.model} 回复「${r.reply}」`, true);
    else flash("test-out", `❌ ${r.error}`, false);
  } catch { flash("test-out", "❌ 守护进程未运行", false); }
  fill(await api("GET", "/agent"));
});
let clearArmed = 0; // 两次点击确认（不依赖系统对话框，各平台网页内核表现一致）
$("clear-key").addEventListener("click", async () => {
  const b = $("clear-key");
  if (Date.now() - clearArmed > 3000) { clearArmed = Date.now(); b.textContent = "再点一次确认清除"; setTimeout(() => (b.textContent = "清除密钥"), 3000); return; }
  clearArmed = 0; b.textContent = "清除密钥";
  fill(await api("POST", "/agent", { clear_key: true })); flash("test-out", "已清除", true);
});
api("GET", "/agent").then(fill).catch(() => flash("save-out", "守护进程未运行", false));
