"""规则沉淀：把反复出现的问题变成本地规则，经用户确认后生效，可随时撤销。三类建议：
  rule    模型诊断过的新报错（本地规则只能泛泛识别的那类）反复出现 → 学成本地规则，下次毫秒识别、不再调用模型
  trust   同一程序同一提醒一周内反复出现、用户从没处理过 → 建议“以后不提醒”
  autofix 同一类问题一周内被用户手动修了好几次、对应的自动修复没开 → 建议打开
只在本机生效。观察样本先脱敏（密钥、用户目录、数字、引号内容），只保留报错的关键一行。"""
import hashlib, json, os, re, threading, time

from . import agentcfg, brain, fix, history, kb, presets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBS = os.path.join(ROOT, "run", "learn_obs.jsonl")       # 模型诊断过的泛化报错样本（已脱敏）
RULES = os.path.join(ROOT, "run", "learned_rules.json")  # 已采纳的本地规则
STATE = os.path.join(ROOT, "run", "learn_state.json")    # 用户忽略过的建议
GENERIC = ("log_unknown", "log_exception", "log_exit")
WATCH = ("cpu_hog", "mem_hog", "mem_leak", "hung", "stale_proc", "zombie")  # 可“以后不提醒”的程序类提醒
AUTOFIX = {"stopped": ("resume_stopped", "进程被挂起时自动恢复"), "log_port": ("dev_port", "开发服务反复占端口时自动结束旧进程")}
_lock = threading.Lock()
# 通用运行时/宿主：一个名字背后是很多不同程序，信任它会连带放过智能体死循环等真问题，不建议信任
GENERIC_HOSTS = re.compile(r"^(python|node|deno|bun|ruby|java|php|dotnet|uvicorn|gunicorn|go|electron|bash|zsh|sh|"
                           r"cmd|powershell|pwsh|conhost|wscript|cscript)[0-9.]*(\.exe)?$", re.I)
KIND_LABEL = {"cpu_hog": "CPU 占用高", "mem_hog": "内存占用大", "mem_leak": "内存快速增长", "hung": "程序未响应",
              "stale_proc": "残留旧进程", "zombie": "僵尸进程"}
_ERR_LINE = re.compile(r"\b([A-Za-z_][\w.]*(Error|Exception|Fault)|ERROR|FATAL|CRITICAL|panic)\b")


def _load(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _sid(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def signature(text):
    """报错关键行 → (脱敏样本, 归一化签名)。取最后一条像报错的行；数字、引号内容、路径都抹掉。"""
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    line = next((l for l in reversed(lines) if _ERR_LINE.search(l)), lines[-1] if lines else "")
    s = brain.redact(line)[:300]
    s = s.replace(os.path.expanduser("~"), "~")
    s = re.sub(r"(/[\w.@-]+){2,}|([A-Za-z]:\\[^\s'\"]+)", "<路径>", s)
    s = re.sub(r"(['\"]).*?\1", r"\1…\1", s)
    s = re.sub(r"0x[0-9a-fA-F]+|\d+", "N", s)
    return s[:200], re.sub(r"\s+", " ", s).strip()


def _pattern(norm):
    """签名 → 安全的字面量正则：取可变部分（数字、引号内容、路径、脱敏处）之间最长的稳定片段，
    只用 re.escape，不会有回溯爆炸。片段太短或没有实际文字时不生成规则。"""
    segs = [x.strip(" '\"(:=,[]-") for x in re.split(r"N|…|<路径>|\[REDACTED\]", norm)]
    seg = max(segs, key=len, default="")
    if len(seg) < 12 or not re.search(r"[A-Za-z\u4e00-\u9fff]{4}", seg):
        return None
    return re.escape(seg).replace(r"\ ", r"\s+")


def observe(ev):
    """模型补充诊断到达时调用：本地规则只能泛泛识别（或把握不大）的报错，记一条脱敏样本。"""
    d, l0 = ev.get("diagnosis_llm") or {}, ev.get("diagnosis") or {}
    text = (ev.get("evidence") or {}).get("text")
    if not d.get("cause") or not text or ev.get("source") not in ("log", "report"):
        return
    if ev.get("kind") not in GENERIC and l0.get("confidence", 1) >= 0.7:
        return
    sample, norm = signature(text)
    row = {"t": round(time.time()), "kind": ev.get("kind"), "severity": ev.get("severity"), "sample": sample, "norm": norm,
           "cause": str(d["cause"])[:200], "advice": [str(a)[:120] for a in (d.get("advice") or [])[:3]]}
    with _lock:
        os.makedirs(os.path.dirname(OBS), exist_ok=True)
        rows = open(OBS, encoding="utf-8").read().splitlines()[-499:] if os.path.exists(OBS) else []
        rows.append(json.dumps(row, ensure_ascii=False))
        with open(OBS + ".tmp", "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        os.replace(OBS + ".tmp", OBS)


def rules():
    return _load(RULES, [])


def load_rules():
    """把已采纳的规则装进本地知识库（排在泛化规则之前，命中后不再请模型）。"""
    kb.set_learned(rules())


def suggestions(clinic=None):
    state, have = _load(STATE, {"dismissed": []}), rules()
    skip = set(state["dismissed"]) | {r["id"] for r in have}
    out, since = [], time.time() - 30 * 86400
    # 1. 新报错学成本地规则：同一签名 30 天内模型诊断过至少 2 次，且现有规则还识别不出来
    groups = {}
    for line in (open(OBS, encoding="utf-8").read().splitlines() if os.path.exists(OBS) else []):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r["t"] >= since:
            groups.setdefault(r["norm"], []).append(r)
    for norm, rs in groups.items():
        pat = _pattern(norm)
        sid = _sid("rule", pat)
        if len(rs) < 2 or not pat or sid in skip or any(re.search(x["pattern"], rs[-1]["sample"], re.I) for x in have):
            continue
        last, seg = rs[-1], re.sub(r"\\s\+", " ", pat).replace("\\", "")
        out.append({"id": sid, "type": "rule", "count": len(rs),
                    "title": f"学会识别报错「…{seg[:60]}…」",
                    "detail": f"近 30 天模型诊断过 {len(rs)} 次，结论：{last['cause'].rstrip('。')}。采纳后本地直接识别，不再调用模型。",
                    "rule": {"pattern": pat, "cause": last["cause"], "advice": last["advice"] or ["查看报错上下文"],
                             "severity": "crit" if last.get("severity") == "crit" else "warn", "sample": last["sample"]}})
    # 2/3. 来自近 7 天维护日志
    rows = history.query(7, limit=5000)["rows"]
    fixed = {r["id"] for r in rows if r["action"] in ("fix", "autofix", "dsh", "codex")}
    cnt = {}
    for r in rows:
        if r["action"] == "open" and r.get("kind") in WATCH and "：" in (r.get("title") or ""):
            name = r["title"].split("：", 1)[1].strip()
            c = cnt.setdefault((r["kind"], name), [0, False])
            c[0] += 1
            c[1] = c[1] or r["id"] in fixed
    trusted = (clinic.trust if clinic else {}) or {}
    for (kind, name), (n, was_fixed) in cnt.items():
        sid = _sid("trust", kind, name)
        if name in fix.PROTECTED or presets.covers(name, kind) or GENERIC_HOSTS.match(name):  # 诊所自身、预设已覆盖、通用运行时不建议
            continue
        if n >= 3 and not was_fixed and sid not in skip and kind not in trusted.get(name, []) and "*" not in trusted.get(name, []):
            out.append({"id": sid, "type": "trust", "count": n, "name": name, "kind": kind,
                        "title": f"「{name}」以后不再提醒“{KIND_LABEL.get(kind, kind)}”",
                        "detail": f"近 7 天提醒了 {n} 次，你一次都没处理，可能是正常现象。采纳后不再对它提醒这一项（其他检测照常）。"})
    af = agentcfg.load().get("autofix", {})
    for kind, (key, label) in AUTOFIX.items():
        n = sum(1 for r in rows if r["action"] == "fix" and r.get("kind") == kind)
        sid = _sid("autofix", key)
        if n >= 2 and not af.get(key) and sid not in skip:
            out.append({"id": sid, "type": "autofix", "count": n, "key": key, "title": f"打开自动修复：{label}",
                        "detail": f"近 7 天你手动修了 {n} 次。采纳后这类问题自动处理、事后告诉你；复发或过于频繁会自动暂停。"})
    order = {"rule": 0, "autofix": 1, "trust": 2}
    return sorted(out, key=lambda x: (order[x["type"]], -x["count"]))


def apply(clinic, sid):
    s = next((x for x in suggestions(clinic) if x["id"] == sid), None)
    if not s:
        return {"ok": False, "msg": "这条建议已经不存在了"}
    if s["type"] == "rule":
        have = rules()
        have.append(dict(s["rule"], id=sid, t=round(time.time())))
        _save(RULES, have)
        load_rules()
        return {"ok": True, "msg": "学会了，以后遇到这个报错我直接告诉你原因"}
    if s["type"] == "trust":
        clinic.set_trust(s["name"], [s["kind"]])
        return {"ok": True, "msg": f"好的，以后不再提醒「{s['name']}」这一项"}
    agentcfg.save({"autofix": {s["key"]: True}})
    return {"ok": True, "msg": "已打开，处理完我会告诉你；可以在 设置 → 自动修复 里关掉"}


def dismiss(sid):
    st = _load(STATE, {"dismissed": []})
    if sid not in st["dismissed"]:
        st["dismissed"].append(sid)
        _save(STATE, st)
    return {"ok": True}


def remove_rule(rid):
    have = rules()
    keep = [r for r in have if r["id"] != rid]
    _save(RULES, keep)
    load_rules()
    dismiss(rid)  # 撤销后不再把同一条当作新建议提出来
    return {"ok": len(keep) < len(have)}
