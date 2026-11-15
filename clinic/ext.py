"""扩展接口（API v1）：给闭源/付费模块（如“AI 防护”订阅）预留的挂载点。契约见 docs/EXTENSION_API.md。

- 扩展是一个目录：extension.json（清单）+ Python 包（__init__.py 里的 setup(ctx)）+ extension.sig（签名）。
  搜索位置：<项目>/ext/、~/.cyberclinic/ext/。开源仓库不含任何付费扩展代码。
- 只加载用公司私钥签过名的扩展（config.json 的 ext.allow_unsigned 仅供开发调试）。
- 清单声明 requires（如 "defense"）的扩展，需要有效订阅（store.subscription）才加载；订阅失效后自动停用。
- 扩展只能通过 Context 与内核交互：收事件、报事件、加巡逻项、加只读工具、加需用户确认的处方、加 HTTP 接口。
  任何回调出错都被隔离，连续出错 5 次自动停用该扩展，内核与露米照常运行。"""
import collections, hashlib, importlib.util, json, os, sys, threading, time, traceback

from . import AI_DEFENSE_INCLUDED, agentcfg, ed25519, store

API_VERSION = 1
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = [os.path.join(ROOT, "ext"), os.path.join(os.path.expanduser("~"), ".cyberclinic", "ext")]
DATA = os.path.join(os.path.expanduser("~"), ".cyberclinic", "ext-data")
SIG_DOMAIN = b"CCEXT1\n"  # 与门户权益清单共用公钥，用前缀区分签名用途
MAX_ERRORS = 5


def digest(path):
    """扩展目录内容摘要：相对路径 + 文件 SHA-256，按路径排序；忽略签名文件与缓存。"""
    h = hashlib.sha256()
    for base, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
        for fn in sorted(files):
            if fn == "extension.sig" or fn.endswith(".pyc") or fn.startswith("."):
                continue
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, path).replace(os.sep, "/")
            h.update(rel.encode() + b"\0" + hashlib.sha256(open(p, "rb").read()).digest())
    return h.digest()


def verify(path):
    pk = store.pubkey()
    try:
        sig = bytes.fromhex(open(os.path.join(path, "extension.sig")).read().strip())
    except (OSError, ValueError):
        return False
    return bool(pk) and ed25519.verify(pk, SIG_DOMAIN + digest(path), sig)


class Context:
    """扩展拿到的唯一句柄（API v1）。只增不改：新增能力提升 API_VERSION，旧方法保持兼容。"""

    def __init__(self, host, ext):
        self._h, self._e = host, ext
        self.api_version = API_VERSION
        self.ext_id = ext["id"]
        self.data_dir = os.path.join(DATA, ext["id"])
        os.makedirs(self.data_dir, exist_ok=True)

    # ---- 观察 ----
    def on_event(self, fn):
        """fn(ev)：每个新异常与状态变化都会收到一份拷贝（只读）。"""
        self._e["listeners"].append(fn)

    def status(self):
        """系统指标 + 未结异常（拷贝）。"""
        c = self._h.c
        return json.loads(json.dumps({"metrics": c.metrics, "open": list(c.active.values())}, default=str))

    def privacy(self):
        """用户在设置面板选的隐私范围：basic / logs。扩展上传任何数据前必须遵守。"""
        return agentcfg.load()["privacy"]

    # ---- 检测 ----
    def emit(self, kind, key, severity, title, evidence, diagnosis=None):
        """报一个异常，走与内置检测相同的告警、气泡、复盘流程。kind/key 自动加扩展前缀。
        diagnosis 可选 {"cause", "advice": [..], "confidence"}，不给则显示 title。"""
        if severity not in ("info", "warn", "crit"):
            raise ValueError("severity 只能是 info / warn / crit")
        kind = f"ext.{self.ext_id}.{kind}"
        if diagnosis:
            self._h.diagnoses[kind] = diagnosis
        return self._h.c.emit(kind, f"{kind}:{key}", severity, title, dict(evidence, ext=self.ext_id),
                              source=f"ext:{self.ext_id}")

    def resolve(self, key, kind):
        self._h.c.resolve(f"ext.{self.ext_id}.{kind}:{key}")

    def add_detector(self, fn, interval_s=30):
        """周期巡逻项：fn() 在扩展线程里每 interval_s 秒调用一次（最短 5 秒），应在 2 秒内返回。"""
        self._e["detectors"].append({"fn": fn, "every": max(5, float(interval_s)), "next": 0})

    # ---- 智能体与处方 ----
    def add_tool(self, name, description, parameters, fn):
        """只读取证工具，供智能体对话调用：fn(args) -> 可 JSON 序列化的结果。v1 不开放会改动系统的工具。"""
        self._e["tools"][f"{self.ext_id}__{name}"] = {"name": f"{self.ext_id}__{name}", "description": description,
                                                      "parameters": parameters or {"type": "object", "properties": {}},
                                                      "fn": fn}

    def add_fix(self, kind, planner, applier):
        """为本扩展报出的某类异常提供处方：planner(ev) -> [{"id","label","detail"}]，applier(ev, action) -> {"ok","msg"}。
        处方与内置处方一样，必须由用户在气泡里确认后才会执行。"""
        self._e["fixes"][f"ext.{self.ext_id}.{kind}"] = (planner, applier)

    # ---- 接口与数据 ----
    def add_route(self, method, sub, fn):
        """HTTP 接口：/ext/<扩展ID>/<sub>，fn(body, query) -> dict。同样需要 X-Clinic-Token。"""
        self._e["routes"][(method.upper(), sub.strip("/"))] = fn

    def secret(self, name):
        return agentcfg.key(f"ext:{self.ext_id}:{name}")

    def set_secret(self, name, value):
        agentcfg.set_key(f"ext:{self.ext_id}:{name}", value)

    def subscription(self):
        req = self._e["man"].get("requires")
        return store.subscription(req) if req else {"active": True}

    def log(self, msg):
        print(f"[ext:{self.ext_id}] {msg}", file=sys.stderr, flush=True)


class Host:
    def __init__(self, clinic):
        self.c, self.exts, self.diagnoses, self.lock = clinic, {}, {}, threading.Lock()
        self.queue = collections.deque(maxlen=500)  # 事件异步分发给扩展，扩展再慢也不拖巡逻
        clinic.ext = self  # 扩展在 setup 里就可能报事件
        cfg = clinic.cfg.get("ext", {})
        self.allow_unsigned = bool(cfg.get("allow_unsigned"))
        self.require_sub = cfg.get("require_subscription", True) is not False
        self.load()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- 加载 ----
    def _scan(self):
        for d in DIRS:
            if os.path.isdir(d):
                for name in sorted(os.listdir(d)):
                    p = os.path.join(d, name)
                    if os.path.isfile(os.path.join(p, "extension.json")):
                        yield p

    def load(self):
        with self.lock:
            for e in self.exts.values():
                e["state"] = "unloaded"
            self.exts = {}
            for p in self._scan():
                self._load_one(p)

    def _load_one(self, path):
        try:
            man = json.load(open(os.path.join(path, "extension.json"), encoding="utf-8"))
        except (OSError, ValueError) as err:
            self.exts[os.path.basename(path)] = {"id": os.path.basename(path), "state": "error", "error": f"清单无法读取：{err}"}
            return
        eid = str(man.get("id") or os.path.basename(path))
        e = {"id": eid, "man": man, "path": path, "state": "error", "error": None, "errors": 0, "signed": verify(path),
             "listeners": [], "detectors": [], "tools": {}, "fixes": {}, "routes": {}}
        self.exts[eid] = e
        if not eid.replace("_", "").isalnum():
            e["error"] = "扩展 ID 只能用字母、数字、下划线"
        elif man.get("api") != API_VERSION:
            e["error"] = f"扩展接口版本 {man.get('api')} 与内核 {API_VERSION} 不匹配"
        elif not AI_DEFENSE_INCLUDED and (eid == "ai_defense" or man.get("requires")):
            e.update(state="locked", error="此扩展需要商业版")
        elif not e["signed"] and not self.allow_unsigned:
            e.update(state="unsigned", error="未签名或签名无效，已拒绝加载")
        elif man.get("requires") and self.require_sub and not store.subscription(man["requires"])["active"]:
            e.update(state="locked", error="需要有效订阅")
        else:
            try:
                spec = importlib.util.spec_from_file_location(f"cc_ext_{eid}", os.path.join(path, "__init__.py"),
                                                              submodule_search_locations=[path])
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                mod.setup(Context(self, e))
                e.update(state="running", error=None)
            except Exception:
                e["error"] = "加载失败：" + traceback.format_exc(limit=3)[-600:]

    # ---- 隔离执行 ----
    def _call(self, e, fn, *a):
        if e["state"] != "running":
            return None
        try:
            return fn(*a)
        except Exception:
            e["errors"] += 1
            e["error"] = traceback.format_exc(limit=3)[-600:]
            if e["errors"] >= MAX_ERRORS:
                e["state"] = "disabled"
            return None

    def _running(self):
        return [e for e in list(self.exts.values()) if e.get("state") == "running"]

    def _loop(self):
        last_sub = 0
        while True:
            now = time.time()
            if now - last_sub > 300:  # 每 5 分钟复核订阅，失效即停用
                last_sub = now
                for e in self._running():
                    req = e["man"].get("requires")
                    if req and self.require_sub and not store.subscription(req)["active"]:
                        e.update(state="locked", error="订阅已到期")
            while self.queue:
                snap = self.queue.popleft()
                for e in self._running():
                    for fn in e["listeners"]:
                        self._call(e, fn, snap)
            for e in self._running():
                for d in e["detectors"]:
                    if now >= d["next"]:
                        d["next"] = now + d["every"]
                        t0 = time.time()
                        self._call(e, d["fn"])
                        if time.time() - t0 > 2:
                            e["error"] = f"巡逻项耗时 {time.time() - t0:.1f}s，超过 2 秒"
            time.sleep(0.5)

    # ---- 内核调用的钩子 ----
    def on_event(self, ev):
        if any(e["listeners"] for e in self._running()):
            self.queue.append(json.loads(json.dumps(ev, default=str)))

    def diagnose(self, ev):
        return self.diagnoses.get(ev["kind"]) or {"cause": ev["title"], "advice": [], "confidence": 0.6}

    def plan(self, ev):
        for e in self._running():
            if ev["kind"] in e["fixes"]:
                out = self._call(e, e["fixes"][ev["kind"]][0], json.loads(json.dumps(ev, default=str))) or []
                return [dict(f, ext=e["id"]) for f in out if isinstance(f, dict) and f.get("id") and f.get("label")]
        return []

    def apply(self, ev, action):
        for e in self._running():
            if ev["kind"] in e["fixes"]:
                r = self._call(e, e["fixes"][ev["kind"]][1], json.loads(json.dumps(ev, default=str)), action)
                return r if isinstance(r, dict) and "ok" in r else {"ok": False, "msg": "扩展处方执行失败"}
        return None

    def tools(self):
        return [{k: v for k, v in t.items() if k != "fn"} for e in self._running() for t in e["tools"].values()]

    def call_tool(self, name, args):
        for e in self._running():
            if name in e["tools"]:
                r = self._call(e, e["tools"][name]["fn"], args or {})
                return r if r is not None else {"error": "扩展工具执行失败"}
        return None

    def route(self, method, path, body, query):
        """path 形如 /ext/<id>/<sub>；返回 (状态码, dict)。"""
        parts = path.strip("/").split("/", 2)
        e = self.exts.get(parts[1]) if len(parts) > 1 else None
        if not e:
            return 404, {"error": "没有这个扩展"}
        fn = e.get("routes", {}).get((method, parts[2] if len(parts) > 2 else ""))
        if e.get("state") != "running" or not fn:
            return 404, {"error": "扩展未运行或没有这个接口", "state": e.get("state")}
        r = self._call(e, fn, body, query)
        return (200, r) if isinstance(r, dict) else (500, {"error": "扩展接口执行失败"})

    def describe(self):
        out = []
        for e in self.exts.values():
            man = e.get("man", {})
            sub = store.subscription(man["requires"]) if man.get("requires") else None
            out.append({"id": e["id"], "name": man.get("name", e["id"]), "version": man.get("version"),
                        "requires": man.get("requires"), "state": e.get("state"), "signed": e.get("signed", False),
                        "error": e.get("error"), "subscription": sub,
                        "tools": sorted(e.get("tools", {})), "detectors": len(e.get("detectors", []))})
        return {"api": API_VERSION, "allow_unsigned": self.allow_unsigned, "extensions": out}
