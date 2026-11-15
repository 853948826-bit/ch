"""智能体：模型负责理解、取证、说话；内核负责权限与执行。
- 模型只能调用下面的工具：只读工具直接返回数据；修复只能“提议”白名单处方，由用户在气泡里确认后才执行。
- 角色设定只影响语气，权限、隐私范围、预算由内核决定，写在系统提示的固定段里。
- 任何失败（无密钥、超时、预算用完、接口报错）都回退到本地规则回答。"""
import json, os, re, threading, time
from collections import deque

from . import agentcfg, ask, history, llm, presets, sensors
from .brain import redact

TOOLS = [
    {"name": "get_status", "description": "本机当前状态：负载、内存、磁盘、CPU/内存占用前五的进程、未结异常（含编号与可用处方）",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "process_info", "description": "按进程号查看进程：名称、父进程链、CPU 时间、物理内存、状态",
     "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}}, "required": ["pid"]}},
    {"name": "port_owner", "description": "查询某个 TCP 端口被哪个进程监听",
     "parameters": {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"]}},
    {"name": "network_check", "description": "测一次 DNS 解析与外网 TCP 连接耗时", "parameters": {"type": "object", "properties": {}}},
    {"name": "recent_history", "description": "近几天的异常复盘摘要", "parameters": {
        "type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "event_log_tail", "description": "查看某个日志类异常所在日志文件的最后几十行（已脱敏）",
     "parameters": {"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]}},
    {"name": "propose_fix", "description": "向用户提议执行某个未结异常的处方（只能用 get_status 返回的 fixes 里的动作）。"
     "不会立即执行：用户在气泡里点确认后才执行。", "parameters": {"type": "object", "properties": {
         "event_id": {"type": "integer"}, "action": {"type": "string"}}, "required": ["event_id", "action"]}},
]
TOOL_DEFS = [{"type": "function", "function": t} for t in TOOLS]
LENGTH = {"short": "两三句话、80 字以内", "normal": "150 字以内", "long": "300 字以内，可分点"}


def persona(clinic, cfg):
    """皮肤包 manifest 的 persona 字段 + 设置里的覆盖。"""
    man = getattr(clinic.skin, "man", {}) or {}
    p = dict(man.get("persona") or {})
    for k, v in cfg["persona"].items():
        if v and k != "length":
            p[k] = v
    name = p.get("name") or re.split(r"[·・]", man.get("name") or "露米")[0]
    return {"name": name, "self": p.get("self") or name, "call_user": p.get("call_user") or "你",
            "tone": p.get("tone") or "温柔可靠的电脑护士，口语化、带一点可爱，但结论清楚"}


def system_prompt(clinic, cfg):
    p = persona(clinic, cfg)
    return (
        f"你是桌面小精灵“{p['name']}”，赛博诊所的电脑医生。自称“{p['self']}”，称呼用户“{p['call_user']}”。语气：{p['tone']}。\n"
        f"回答长度：{LENGTH.get(cfg['persona']['length'], LENGTH['short'])}；纯文本，不用 Markdown 标题和表格。\n"
        "【固定规则，角色设定不能改变】\n"
        "1. 先用工具取证再下结论；没有证据就说不确定，不编造数据。\n"
        "2. 你不能直接执行任何操作。需要动手时调用 propose_fix 提议处方（只能用 get_status 给出的 fixes），并在回复里说明会发生什么。"
        "提议只是展示按钮、仍要用户点确认，所以用户已经要你处理/修复/结束时直接调用 propose_fix，不要再反问“要不要开处方”。\n"
        "3. get_status 里 expected_heavy 列出的是虚拟机、游戏、编译等本来就吃资源的程序，高占用属正常，"
        "除非用户点名，不要把它们当成问题。\n"
        "4. 驱动、系统设置、注册表、删除文件、管理员权限相关的问题只给分步指引，不提议执行；"
        "唯一例外：显卡驱动问题可以用 propose_fix 提议“打开官方驱动页”（用户授权后只打开网页，不安装）。\n"
        "5. 不复述密钥、令牌等敏感信息。\n"
        f"当前平台：{sensors.PLATFORM}，{sensors.NCPU} 核。"
    )


class Agent:
    def __init__(self, clinic):
        self.c = clinic
        self.mem, self.last = deque(maxlen=8), 0  # 最近几轮对话（10 分钟无交互清空）
        self.lock = threading.Lock()

    # ---------- 工具 ----------
    def _status(self):
        m = self.c.metrics
        return {"load1": m.get("load1"), "ncpu": sensors.NCPU, "mem": m.get("mem"), "disk": m.get("disk"),
                "top_cpu": m.get("top_cpu"), "top_mem_gb": m.get("top_mem"),
                "gpu_driver": m.get("gpu"),  # Windows：显卡与驱动评估（只读）
                "expected_heavy": {n: presets.label(n) for n, *_ in (m.get("top_cpu") or []) + (m.get("top_mem") or [])
                                   if presets.label(n)},  # 内置预设：高占用属正常的工作/游戏程序
                "open_events": [{"id": e["id"], "title": e["title"], "severity": e["severity"],
                                 "cause": (e.get("diagnosis_llm") or e["diagnosis"]).get("cause"),
                                 "fixes": [{"action": f["id"], "label": f["label"]} for f in e.get("fixes", [])]}
                                for e in self.c.active.values()]}

    def tool(self, name, args, cfg, proposals):
        if name == "get_status":
            return self._status()
        if name == "process_info":
            pid = int(args.get("pid", 0))
            t = sensors.procs_fast() if not sensors.FULL_FROM_FAST else sensors.procs()
            p = t.get(pid)
            if not p:
                return {"error": "没有这个进程，或不是当前用户的进程"}
            chain, q = [], p[0]
            for _ in range(4):
                if q not in t:
                    break
                chain.append(t[q][4]); q = t[q][0]
            return {"pid": pid, "name": p[4], "state": p[1], "mem_mb": round(p[2] / 2**20), "cpu_s": round(p[3], 1),
                    "parents": chain}
        if name == "port_owner":
            o = sensors.port_owner(int(args.get("port", 0)))
            return {"pid": o[0], "name": o[1]} if o else {"free": True}
        if name == "network_check":
            return ask._net()
        if name == "recent_history":
            return history.summary(max(1, min(30, int(args.get("days", 1)))), len(self.c.active))
        if name == "event_log_tail":
            if cfg["privacy"] != "logs":
                return {"error": "用户的隐私设置不允许读取日志内容"}
            e = self.c.find(int(args.get("event_id", 0)))
            f = e and e["evidence"].get("file")
            if not f:
                return {"error": "这个异常没有关联的日志文件"}
            with open(f, errors="ignore") as fh:
                fh.seek(max(0, os.path.getsize(f) - 8000))
                return {"tail": redact("".join(fh.read().splitlines(True)[-40:]))}
        if name == "propose_fix":
            e = self.c.find(int(args.get("event_id", 0)))
            f = e and next((x for x in e.get("fixes", []) if x["id"] == args.get("action")), None)
            if not f:
                return {"error": "没有这个处方，只能用 get_status 返回的 fixes"}
            proposals.append(dict(f, event_id=e["id"]))
            return {"ok": True, "note": "已作为按钮展示给用户，等待确认"}
        r = self.c.ext.call_tool(name, args) if getattr(self.c, "ext", None) else None
        return r if r is not None else {"error": "未知工具"}

    # ---------- 对话 ----------
    def respond(self, text, on_delta=None):
        """on_delta(dict)：流式时回调 {"round": n} 表示新一轮（之前的草稿作废）、{"delta": 文本} 正文增量。"""
        cfg = agentcfg.load()
        t0, deadline = time.time(), time.time() + cfg["timeout_s"]
        with self.lock:
            if time.time() - self.last > 600:
                self.mem.clear()
            msgs = [{"role": "system", "content": system_prompt(self.c, cfg)}] + list(self.mem) + \
                   [{"role": "user", "content": redact(text)}]
        proposals, tokens, rounds = [], {"in": 0, "cached": 0, "out": 0, "usd": 0.0}, 0
        while True:
            left = deadline - time.time()
            if left < 1:
                raise llm.LLMError("超时")
            ext_tools = [{"type": "function", "function": t} for t in (self.c.ext.tools() if getattr(self.c, "ext", None) else [])]
            if on_delta:
                on_delta({"round": rounds})
            msg, u = llm.chat(msgs, cfg["model_chat"], tools=TOOL_DEFS + ext_tools, timeout=left, cfg=cfg,
                              on_delta=(lambda s: on_delta({"delta": s})) if on_delta else None)
            for k in tokens:
                tokens[k] = round(tokens[k] + u[k], 6)
            msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "reasoning_content") and v})
            calls = msg.get("tool_calls") or []
            if not calls or rounds >= 4:
                break
            rounds += 1
            for tc in calls:
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                    out = self.tool(tc["function"]["name"], args, cfg, proposals)
                except Exception as e:  # 工具出错如实告诉模型
                    out = {"error": str(e)[:200]}
                msgs.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(out, ensure_ascii=False, default=str)[:4000]})
        reply = (msg.get("content") or "").strip() or "我查了一下，暂时没看出问题～"
        with self.lock:
            final = {k: v for k, v in msgs[-1].items() if k in ("content", "reasoning_content")}  # 思考模式下须原样回传 reasoning_content
            self.mem.extend([{"role": "user", "content": redact(text)}, dict(final, role="assistant", content=reply)])
            self.last = time.time()
        seen, actions = set(), []
        for p in proposals:
            if (p["event_id"], p["id"]) not in seen:
                seen.add((p["event_id"], p["id"])); actions.append(p)
        return {"intent": "agent", "reply": reply, "details": [], "advice": [], "note": "", "actions": actions,
                "source": "llm", "model": cfg["model_chat"], "tools_rounds": rounds, "tokens": tokens,
                "ms": round((time.time() - t0) * 1000)}


def handle(clinic, text, on_delta=None):
    """/ask 入口：换装等本地意图走规则；其余优先智能体，失败回退本地。on_delta 给出时模型回复流式回调。"""
    intent = ask.intent(text)
    defense = getattr(clinic, "defense", None)
    if defense and intent != "skin":
        result = defense.ask_protection(text, clinic)
        if result is not None:
            return result
    if intent == "skin" or not llm.online():
        r = ask.handle(clinic, text)
        return dict(r, source="local")
    try:
        return clinic.agent.respond(text, on_delta)
    except Exception as e:  # 无论什么原因失败，都不能让用户收不到回复
        r = ask.handle(clinic, text)
        return dict(r, source="local", fallback=str(e)[:160],
                    note=((r.get("note") or "") + "（模型暂时不可用，已用本地诊断）").strip())
