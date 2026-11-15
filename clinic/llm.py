"""大模型调用（OpenAI 兼容 Chat Completions，纯标准库直连，不依赖第三方包）+ 用量记账与预算闸门。"""
import json, os, threading, time, urllib.error, urllib.request

from . import agentcfg

USAGE = os.path.join(agentcfg.DIR, "usage.json")
_lock = threading.Lock()


class LLMError(Exception):
    pass


def _usage():
    try:
        u = json.load(open(USAGE, encoding="utf-8"))
    except (OSError, ValueError):
        u = {}
    today = time.strftime("%Y-%m-%d")
    if u.get("day") != today:
        u = {"day": today, "calls": 0, "in": 0, "cached": 0, "out": 0, "usd": 0.0, "fail": 0, "hour": []}
    u["hour"] = [t for t in u.get("hour", []) if time.time() - t < 3600]
    return u


def usage():
    with _lock:
        u = _usage()
        return {k: v for k, v in u.items() if k != "hour"} | {"calls_last_hour": len(u["hour"])}


def budget_ok(cfg):
    """超过每小时次数或每日金额上限就不再调用模型（回退本地规则）。返回 (是否可用, 原因)。"""
    with _lock:
        u = _usage()
    b = cfg["budget"]
    if len(u["hour"]) >= b["calls_per_hour"]:
        return False, f"已达每小时 {b['calls_per_hour']} 次调用上限"
    if u["usd"] >= b["daily_usd"]:
        return False, f"已达今日 ${b['daily_usd']} 预算上限"
    return True, ""


def _record(cfg, model, usage_obj, ok=True):
    p = agentcfg.PRESETS[cfg["provider"]]["prices"].get(model)
    tin = usage_obj.get("prompt_tokens", 0) if usage_obj else 0
    hit = usage_obj.get("prompt_cache_hit_tokens", 0) if usage_obj else 0
    tout = usage_obj.get("completion_tokens", 0) if usage_obj else 0
    usd = ((tin - hit) * p[0] + hit * p[1] + tout * p[2]) / 1e6 if p else 0.0
    with _lock:
        u = _usage()
        u["hour"].append(time.time())
        u["calls"] += 1
        u["fail"] += 0 if ok else 1
        u["in"] += tin; u["cached"] += hit; u["out"] += tout; u["usd"] = round(u["usd"] + usd, 6)
        os.makedirs(agentcfg.DIR, exist_ok=True)
        json.dump(u, open(USAGE, "w", encoding="utf-8"))
    return {"in": tin, "cached": hit, "out": tout, "usd": round(usd, 6)}


def online(cfg=None):
    cfg = cfg or agentcfg.load()
    return bool(agentcfg.key(cfg["provider"])) and bool(agentcfg.base_url(cfg))


def _read_stream(r, on_delta):
    """读 SSE 流式响应，边收边回调正文增量；拼回与非流式相同的消息结构。"""
    msg, calls, usage_obj = {"role": "assistant", "content": "", "reasoning_content": ""}, {}, None
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        j = json.loads(data)
        usage_obj = j.get("usage") or usage_obj
        for ch in j.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                msg["content"] += d["content"]
                on_delta(d["content"])
            if d.get("reasoning_content"):
                msg["reasoning_content"] += d["reasoning_content"]
            for tc in d.get("tool_calls") or []:  # 工具调用按 index 分片到达，逐段拼接
                c = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                c["id"] = tc.get("id") or c["id"]
                f = tc.get("function") or {}
                c["function"]["name"] += f.get("name") or ""
                c["function"]["arguments"] += f.get("arguments") or ""
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return msg, usage_obj


def chat(messages, model, tools=None, json_mode=False, timeout=None, cfg=None, max_tokens=800, on_delta=None):
    """返回 (assistant 消息 dict, 用量)。失败抛 LLMError，调用方负责回退。on_delta 给出时流式调用，正文增量逐段回调。"""
    cfg = cfg or agentcfg.load()
    k = agentcfg.key(cfg["provider"])
    if not k:
        raise LLMError("未配置 API 密钥")
    ok, why = budget_ok(cfg)
    if not ok:
        raise LLMError(why)
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if on_delta:
        body["stream"], body["stream_options"] = True, {"include_usage": True}
    if agentcfg.PRESETS[cfg["provider"]].get("thinking_param"):
        body["thinking"] = {"type": "enabled" if cfg["thinking"] else "disabled"}
    req = urllib.request.Request(agentcfg.base_url(cfg) + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {k}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg["timeout_s"]) as r:
            if on_delta:
                msg, u = _read_stream(r, on_delta)
                return msg, _record(cfg, model, u)
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _record(cfg, model, None, ok=False)
        detail = e.read()[:300].decode("utf-8", "replace")
        raise LLMError({401: "API 密钥无效", 402: "账户余额不足", 429: "请求过于频繁或额度不足"}.get(e.code, f"HTTP {e.code}")
                       + f"：{detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        _record(cfg, model, None, ok=False)
        raise LLMError(f"连接失败或超时：{getattr(e, 'reason', e)}") from None
    msg = data["choices"][0]["message"]
    return msg, _record(cfg, model, data.get("usage"))
