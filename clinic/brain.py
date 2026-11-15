"""大脑层：L0 规则之外的模型升级诊断。提供方见设置面板（deepseek / openai / 自定义 OpenAI 兼容）。
只在 L0 置信度低或多异常需关联时调用，异步执行，不阻塞巡逻与首次告警。"""
import json, queue, re, threading, time

from . import agentcfg, llm

SYSTEM = ("你是“赛博诊所”的诊断医生，负责诊断用户电脑上智能体、终端、游戏运行时的异常。"
          "只依据给出的证据推断，不确定就说明。L0 是本地规则的初判，仅供参考：不要照抄，"
          "要补充它没说到的东西（更具体的原因、排查顺序）。concurrent 只是同时段其他异常的标题，"
          "没有证据表明同源时就当作无关，不要臆测关联，也不要建议处理它们。"
          "建议用口语短句，只针对本事件；需要动手时只能指向 fixes 里列出的修复动作。只输出一个 JSON 对象："
          '{"cause": "一句话病因", "advice": ["最多3条可执行建议"], "confidence": 0到1, "needs_repair": true或false}')

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|"
                     r"(?i:(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s'\",]{6,})")


def redact(text):
    """发给任何外部模型前脱敏：密钥、Bearer token、key=value 形式的凭据。"""
    return _SECRET.sub("[REDACTED]", text)


def build_prompt(ev, related=()):
    """紧凑上下文：事件证据 + 同时段其他未结事件标题。控制在 ~1-2k token。"""
    ctx = {"kind": ev["kind"], "title": ev["title"], "evidence": ev["evidence"], "L0": ev.get("diagnosis"),
           "fixes": [f.get("label") for f in ev.get("fixes") or []]}
    if related:
        ctx["concurrent"] = [r["title"] for r in related][:8]
    text = redact(json.dumps(ctx, ensure_ascii=False, default=str))
    return text[:6000]


def est_tokens(text):
    """粗估：中文≈1 token/字，ASCII≈1 token/3.5 字符。仅用于无密钥时的预算估算。"""
    cjk = sum(1 for c in text if ord(c) > 0x2E80)
    return int(cjk + (len(text) - cjk) / 3.5)


def _parse(text):
    try:
        s = text[text.index("{"): text.rindex("}") + 1]
        return json.loads(s)
    except Exception:
        return None


class Brain:
    """事件升级诊断：设置面板里选的提供方与诊断模型（clinic/agentcfg.py），异步执行，预算由 llm.budget_ok 把关。"""

    def __init__(self, cfg, on_update):
        self.on_update = on_update
        self.q = queue.Queue(maxsize=20)
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def name(self):
        return agentcfg.load()["provider"]

    @property
    def online(self):
        return llm.online()

    @property
    def reason(self):
        if not self.online:
            return "未配置 API 密钥，仅运行 L0 规则诊断"
        ok, why = llm.budget_ok(agentcfg.load())
        return "" if ok else why + "，暂时只运行 L0 规则诊断"

    @property
    def usage(self):
        return llm.usage()

    def submit(self, ev, tier, related=()):
        if not self.online or self.reason:
            return False
        try:
            self.q.put_nowait((ev, tier, build_prompt(ev, related)))
            return True
        except queue.Full:
            return False

    def _loop(self):
        while True:
            ev, tier, prompt = self.q.get()
            cfg, t0 = agentcfg.load(), time.time()
            model = cfg["model_deep"] if tier == "L2" else cfg["model_diag"]
            try:
                msg, u = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                  model, json_mode=True, cfg=cfg, timeout=max(cfg["timeout_s"], 20))
                d = _parse(msg.get("content") or "")
                if d and d.get("cause"):
                    d.update(tier=tier, model=model, llm_ms=int((time.time() - t0) * 1000), tokens=u)
                    self.on_update(ev, d)
                else:
                    self.on_update(ev, None, error="模型输出无法解析")
            except Exception as e:
                self.on_update(ev, None, error=str(e)[:200])
