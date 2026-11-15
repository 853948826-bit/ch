"""赛博诊所守护进程：巡逻(采集→检测) → L0 诊断 → 推送；可选 L1/L2 模型升级诊断。
单线程事件循环 + kqueue：日志/崩溃目录是事件驱动（零轮询），进程表自适应采样。"""
import argparse, glob, html, json, os, re, secrets, select, sys, threading, time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import webbrowser


def open_url(url):
    """用系统默认浏览器打开网址（门户登录）。"""
    try:
        if sys.platform == "win32":
            os.startfile(url)
            return True
        return webbrowser.open(url)
    except (OSError, webbrowser.Error):
        return False


class ClinicServer(ThreadingHTTPServer):
    """独占端口：Windows 上 SO_REUSEADDR 允许多个进程同时监听同一端口，
    会让新版本守护进程启动“成功”却由旧进程接走请求。这里改为独占绑定，端口被占就明确失败。"""
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
from urllib.parse import parse_qs, urlparse

from . import AI_DEFENSE_INCLUDED, agent, agentcfg, autofix, coderepair, guardian, learn, portal, ext, gpu, presets, ask, autostart, fix, history, kb, llm, sensors, store
from .skin import Skin, list_packs as skin_list
from .brain import Brain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".cyberclinic", "token")


def ensure_token():
    """本机接口令牌：仅当前用户可读。挡住同机其他用户、浏览器网页（CSRF/DNS 重绑定）对本地接口的调用。"""
    try:
        t = open(TOKEN_PATH).read().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    t = secrets.token_hex(24)
    with open(TOKEN_PATH, "w") as f:
        f.write(t)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return t


def load_cfg(path):
    with open(path, encoding="utf-8") as f:  # 中文 Windows 默认 GBK，配置里有中文会读崩
        return json.load(f)


class Clinic:
    def __init__(self, cfg, skin_path=None):
        self.cfg, self.t_start = cfg, time.time()
        self.patrol_heartbeat = None
        self.guardian_pid = int(os.environ.get("AI_DEFENSE_GUARDIAN_PID", "0"))
        self.guardian_restarts = int(os.environ.get("AI_DEFENSE_RESTARTS", "0"))
        self.events, self.active, self.recent = [], {}, {}
        self.next_id = int(time.time()) * 1000  # 跨重启唯一，复盘时按 id 配对发现/结案
        self.seq, self.cond = 1, threading.Condition()  # 从 1 起：客户端 since=1 时会等待，避免空库时 since=0 被当作首次同步而空转
        self.brain = Brain(cfg, self._brain_update)
        self.metrics = {}
        self.skin = Skin(skin_path)
        self.agent = agent.Agent(self)
        self.autofix = autofix.AutoFix(self)
        self.token = ensure_token()
        history.prune()
        self.trust_path = os.path.join(ROOT, "run", "trust.json")
        self.trust = {}  # {程序名: [kind, ...] 或 ["*"]}，本机用户数据，不进仓库
        for enc in ("utf-8", None):  # 旧版本在中文 Windows 上按系统编码（GBK）写过，读不出就按系统编码再读一次
            try:
                self.trust = json.load(open(self.trust_path, encoding=enc))
                break
            except (OSError, ValueError):
                continue
        learn.load_rules()  # 用户采纳过的沉淀规则
        self.ext = None
        ext.Host(self)  # 付费/第三方扩展（docs/EXTENSION_API.md），出错不影响内核

    def guardian_status(self):
        age = None if self.patrol_heartbeat is None else round(time.monotonic() - self.patrol_heartbeat, 2)
        local_claude = getattr(getattr(self, "defense", None), "local_claude", None)
        return {"guardian_pid": self.guardian_pid, "patrol_age_s": age, "restarts": self.guardian_restarts,
                "claude_sample_in_progress_s": local_claude.sample_in_progress_age() if local_claude else None,
                "guardian_manager": os.environ.get("AI_DEFENSE_GUARDIAN_MANAGER", "local") if self.guardian_pid else None}

    # ---------- 事件总线 ----------
    def _publish(self, ev):
        with self.cond:
            self.seq += 1
            ev["seq"] = self.seq
            self.cond.notify_all()
        if self.ext:
            self.ext.on_event(ev)

    @staticmethod
    def subject(ev_or_evidence):
        e = ev_or_evidence.get("evidence", ev_or_evidence)
        return e.get("name") or e.get("parent") or e.get("app") or e.get("reporter")

    def trusted(self, kind, evidence):
        name = self.subject(evidence) or ""
        kinds = self.trust.get(name, [])
        return "*" in kinds or kind in kinds or presets.covers(name, kind)  # 用户信任 + 内置工作/游戏预设

    def set_trust(self, name, kinds=None, remove=False):
        if remove:
            self.trust.pop(name, None)
        else:
            self.trust[name] = sorted(set(self.trust.get(name, [])) | set(kinds or ["*"]))
        os.makedirs(os.path.dirname(self.trust_path), exist_ok=True)
        json.dump(self.trust, open(self.trust_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        for k, ev in list(self.active.items()):
            if self.trusted(ev["kind"], ev["evidence"]):
                self.resolve(k, "trust")
        return self.trust

    def emit(self, kind, key, severity, title, evidence, onset=None, source="patrol"):
        now = time.time()
        if self.trusted(kind, evidence):
            return None
        if key in self.active:
            self.active[key]["last_seen"] = now
            return None
        if now - self.recent.get(key, 0) < self.cfg["cooldown_s"]:
            return None
        ev = {"id": self.next_id, "kind": kind, "key": key, "severity": severity, "title": title,
              "evidence": evidence, "source": source, "state": "open", "t_detect": now, "t_onset": onset,
              "last_seen": now}
        ev["diagnosis"] = dict(self.ext.diagnose(ev) if kind.startswith("ext.") and self.ext else kb.diagnose(ev), tier="L0")
        ev["t_diag"] = time.time()
        ev["fixes"] = fix.plan(ev) + (self.ext.plan(ev) if self.ext else []) + coderepair.plan(ev)
        self.next_id += 1
        self.events.append(ev)
        if len(self.events) > 400:  # 常驻进程：只保留最近的事件，未结案的始终保留
            keep = set(id(e) for e in self.active.values())
            self.events = [e for e in self.events[:-300] if id(e) in keep] + self.events[-300:]
        rule = self.autofix.on_open(ev)  # 复发标记 / 是否免确认自动修复（默认关）
        self.active[key] = ev
        history.record(ev, "open")
        self.recent[key] = now
        self._publish(ev)
        self._maybe_escalate(ev)
        if rule and not rule[1]:
            threading.Thread(target=self.autofix.run, args=(ev, rule), daemon=True).start()
        return ev

    def resolve(self, key, how="auto"):
        """how：auto 自动恢复 / fix 一键修复 / autofix 免确认自动修复 / dismiss 用户知悉 / expired 一次性事件到期 / trust 设为信任"""
        ev = self.active.pop(key, None)
        if ev:
            ev["state"], ev["t_resolved"], ev["resolved_by"] = "resolved", time.time(), how
            history.record(ev, "resolved")
            self._publish(ev)

    def on_entitlements(self):
        """订阅权益变化后：重新加载付费扩展、刷新皮肤（到期的付费款退回免费款）。"""
        if self.ext:
            self.ext.load()
        try:
            if self.skin.man.get("sku") and not store.entitled(self.skin.man["sku"]):
                self.apply_skin({"reset": True})
                return
            self.apply_skin({"pack": self.skin.pack_id})
        except PermissionError:
            self.apply_skin({"reset": True})
        except (OSError, ValueError):
            pass

    def find(self, eid):
        return next((e for e in self.active.values() if e["id"] == eid), None)

    def find_any(self, eid):
        return self.find(eid) or next((e for e in reversed(self.events) if e["id"] == eid), None)

    def request_fix(self, eid, action, by, reason=""):
        """智能体申请修复：只登记申请并让小精灵弹出确认，执行仍由用户点击决定。"""
        ev = self.find(eid)
        if not ev:
            return {"ok": False, "msg": "这个异常已经结束了"}
        f = next((x for x in ev.get("fixes", []) if x["id"] == action), None)
        if not f:
            return {"ok": False, "msg": "没有这个修复动作；只能用事件 fixes 里的动作", "fixes": ev.get("fixes", [])}
        ev["request"] = {"action": action, "label": f["label"], "by": str(by)[:40], "reason": str(reason)[:200],
                         "t": time.time(), "state": "pending"}
        self._publish(ev)
        return {"ok": True, "msg": "已请用户在小精灵上确认", "state": "pending"}

    def reject_fix(self, eid):
        ev = self.find(eid)
        if ev and (ev.get("request") or {}).get("state") == "pending":
            ev["request"].update(state="rejected", t_done=time.time())
            self._publish(ev)
        return {"ok": bool(ev)}

    def fix_result(self, eid, wait=0):
        """闭环查询：申请/修复的结果（可长轮询等待用户确认或 DSH 修完）。"""
        end = time.time() + min(max(float(wait), 0), 60)
        while True:
            ev = self.find_any(eid)
            if not ev:
                return {"ok": False, "msg": "没有这个事件"}
            req, rep_ = ev.get("request") or {}, ev.get("repair") or {}
            busy = req.get("state") == "pending" or rep_.get("state") == "running"
            if not busy or ev["state"] != "open" or time.time() >= end:
                return {"event_id": eid, "state": ev["state"], "resolved_by": ev.get("resolved_by"),
                        "request": req or None, "repair": rep_ or None, "fixes": ev.get("fixes", []) if ev["state"] == "open" else []}
            with self.cond:
                self.cond.wait(min(2, max(end - time.time(), 0.05)))

    def _repair(self, ev, action):
        r = coderepair.run(ev, action)
        ev["repair"] = dict(r, state="done" if r["ok"] else "failed", t_done=time.time())
        how = coderepair.AGENTS[action][0]
        history.record(ev, how if r["ok"] else "fix_failed", note=r["msg"])
        if (ev.get("request") or {}).get("action") == action:
            ev["request"].update(state="done" if r["ok"] else "failed", t_done=time.time(), msg=r["msg"])
        if r["ok"]:
            self.resolve(ev["key"], how)
        else:
            self._publish(ev)

    def expire(self):
        """日志/崩溃/上报是一次性事件，不会自己“恢复”：一段时间没再出现就自动结案，避免告警常亮、菜单堆积。"""
        ttl, now = self.cfg.get("oneshot_ttl_s", 600), time.time()
        for k, ev in list(self.active.items()):
            if ev["source"] in ("log", "crash", "report", "sysevent") and now - ev["last_seen"] > ttl:
                self.resolve(k, "expired")

    def do_fix(self, eid, action):
        ev = self.find(eid)
        if not ev:
            return {"ok": False, "msg": "这个异常已经结束了"}
        if action in coderepair.AGENTS:  # 代码级修复耗时较长：后台执行，完成后通过事件流通知
            label = coderepair.AGENTS[action][1]
            if (ev.get("repair") or {}).get("state") == "running":
                return {"ok": True, "pending": True, "msg": f"{ev['repair'].get('agent', label)} 正在修复中"}
            if not any(f["id"] == action for f in coderepair.plan(ev)):
                return {"ok": False, "msg": f"{label} 不可用或这个报错没有项目目录"}
            ev["repair"] = {"state": "running", "t": time.time(), "agent": label}
            if (ev.get("request") or {}).get("state") == "pending":
                ev["request"]["state"] = "approved"
            self._publish(ev)
            threading.Thread(target=self._repair, args=(ev, action), daemon=True).start()
            return {"ok": True, "pending": True, "msg": f"已交给 {label} 修复，完成后告诉你改了什么"}
        r = (self.ext.apply(ev, action) if ev["kind"].startswith("ext.") and self.ext else None) or fix.apply(ev, action)
        if (ev.get("request") or {}).get("state") == "pending" and ev["request"]["action"] == action:
            ev["request"].update(state="done" if r["ok"] else "failed", t_done=time.time(), msg=r["msg"])
            if not r["ok"]:
                self._publish(ev)
        if action in fix.GUIDE_ACTIONS:  # 只是按授权打开指引（如官方驱动页），问题没解决：不算修复、不结案
            history.record(ev, "guide" if r["ok"] else "fix_failed", note=r["msg"])
            return dict(r, guide=True)
        history.record(ev, "fix" if r["ok"] else "fix_failed", note=r["msg"])
        if r["ok"]:
            self.autofix.note_fixed(ev)
            self.resolve(ev["key"], "fix")
        return r

    def apply_skin(self, req):
        """换装并唤醒长轮询，让小精灵立刻拿到新 skin_ver。"""
        ver = self.skin.apply(req)
        with self.cond:
            self.seq += 1
            self.cond.notify_all()
        return ver

    def _maybe_escalate(self, ev):
        """升级策略：L0 置信度<0.7 → L1；同时 ≥3 个未结异常 → L2 关联分析。"""
        opened = [e for e in self.active.values() if e is not ev]
        if len(opened) >= 2:
            tier = "L2"
        elif ev["diagnosis"]["confidence"] < 0.7:
            tier = "L1"
        else:
            return
        ev["llm_pending"] = self.brain.submit(ev, tier, opened)

    def _brain_update(self, ev, diag, error=None):
        ev["llm_pending"] = False
        if diag:
            ev["diagnosis_llm"], ev["t_llm"] = diag, time.time()
            try:
                learn.observe(ev)  # 规则沉淀：记录模型诊断过的泛化报错（脱敏）
            except Exception:
                pass
        else:
            ev["llm_error"] = error
        self._publish(ev)

    def wait(self, since, timeout):
        with self.cond:
            if since > self.seq:  # 客户端来自上一次运行的守护进程（序号已重置）：从头同步，免得漏掉重启后的新事件
                since = 0
            if since > 0 and self.seq <= since:  # 首次同步(since=0)立即返回，让刚启动的小精灵马上拿到皮肤
                self.cond.wait(timeout)
            return self.seq, [e for e in self.events if e["seq"] > since]

    def overhead(self):
        cpu, rss = sensors.self_usage()
        up = time.time() - self.t_start
        return {"uptime_s": round(up, 1), "cpu_s": round(cpu, 3), "avg_cpu_pct": round(cpu * 100 / max(up, 1), 3),
                "rss_mb": round(rss, 1), "platform": sensors.PLATFORM}


class Patrol:
    """巡逻员：快巡(负载/内存，系统调用) / 进程巡(ps，空闲3s、警戒1s) / 慢巡(磁盘/热) / kqueue(日志/崩溃)。"""

    def __init__(self, c: Clinic):
        self.c, self.cfg = c, c.cfg
        self.prev, self.hog_since, self.hog_low, self.rss_hist = {}, {}, {}, defaultdict(deque)
        self.zombie_since, self.stop_since, self.hung_since, self.over_n = {}, {}, {}, 0
        self.stale_cpu, self.next_stale = {}, 0
        self.alert, self.offsets, self.fds, self.dirs = False, {}, {}, set()
        # 文件监听：macOS 用 kqueue 事件驱动（零轮询）；Windows 等平台每秒检查一次大小/目录（开销极小）
        self.kq = select.kqueue() if hasattr(select, "kqueue") else None
        self.crash_dirs = [os.path.expanduser(d) for d in (self.cfg.get("crash_dirs") or sensors.CRASH_DIRS)]
        self.crash_seen = {d: self._listdir(d) for d in self.crash_dirs}
        self.sys_path = os.path.join(ROOT, "run", "sysevents.json")  # 系统事件日志已处理到的记录号（Windows）
        try:
            self.sys_state = json.load(open(self.sys_path, encoding="utf-8"))
        except (OSError, ValueError):
            self.sys_state = {}
        for d in self.crash_dirs:
            self._watch_dir(d)
        for pat in self.cfg["watch"]:
            d = os.path.dirname(os.path.join(ROOT, pat))
            os.makedirs(d, exist_ok=True)
            self._watch_dir(d)
        self._rescan_logs(initial=True)

    @staticmethod
    def _listdir(d):
        try:
            return set(os.listdir(d))
        except OSError:
            return set()

    # ---------- kqueue ----------
    def _kq_add(self, path):
        if self.kq is None or path in self.fds or not os.path.exists(path):
            return
        fd = os.open(path, os.O_RDONLY)
        self.fds[path] = fd
        flt = select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
        self.kq.control([select.kevent(fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR, flt)], 0)

    def _watch_dir(self, d):
        if os.path.isdir(d):
            self.dirs.add(d)
            self._kq_add(d)

    def _rescan_logs(self, initial=False):
        for pat in self.cfg["watch"]:
            for p in glob.glob(os.path.join(ROOT, pat)):
                if p not in self.offsets:
                    self.offsets[p] = os.path.getsize(p) if initial else 0
                    self._kq_add(p)

    def _read_log(self, p):
        try:
            size = os.path.getsize(p)
        except OSError:
            return
        off = self.offsets.get(p, 0)
        if size < off:
            off = 0  # 被截断/轮转
        if size == off:
            return
        with open(p, "r", errors="ignore") as f:
            f.seek(max(off, size - 65536))
            chunk = f.read()
        self.offsets[p] = size
        now = time.time()
        for block in chunk.split("\n\n"):
            hit = kb.classify(block)
            if hit:
                kind, sev, cause, _, m = hit
                tail = block.strip().splitlines()[-6:]
                self.c.emit(kind, f"{kind}:{os.path.basename(p)}:{m[:40]}", sev, f"日志异常：{cause}",
                            {"file": p, "match": m, "text": "\n".join(tail)[-1500:]}, source="log")

    def _check_crash(self, d=None):
        for cd in ([d] if d else self.crash_dirs):
            now = self._listdir(cd)
            for name in sorted(now - self.crash_seen.get(cd, set())):
                if sensors.is_crash_entry(name):
                    path = os.path.join(cd, name)
                    app, bug = sensors.crash_meta(path)
                    if not app:
                        continue
                    self.c.emit("crash", f"crash:{name}", "crit", f"程序崩溃：{app}",
                                {"app": app, "bug_type": bug, "path": path}, source="crash")
            self.crash_seen[cd] = now

    def _poll_files(self):
        """无 kqueue 的平台：检查崩溃目录新条目、日志目录新文件、日志文件增长。"""
        self._check_crash()
        self._rescan_logs()
        for p in list(self.offsets):
            try:
                if os.path.getsize(p) != self.offsets[p]:
                    self._read_log(p)
            except OSError:
                self.offsets.pop(p, None)

    # ---------- 采样检测 ----------
    def fast(self):
        l1 = sensors.load()[0]
        m = sensors.memory()
        self.c.metrics.update(load1=round(l1, 2), mem=m)
        lim = sensors.NCPU * (sensors.LOAD_LIMIT_FACTOR or self.cfg["load_factor"])
        self.over_n = self.over_n + 1 if l1 > lim else 0
        if self.over_n >= 2:
            self.c.emit("overload", "overload", "warn", "系统负载过高",
                        {"load1": round(l1, 2), "ncpu": sensors.NCPU, "factor": self.cfg["load_factor"]})
        elif l1 < lim * 0.8:
            self.c.resolve("overload")
        if (m["pressure"] or 1) >= 2 and self.prev:  # 等首轮进程巡检完成，告警里才能列出主要占用者
            top = sorted(self.prev.items(), key=lambda kv: -kv[1][2])[:3] if self.prev else []
            self.c.emit("mem_pressure", "mem_pressure", "crit" if m["pressure"] >= 4 else "warn", "系统内存压力",
                        dict(m, top=[(v[4], round(v[2] / 2**30, 1)) for _, v in top]))
        else:
            self.c.resolve("mem_pressure")

    def slow(self):
        if getattr(getattr(self.c, "defense", None), "demo", False):
            threading.Thread(target=self.c.defense.status, args=(self.c,), daemon=True).start()
        rights = frozenset(store.owned())
        if rights != getattr(self, "skin_rights", rights):
            self.c.on_entitlements()
        self.skin_rights = rights
        if portal.due():  # 已登录门户：定期同步订阅权益（续费、到期、换套餐）
            def sync_entitlements():
                portal.refresh()
                self.c.on_entitlements()
            threading.Thread(target=sync_entitlements, daemon=True).start()
        d = sensors.disk()
        self.c.metrics["disk"] = d
        if d["free_pct"] < self.cfg["disk"]["warn_pct"] or d["free_gb"] < self.cfg["disk"]["warn_gb"]:
            self.c.emit("disk_low", "disk_low", "crit" if d["free_pct"] < 2 else "warn", "磁盘空间不足", d)
        else:
            self.c.resolve("disk_low")
        t = sensors.thermal()
        th = {k: v for k, v in t.items() if v and "Limit" not in k}
        lim = t.get("CPU_Speed_Limit", 100)
        if th or lim < 100:
            self.c.emit("thermal", "thermal", "warn", "设备过热降频", dict(th, CPU_Speed_Limit=lim))
        else:
            self.c.resolve("thermal")
        if hasattr(sensors, "listen_ports") and time.time() >= self.next_stale:
            self.next_stale = time.time() + self.cfg.get("stale", {}).get("check_s", 300)
            self._stale()
        if hasattr(sensors, "sys_events"):
            self._sys_events()
        if hasattr(sensors, "gpu_info"):
            self._gpu_check()

    def _gpu_assess(self, refresh=False, extra_resets=0, extra_bsod=0):
        """extra_*：正在发出的这次事件还没写进复盘记录，计数时补上。"""
        now = time.time()
        if refresh or now - getattr(self, "gpu_t", 0) > 600:
            try:
                self.gpus = sensors.gpu_info()
            except Exception:
                self.gpus = []
            self.gpu_t = now
        bsod = sum(1 for r in history.query(7, "bsod")["rows"]
                   if r["action"] == "open" and kb.bsod_is_gpu(r.get("title", "")))
        a = gpu.assess(self.gpus, gpu.recent_resets(self.c) + extra_resets, bsod + extra_bsod)
        self.c.metrics["gpu"] = a
        return a

    def _gpu_check(self):
        """只检测和评估；驱动过旧时每 7 天最多提醒一次，处置要用户授权。"""
        a = self._gpu_assess()
        if not a or a["verdict"] != "outdated":
            return
        last = self.sys_state.get("_gpu_notice", 0)
        if time.time() - last < 7 * 86400:
            return
        self.c.emit("gpu_driver", "gpu_driver", "info", f"显卡驱动较旧：{a['name']}", {"gpu": a}, source="sysevent")
        self.sys_state["_gpu_notice"] = time.time()
        self._save_sys_state()

    def _save_sys_state(self):
        try:
            with open(self.sys_path, "w", encoding="utf-8") as f:
                json.dump(self.sys_state, f)
        except OSError:
            pass

    def _sys_events(self):
        """显卡驱动重置 / 蓝屏 / 意外关机：开机后也能复盘上一次（首次只回看 24 小时）。"""
        try:
            evs, st = sensors.sys_events(self.sys_state)
        except Exception:
            return
        for e in evs:
            ev_t = e.get("time")
            if e["kind"] == "gpu_reset":
                cur = self.c.active.get("gpu_reset")
                if cur:  # 连续重置合并为一条，只累计次数
                    cur["evidence"]["count"] += 1
                    cur["evidence"]["last"] = ev_t
                    cur["last_seen"] = time.time()
                    if hasattr(sensors, "gpu_info"):
                        cur["evidence"]["gpu"] = self._gpu_assess()
                    cur["diagnosis"] = dict(kb.diagnose(cur), tier="L0")
                    cur["fixes"] = fix.plan(cur)
                    self.c._publish(cur)
                    continue
                self.c.emit("gpu_reset", "gpu_reset", "warn", "显卡驱动重置（黑屏后恢复）",
                            {"driver": e.get("driver"), "via": e["via"], "count": 1, "time": ev_t, "last": ev_t,
                             "gpu": self._gpu_assess(extra_resets=1) if hasattr(sensors, "gpu_info") else None},
                            onset=ev_t, source="sysevent")
            elif e["kind"] == "bsod":  # 蓝屏事件与 Kernel-Power 41 同一代码合并
                self.c.emit("bsod", f"bsod:{e['code']}", "crit", f"上次蓝屏（{e['code']}）",
                            {"code": e["code"], "param": e.get("param"), "dump": e.get("dump"), "via": e["via"],
                             "time": ev_t, "gpu": self._gpu_assess(extra_bsod=1) if hasattr(sensors, "gpu_info") and
                             kb.bsod_is_gpu(e["code"]) else None}, onset=ev_t, source="sysevent")
            elif e["kind"] == "unexpected_shutdown":
                self.c.emit("unexpected_shutdown", f"unexpected_shutdown:{e['rid']}", "crit", "上次意外关机/重启",
                            {"via": e["via"], "time": ev_t, "power_button": e.get("power_button")},
                            onset=ev_t, source="sysevent")
        if any(self.sys_state.get(k) != v for k, v in st.items()):
            self.sys_state.update(st)
            self._save_sys_state()

    STALE_RT = re.compile(r"^(python|node|deno|bun|ruby|java|php|dotnet|uvicorn|gunicorn|go)[0-9.]*$")

    def _stale(self):
        """残留旧进程（macOS）：开发运行时的进程，启动它的终端/工具已退出（被系统接管），长时间空闲，
        却仍占着端口或大量内存。launchd 托管的常驻服务、诊所自身不算。两次检查之间几乎没用 CPU 才判定为空闲。"""
        sc, now, me = self.cfg.get("stale", {}), time.time(), os.getpid()
        min_h, rss_mb = sc.get("min_hours", 2), sc.get("rss_mb", 300)
        table, cands = self.prev, {}
        for pid, (ppid, stat, rss, cpu, name) in table.items():
            if ppid == 1 and pid != me and not stat.startswith("Z") and self.STALE_RT.match(name.lower()):
                cands[pid] = (rss, cpu, name)
        prev_cpu, self.stale_cpu = self.stale_cpu, {p: v[1] for p, v in cands.items()}
        ports = sensors.listen_ports() if cands else {}
        managed = sensors.launchd_pids() if cands else set()
        live = set()
        for pid, (rss, cpu, name) in cands.items():
            t0 = sensors.start_time(pid)
            if pid in managed or not t0 or now - t0 < min_h * 3600 or pid not in prev_cpu:
                continue
            idle = cpu - prev_cpu[pid] < 3  # 一个检查周期（默认 5 分钟）内 CPU 不到 3 秒
            held = ports.get(pid) or rss >= rss_mb * 2**20
            if not (idle and held):
                continue
            k = f"stale_proc:{pid}"
            live.add(k)
            self.c.emit("stale_proc", k, "warn", f"残留旧进程：{name}",
                        {"pid": pid, "name": name, "age_h": round((now - t0) / 3600, 1), "ports": ports.get(pid, []),
                         "rss_mb": round(rss / 2**20)})
        for k in [k for k in self.c.active if k.startswith("stale_proc:") and k not in live]:
            self.c.resolve(k)

    def _ancestry(self, pid, table, n=4):
        out = []
        while pid in table and n:
            pid = table[pid][0]
            if pid in table:
                out.append(table[pid][4])
            n -= 1
        return out

    def procs(self):
        cfg, now = self.cfg, time.time()
        fresh = sensors.procs_fast()  # 当前用户进程：系统调用，~1ms
        local_claude = getattr(getattr(self.c, "defense", None), "local_claude", None)
        if local_claude:
            local_claude.tick(fresh)
        if now >= self.next_full:      # 系统/他人进程：低频 ps 补全（Windows 快路径已含全部进程）
            full = {} if sensors.FULL_FROM_FAST else sensors.procs()
            self.sys_table = {p: v for p, v in full.items() if p not in fresh}
            self.next_full, sys_fresh = now + cfg["intervals"]["ps_full"], True
        else:
            sys_fresh = False
        table = {**self.sys_table, **fresh}
        ign = set(cfg["ignore_procs"])
        hot, live_keys, me, tops, game = False, set(), os.getpid(), [], None
        zombies = defaultdict(int)
        for pid, (ppid, stat, rss, cpu, name) in table.items():
            if stat.startswith("Z"):
                zombies[ppid] += 1
            if pid == me or name in ign:
                continue
            if pid not in fresh and not sys_fresh:  # 本轮无新数据：保持已开事件
                live_keys.update(k for k in self.c.active if k.endswith(f":{pid}"))
                continue
            # CPU：两次采样的 CPU 时间差 / 墙钟差（精确，不依赖 ps 的衰减平均值）
            p = self.cpu_prev.get(pid)
            self.cpu_prev[pid] = (cpu, now, name)
            pct = (cpu - p[0]) * 100 / max(now - p[1], 0.1) if p and p[2] == name else 0
            t_from = p[1] if p else now
            tops.append((name, pid, round(pct)))
            if pct >= 15 and presets.category(name) == "game":  # 正在玩的游戏（含窗口模式）：供小精灵自动进入专注模式
                game = name
            busy = not presets.covers(name, "cpu_hog")  # 预设里的游戏/虚拟机等：高占用属正常，不触发加速巡逻
            if busy and pct > cfg["cpu"]["watch_pct"]:
                hot = True
            if busy and pct >= cfg["cpu"]["hog_pct"]:
                t0 = self.hog_since.setdefault(pid, t_from)
                self.hog_low.pop(pid, None)
                if now - t0 >= cfg["cpu"]["sustain_s"]:
                    k = f"cpu_hog:{pid}"
                    live_keys.add(k)
                    self.c.emit("cpu_hog", k, "warn", f"CPU 异常占用：{name}",
                                {"pid": pid, "name": name, "cpu_pct": round(pct), "sustained_s": round(now - t0),
                                 "ancestry": self._ancestry(pid, table)})
            elif f"cpu_hog:{pid}" in self.c.active and now - self.hog_low.setdefault(pid, now) < cfg["cpu"]["sustain_s"]:
                live_keys.add(f"cpu_hog:{pid}")  # 已告警的：持续低于阈值 sustain_s 才结案，避免负载波动导致闪断
            else:
                self.hog_since.pop(pid, None)
                self.hog_low.pop(pid, None)
            # 内存：绝对值 + 增长速率（泄漏）
            if rss > cfg["mem"]["track_min_mb"] * 2**20:
                h = self.rss_hist[pid]
                h.append((now, rss))
                while h and now - h[0][0] > cfg["mem"]["leak_window_s"]:
                    h.popleft()
                growth = rss - min(r for _, r in h)
                if growth > 200 * 2**20:
                    hot = True
                if growth > cfg["mem"]["leak_mb"] * 2**20:
                    k = f"mem_leak:{pid}"
                    live_keys.add(k)
                    self.c.emit("mem_leak", k, "warn", f"内存快速增长：{name}",
                                {"pid": pid, "name": name, "growth_mb": round(growth / 2**20),
                                 "window_s": round(now - h[0][0]), "rss_mb": round(rss / 2**20)})
                elif f"mem_leak:{pid}" in self.c.active:
                    # 增长停止后再观察 60 秒：仍在涨就保持告警，稳定了就结案（虚拟机、大模型加载等一次性申请内存的情况）
                    if now - self.c.active[f"mem_leak:{pid}"]["t_detect"] < 60 or growth > 100 * 2**20:
                        live_keys.add(f"mem_leak:{pid}")
                if rss > cfg["mem"]["rss_abs_gb"] * 2**30:
                    k = f"mem_hog:{pid}"
                    live_keys.add(k)
                    self.c.emit("mem_hog", k, "info", f"内存大户：{name}",
                                {"pid": pid, "name": name, "rss_gb": round(rss / 2**30, 1)})
            # 进程状态
            if stat.startswith("T"):
                t0 = self.stop_since.setdefault(pid, now)
                if now - t0 >= cfg["stopped_s"]:
                    k = f"stopped:{pid}"
                    live_keys.add(k)
                    self.c.emit("stopped", k, "warn", f"进程被挂起：{name}", {"pid": pid, "name": name})
            else:
                self.stop_since.pop(pid, None)
        for ppid, n in zombies.items():
            t0 = self.zombie_since.setdefault(ppid, now)
            if now - t0 >= cfg["zombie_s"]:
                k = f"zombie:{ppid}"
                live_keys.add(k)
                parent = table.get(ppid, (0, "", 0, 0, "?"))[4]
                self.c.emit("zombie", k, "warn", f"僵尸进程堆积：{parent}", {"ppid": ppid, "parent": parent, "count": n})
        for pid in sensors.hung_pids():  # 窗口未响应（Windows）
            if pid in table and table[pid][4] not in ign:
                t0 = self.hung_since.setdefault(pid, now)
                if now - t0 >= cfg.get("hung_s", 5):
                    k = f"hung:{pid}"
                    live_keys.add(k)
                    self.c.emit("hung", k, "warn", f"程序未响应：{table[pid][4]}",
                                {"pid": pid, "name": table[pid][4], "hung_s": round(now - t0)})
        for pid in [p for p in self.hung_since if f"hung:{p}" not in live_keys and now - self.hung_since[p] > 1 and p not in sensors.hung_pids()]:
            del self.hung_since[pid]
        for ppid in list(self.zombie_since):
            if ppid not in zombies:
                del self.zombie_since[ppid]
        for d in (self.rss_hist, self.cpu_prev, self.hog_since, self.hog_low, self.stop_since):
            for pid in [p for p in d if p not in table]:
                del d[pid]
        for k in [k for k in self.c.active if k.split(":")[0] in ("cpu_hog", "mem_leak", "mem_hog", "stopped", "zombie", "hung")]:
            if k not in live_keys:
                self.c.resolve(k)
        self.c.autofix.tick()
        self.prev, self.alert = table, hot or bool(self.hog_since)
        self.c.metrics.update(
            game=game, procs=len(table), top_cpu=sorted(tops, key=lambda t: -t[2])[:5],
            top_mem=[(v[4], p, round(v[2] / 2**30, 1)) for p, v in
                     sorted(table.items(), key=lambda kv: -kv[1][2])[:5]])

    def run(self):
        iv = self.cfg["intervals"]
        self.next_full, self.sys_table, self.cpu_prev = 0, {}, {}
        nxt = {"fast": 0, "procs": 0, "slow": 0}
        while True:
            now = time.monotonic()
            if now >= nxt["fast"]:
                self.fast(); nxt["fast"] = now + iv["fast"]
            if now >= nxt["procs"]:
                self.procs(); nxt["procs"] = now + (iv["ps_alert"] if self.alert else iv["ps_idle"])
            if now >= nxt["slow"]:
                self.slow(); self.c.expire(); nxt["slow"] = now + iv["slow"]
            self.c.patrol_heartbeat = time.monotonic()
            timeout = min(2.0, max(0.05, min(nxt.values()) - time.monotonic()))
            if self.kq is None:
                time.sleep(min(timeout, 1.0))
                self._poll_files()
                continue
            for kev in self.kq.control(None, 32, timeout):
                path = next((p for p, fd in self.fds.items() if fd == kev.ident), None)
                if path is None:
                    continue
                if kev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                    os.close(self.fds.pop(path)); self.offsets.pop(path, None)
                if path in self.dirs:
                    if path in self.crash_dirs:
                        self._check_crash(path)
                    else:
                        self._rescan_logs()
                        for p in list(self.offsets):
                            if os.path.dirname(p) == path:
                                self._read_log(p)
                elif path in self.offsets:
                    self._read_log(path)


def make_handler(c: Clinic):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _authed(self, path):
            # /oauth/callback 由浏览器回跳、带不了令牌：靠一次性 state + PKCE 防伪
            if path in ("/health", "/oauth/callback") or self.headers.get("X-Clinic-Token") == c.token:
                return True
            self._send({"error": "unauthorized: missing or bad X-Clinic-Token (see ~/.cyberclinic/token)"}, 401)
            return False

        def do_GET(self):
            u = urlparse(self.path)
            if not self._authed(u.path):
                return
            params = parse_qs(u.query, keep_blank_values=True)
            q = {k: v[0] for k, v in params.items()}
            if u.path == "/health":
                health = getattr(c, "guardian_status", lambda: {})()
                self._send({"ok": True, "pid": os.getpid(), **health})
            elif u.path == "/status":
                self._send({"metrics": c.metrics, "overhead": c.overhead(), "open": list(c.active.values()),
                            "brain": {"provider": c.brain.name, "online": c.brain.online, "note": c.brain.reason,
                                      "usage": c.brain.usage}})
            elif u.path == "/defense/status" and hasattr(c, "defense"):
                self._send(c.defense.status(c))
            elif u.path == "/defense/protection" and hasattr(c, "defense"):
                self._send(c.defense.protection_status(c))
            elif u.path == "/events":
                seq, evs = c.wait(int(q.get("since", 0)), min(float(q.get("wait", 0)), 30))
                self._send({"seq": seq, "skin_ver": c.skin.ver, "events": evs[-int(q.get("limit", 50)):]})
            elif u.path == "/skin":
                self._send(c.skin.describe() if q.get("spec") else c.skin.render())
            elif u.path == "/trust":
                self._send(c.trust)
            elif u.path == "/fix":  # 某个异常可执行的修复动作 ?id=
                ev = c.find(int(q.get("id", 0)))
                self._send({"fixes": ev.get("fixes", []) if ev else []})
            elif u.path == "/learn":  # 规则沉淀：待确认的建议 + 已采纳的规则
                self._send({"suggestions": learn.suggestions(c), "rules": learn.rules()})
            elif u.path == "/focus":  # 专注模式的被动识别信号：正在运行的游戏
                self._send({"game": c.metrics.get("game")})
            elif u.path == "/fix/result":  # 闭环查询 ?id=&wait=秒（≤60）
                self._send(c.fix_result(int(q.get("id", 0)), q.get("wait", 0)))
            elif u.path == "/history/weekly":  # 每周复盘（小精灵每周首次见面时口播）
                self._send(history.weekly(len(c.active)))
            elif u.path == "/history/summary":  # 复盘摘要（小精灵每天首次见面时口播）
                self._send(history.summary(int(q.get("days", 1)), len(c.active)))
            elif u.path == "/autostart":
                self._send({"enabled": autostart.enabled()})
            elif u.path == "/agent":  # 智能体设置（密钥只返回掩码）
                self._send(dict(agentcfg.public(), usage=llm.usage(), online=c.brain.online, note=c.brain.reason))
            elif u.path == "/history":  # 诊断维护日志
                self._send(history.query(int(q.get("days", 7)), q.get("kind")))
            elif u.path == "/wardrobe":  # 当前皮肤的模块化衣橱
                self._send(c.skin.wardrobe())
            elif u.path == "/preview":  # 悬停预览：?pack=皮肤包 或 ?item=衣物（不改变当前皮肤）
                try:
                    self._send({"svg": c.skin.preview(q.get("pack"), q.get("item"))})
                except (OSError, ValueError, KeyError) as e:
                    self._send({"error": str(e)}, 400)
            elif u.path in ("/skins", "/store"):  # 皮肤列表 / 商店（含锁定与价格）
                self._send({"current": c.skin.pack_id, "packs": skin_list(c.skin.pack_id), "store_ready": bool(store.pubkey()),
                            "account": portal.status()})
            elif u.path == "/account":  # 门户账号与订阅权益
                self._send(portal.status())
            elif u.path == "/oauth/callback":  # 门户登录回跳（浏览器打开），返回一个提示页
                ok, msg = portal.callback(q) if all(len(v) == 1 for v in params.values()) else (False, "门户授权回调校验失败")
                if ok:
                    c.on_entitlements()
                page = (f'<!doctype html><meta charset="utf-8"><title>AI Defense</title><body style="font:16px sans-serif;'
                        f'text-align:center;padding-top:20vh">{"✅" if ok else "⚠️"} {html.escape(msg)}<br><br>'
                        f'<small>可以关闭此页面，回到小精灵。</small></body>').encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif u.path == "/ext":  # 扩展状态（含订阅）
                self._send(c.ext.describe())
            elif u.path.startswith("/ext/"):
                self._send(*reversed(c.ext.route("GET", u.path, {}, q)))
            else:
                self._send({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authed(u.path):
                return
            if u.path.startswith("/defense/") and not hasattr(c, "defense"):
                self._send({"error": "not found"}, 404)
                return
            if u.path in ("/defense/wallet-quote", "/defense/wallet-activate"):
                if self.headers.get("Origin") is not None:
                    self._send({"ok": False, "error": "仅允许桌面客户端发起余额支付。"}, 403)
                    return
                length = self.headers.get("Content-Length", "")
                if (not length.isascii() or not length.isdecimal() or len(length) > 4 or int(length) > 512
                        or self.headers.get_content_type() != "application/json"):
                    self._send({"ok": False, "error": "支付请求格式无效。"}, 400)
                    return
            b = self._body()
            if u.path.startswith("/defense/") and hasattr(c, "defense"):
                try:
                    if not isinstance(b, dict):
                        raise ValueError("操作参数无效")
                    self._send(c.defense.action(u.path.removeprefix("/defense/"), b, c))
                except (OSError, ValueError):
                    self._send({"ok": False, "error": "无法执行防护操作，请检查服务连接和账户状态。"}, 400)
            elif u.path == "/report":  # 智能体/外部框架主动上报
                text = str(b.get("message", ""))[:4000]
                hit = kb.classify(text)
                kind, sev = (hit[0], hit[1]) if hit else ("log_unknown", "warn")
                ev = c.emit(kind, f"report:{b.get('source', '?')}:{hash(text) & 0xffffff}", sev,
                            f"{b.get('source', '智能体')} 上报：{(hit[2] if hit else text[:40])}",
                            {"text": text, "context": b.get("context"), "reporter": b.get("source")},
                            onset=b.get("t_onset"), source="report")
                self._send({"event": ev or c.active.get(f"report:{b.get('source', '?')}:{hash(text) & 0xffffff}")})
            elif u.path == "/ack":  # 前端(小精灵)收到并展示
                for e in c.events:
                    if e["id"] == b.get("id") and "t_ui" not in e:
                        e["t_ui"] = time.time()
                self._send({"ok": True})
            elif u.path == "/trust":  # 信任程序：{name, kinds?: [...], remove?: bool}
                if not b.get("name"):
                    self._send({"error": "name required"}, 400)
                else:
                    self._send(c.set_trust(str(b["name"])[:80], b.get("kinds"), bool(b.get("remove"))))
            elif u.path == "/ask" and b.get("stream"):  # 流式：逐行 JSON（{"round"}/{"delta"}，最后一行 {"result"}），连接结束即完成
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.end_headers()
                def emit(obj):
                    try:
                        self.wfile.write((json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode())
                        self.wfile.flush()
                    except OSError:
                        pass  # 小精灵中途关闭：继续算完，结果照常记入对话记忆
                emit({"result": agent.handle(c, str(b.get("text", ""))[:500], emit)})
                self.close_connection = True
            elif u.path == "/ask":  # 用户提需求：快捷键/语音/文字
                self._send(agent.handle(c, str(b.get("text", ""))[:500]))
            elif u.path == "/skin":  # 形象客制化：整包/单层/调色/台词，版本号驱动小精灵热更新
                try:
                    self._send({"ok": True, "ver": c.apply_skin(b)})
                except PermissionError as e:
                    self._send({"ok": False, "locked": True, "error": str(e)}, 402)
                except (ValueError, OSError) as e:
                    self._send({"ok": False, "error": str(e)}, 400)
            elif u.path == "/wardrobe":  # 穿上 {equip: 物品ID} / 脱下 {unequip: 槽位}
                try:
                    c.skin.wear(b.get("equip"), b.get("unequip"))
                    with c.cond:
                        c.seq += 1
                        c.cond.notify_all()
                    self._send(dict(c.skin.wardrobe(), ok=True))
                except PermissionError as e:
                    self._send({"ok": False, "locked": True, "error": str(e)}, 402)
                except (ValueError, OSError) as e:
                    self._send({"ok": False, "error": str(e)}, 400)
            elif u.path == "/learn/apply":  # 用户采纳一条建议 {id}
                self._send(learn.apply(c, str(b.get("id", ""))))
            elif u.path == "/learn/dismiss":  # 用户忽略一条建议 {id}（不再提）
                self._send(learn.dismiss(str(b.get("id", ""))))
            elif u.path == "/learn/remove":  # 撤销一条已采纳的规则 {id}
                self._send(learn.remove_rule(str(b.get("id", ""))))
            elif u.path == "/fix/request":  # 智能体申请修复 {id, action, by, reason}：只弹确认，不直接执行
                self._send(c.request_fix(int(b.get("id", 0)), str(b.get("action", "")), b.get("by") or "智能体", b.get("reason", "")))
            elif u.path == "/fix/reject":  # 用户拒绝智能体的修复申请 {id}
                self._send(c.reject_fix(int(b.get("id", 0))))
            elif u.path == "/fix":  # 一键修复 {id, action}：执行前须经用户确认（界面负责二次确认）
                self._send(c.do_fix(int(b.get("id", 0)), str(b.get("action", ""))))
            elif u.path == "/agent":  # 保存设置 {settings?: {...}, key?: 字符串, clear_key?: bool}
                cfg = agentcfg.save(b.get("settings") or {})
                if b.get("clear_key"):
                    agentcfg.set_key(cfg["provider"], "")
                elif b.get("key"):
                    agentcfg.set_key(cfg["provider"], str(b["key"])[:300])
                self._send(dict(agentcfg.public(cfg), usage=llm.usage(), online=c.brain.online, note=c.brain.reason))
            elif u.path == "/agent/test":  # 连接测试：一次极短的真实调用
                cfg, t0 = agentcfg.load(), time.time()
                try:
                    msg, used = llm.chat([{"role": "user", "content": "只回复两个字：在线"}], cfg["model_chat"],
                                         cfg=cfg, max_tokens=10)
                    self._send({"ok": True, "ms": round((time.time() - t0) * 1000), "model": cfg["model_chat"],
                                "reply": (msg.get("content") or "")[:20], "tokens": used})
                except llm.LLMError as e:
                    self._send({"ok": False, "error": str(e)[:300], "ms": round((time.time() - t0) * 1000)})
            elif u.path == "/autostart":  # 开机自启开关 {enabled: bool}，只作用于当前用户
                self._send({"enabled": autostart.set_enabled(bool(b.get("enabled")))})
            elif u.path == "/dismiss":  # 用户表示知道了 {id}
                ev = c.find(int(b.get("id", 0)))
                if ev:
                    c.resolve(ev["key"], "dismiss")
                self._send({"ok": bool(ev)})
            elif u.path == "/history/report":  # 生成 HTML 报告，返回本机路径
                self._send({"path": history.report(int(b.get("days", 7)), len(c.active))})
            elif u.path == "/account/login":  # 打开门户授权页（浏览器），登录结果经 /oauth/callback 回到这里
                try:
                    url = portal.begin(c.cfg["port"])
                except ValueError as e:
                    self._send({"ok": False, "error": str(e), "configured": False})
                else:
                    if not b.get("no_open") and not open_url(url):
                        self._send({"ok": False, "error": portal.browser_unavailable()})
                    else:
                        self._send({"ok": True, "url": url})
            elif u.path == "/account/refresh":
                ok, msg = portal.refresh()
                c.on_entitlements()
                self._send({"ok": ok, "msg": msg, **portal.status()})
            elif u.path == "/account/manage":  # 在浏览器打开门户的订阅管理页（续费、换套餐、取消）
                st = portal.status()
                if st["manage_url"]:
                    open_url(st["manage_url"])
                self._send({"ok": bool(st["manage_url"])})
            elif u.path == "/account/logout":
                portal.logout()
                c.on_entitlements()
                self._send({"ok": True, **portal.status()})
            elif u.path == "/diagnose":  # 同步诊断一段报错文本，不入事件流
                text = str(b.get("text", ""))[:4000]
                hit = kb.classify(text)
                ev = {"kind": hit[0] if hit else "log_unknown", "evidence": {"text": text}}
                self._send({"kind": ev["kind"], "diagnosis": dict(kb.diagnose(ev), tier="L0")})
            elif u.path == "/ext/reload":  # 兑换订阅码或安装扩展后重新加载
                c.ext.load()
                self._send(c.ext.describe())
            elif u.path.startswith("/ext/"):
                self._send(*reversed(c.ext.route("POST", u.path, b, {})))
            else:
                self._send({"error": "not found"}, 404)
    return H


def main():
    global TOKEN_PATH
    ap = argparse.ArgumentParser(description="AI Defense daemon")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--defense-demo", action="store_true")
    a = ap.parse_args()
    if a.defense_demo and not AI_DEFENSE_INCLUDED:
        ap.error("AI Defense is available in the commercial edition")
    guardian.parent_watch()
    if sys.stdout is None:  # Windows pythonw 无控制台：日志写入 run/daemon.log
        os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(ROOT, "run", "daemon.log"), "a", encoding="utf-8", buffering=1)
    for _s in (sys.stdout, sys.stderr):  # Windows 中文系统重定向时默认 GBK
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg = load_cfg(a.config)
    portal.CONFIG_PATH = a.config
    demo_dir = os.path.join(ROOT, "run", "ai-defense-demo")
    if a.defense_demo:
        cfg["port"] = 18765
        cfg["watch"] = []
        cfg["ext"] = {}
        ext.DIRS = []
        history.DIR = os.path.join(demo_dir, "history")
        TOKEN_PATH = os.path.join(demo_dir, "token")
        agentcfg.DIR = demo_dir
        agentcfg.SETTINGS = os.path.join(demo_dir, "agent.json")
        agentcfg.SECRETS = os.path.join(demo_dir, "secrets.json")
    if AI_DEFENSE_INCLUDED:
        from .defense_desktop import DesktopDefense
        defense = DesktopDefense(a.defense_demo, demo_dir)
        c = Clinic(cfg, defense.skin_path)
        c.defense = defense
    else:
        c = Clinic(cfg)
    if a.defense_demo:
        c._maybe_escalate = lambda _event: None
        c.autofix.on_open = lambda _event: None
    srv = ClinicServer(("127.0.0.1", cfg["port"]), make_handler(c))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[clinic] listening 127.0.0.1:{cfg['port']} brain={c.brain.name} online={c.brain.online} {c.brain.reason}",
          flush=True)
    sensors.lower_priority()  # 降低调度优先级，让位给用户前台任务
    Patrol(c).run()


if __name__ == "__main__":
    main()
