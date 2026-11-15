"""智能体设置：普通设置存 ~/.cyberclinic/agent.json；API 密钥单独存 ~/.cyberclinic/secrets.json（仅当前用户可读）。
密钥不进仓库、皮肤包、日志，接口返回时只给掩码。"""
import json, os, threading

DIR = os.path.join(os.path.expanduser("~"), ".cyberclinic")
SETTINGS, SECRETS = os.path.join(DIR, "agent.json"), os.path.join(DIR, "secrets.json")

# 提供方预设：均为 OpenAI 兼容 Chat Completions 接口。价格为每百万 Token 美元（入未命中缓存 / 入命中缓存 / 出），
# 取高峰价做预算上限（保守）；来源：https://api-docs.deepseek.com/quick_start/pricing（2026-11-12 核对）
PRESETS = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "thinking_param": True,
                 "models": ["deepseek-flash", "deepseek-v4-pro"],
                 "prices": {"deepseek-flash": [0.3, 0.006, 1.2], "deepseek-v4-pro": [1.32, 0.044, 3.96]}},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "models": [], "prices": {}},
    "custom": {"label": "自定义（OpenAI 兼容：通义、Kimi、智谱、本地 Ollama 等）", "base_url": "", "models": [], "prices": {}},
}

DEFAULTS = {
    "provider": "deepseek", "base_url": "",
    "model_chat": "deepseek-flash", "model_diag": "deepseek-flash", "model_deep": "deepseek-v4-pro",
    "thinking": False, "timeout_s": 8,
    "budget": {"calls_per_hour": 60, "daily_usd": 0.5},
    "persona": {"name": "", "self": "", "call_user": "", "tone": "", "length": "short"},
    "proactive": "alert",   # quiet 只在被叫时说话（仅严重告警弹气泡）/ alert 异常时主动提醒
    "privacy": "basic",     # basic 指标+进程名+异常证据 / logs 另含日志片段
    # 免确认自动修复（B 级）：默认全关，用户逐项打开；执行后通知，复发或过于频繁时自动暂停
    "autofix": {"agent_loop": False, "resume_stopped": False, "dev_port": False},
}
_lock, _cache = threading.Lock(), {"mtime": None, "data": None}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k in base:
            out[k] = _merge(base[k], v) if isinstance(base[k], dict) and isinstance(v, dict) else v
    return out


def load():
    try:
        m = os.path.getmtime(SETTINGS)
    except OSError:
        return json.loads(json.dumps(DEFAULTS))
    with _lock:
        if _cache["mtime"] != m:
            try:
                _cache["data"] = _merge(DEFAULTS, json.load(open(SETTINGS, encoding="utf-8")))
            except (OSError, ValueError):
                _cache["data"] = json.loads(json.dumps(DEFAULTS))
            _cache["mtime"] = m
        return json.loads(json.dumps(_cache["data"]))


def _write(path, obj, private=False):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def save(patch):
    """只接受已知字段；类型不对的值忽略。"""
    cur = load()
    for k, v in (patch or {}).items():
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        if isinstance(d, dict) and isinstance(v, dict):
            for kk, vv in v.items():
                if kk in d and isinstance(d[kk], bool):
                    cur[k][kk] = bool(vv)
                elif kk in d and (isinstance(vv, str) == isinstance(d[kk], str)) and not isinstance(vv, (dict, list)):
                    cur[k][kk] = vv.strip()[:120] if isinstance(vv, str) else max(0, vv)
        elif isinstance(d, bool):
            cur[k] = bool(v)
        elif isinstance(d, (int, float)) and isinstance(v, (int, float)):
            cur[k] = v
        elif isinstance(d, str) and isinstance(v, str):
            cur[k] = v.strip()[:200]
    for k, ok in (("provider", PRESETS), ("proactive", ("quiet", "alert")), ("privacy", ("basic", "logs"))):
        if cur[k] not in ok:
            cur[k] = DEFAULTS[k]
    if cur["persona"]["length"] not in ("short", "normal", "long"):
        cur["persona"]["length"] = "short"
    cur["timeout_s"] = max(3, min(30, cur["timeout_s"]))
    _write(SETTINGS, cur)
    return cur


def _secrets():
    try:
        return json.load(open(SECRETS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def key(provider):
    return _secrets().get(provider, "")


def set_key(provider, value):
    s = _secrets()
    if value:
        s[provider] = value.strip()
    else:
        s.pop(provider, None)
    _write(SECRETS, s, private=True)


def mask(k):
    return f"{k[:3]}…{k[-4:]}" if len(k) > 10 else ("已设置" if k else "")


def base_url(cfg):
    return (cfg.get("base_url") or PRESETS[cfg["provider"]]["base_url"]).rstrip("/")


def public(cfg=None):
    """给设置面板：设置 + 掩码后的密钥 + 预设。"""
    cfg = cfg or load()
    return {"settings": cfg, "key_masked": mask(key(cfg["provider"])),
            "presets": {k: {kk: v[kk] for kk in ("label", "base_url", "models")} for k, v in PRESETS.items()}}
