"""B 级免确认自动修复 + 复发监测（P3）。
- 只在用户于设置里逐项打开后生效（agentcfg 的 autofix，默认全关）；动作只从现有处方白名单（fix.py）里取，
  执行前同样核对 pid/进程名/属主/保护名单。
- 立即类（挂起恢复、开发服务占端口）：不弹告警，处理完通知结果；延迟类（智能体死循环）：先照常告警，2 分钟没人处理再自动结束。
- 刹车：修复后 10 分钟内复发的标“复发”且不再自动处理；每小时最多 6 次、同一程序最多 3 次，超出即暂停、改回请用户确认。"""
import re, threading, time
from collections import defaultdict, deque

from . import agentcfg, fix, history, kb

RECUR_S, HOUR, LOOP_S = 600, 3600, 120
MAX_PER_HOUR, MAX_SAME_PER_HOUR = 6, 3
DEV = ("python", "node", "deno", "bun", "ruby", "java", "php", "dotnet", "uvicorn", "gunicorn", "go")


def _recent(q, now, span=HOUR):
    while q and now - q[0] > span:
        q.popleft()
    return q


class AutoFix:
    def __init__(self, clinic):
        self.c, self.lock = clinic, threading.Lock()
        self.fixed = {}                   # (kind, 对象) → 最近一次修复成功的时间
        self.recur = defaultdict(deque)   # (kind, 对象) → 复发时间
        self.done, self.by_name = deque(), defaultdict(deque)
        self.ports = defaultdict(deque)   # 端口 → 冲突时间

    def _who(self, ev):
        return self.c.subject(ev.get("evidence", {})) or ev.get("title", "")

    @staticmethod
    def _port(ev):
        m = re.search(r"(?:port|:)\s*(\d{2,5})\b", (ev.get("evidence") or {}).get("text", ""))
        return int(m.group(1)) if m else None

    def on_open(self, ev):
        """新异常发布前调用：标记复发、决定是否自动处理。返回规则 (动作, 延迟秒, 说明) 或 None。"""
        now, k = time.time(), (ev["kind"], self._who(ev))
        t = self.fixed.get(k)
        if t and now - t < RECUR_S:
            q = _recent(self.recur[k], now)
            q.append(now)
            ev["recur"] = {"after_s": round(now - t), "count": len(q)}
            ev["title"] = "复发：" + ev["title"]
            d = ev["diagnosis"]
            d["advice"] = [f"修复后 {max(1, round((now - t) / 60))} 分钟内又出现：治标没治本，建议查根因；"
                           "如果是正常的常驻程序，可以设为信任"] + list(d.get("advice", []))[:2]
        port = self._port(ev) if ev["kind"] == "log_port" else None
        if port:
            _recent(self.ports[port], now).append(now)
        rule = self.rule(ev, port)
        if rule:
            if rule[1]:
                ev["autofix_due"] = ev["t_detect"] + max(0, rule[1] - (ev["evidence"].get("sustained_s") or 0))
            else:
                ev["autofix"] = "pending"   # 界面不弹告警，等处理结果
        return rule

    def rule(self, ev, port=None):
        cfg = agentcfg.load().get("autofix", {})
        fixes = {f["id"]: f for f in ev.get("fixes", [])}
        k = ev["kind"]
        if ev.get("recur"):
            return None  # 复发的交还用户
        if k == "stopped" and cfg.get("resume_stopped") and "resume" in fixes:
            return "resume", 0, "进程被意外挂起，已自动恢复运行"
        if k == "cpu_hog" and cfg.get("agent_loop") and "terminate" in fixes and kb._agent_ctx(ev):
            return "terminate", LOOP_S, "智能体/脚本里的死循环持续超过 2 分钟，已自动结束"
        if k == "log_port" and cfg.get("dev_port") and "terminate" in fixes and port:
            name = (fixes["terminate"].get("name") or "").lower()
            if name.startswith(DEV) and len(self.ports[port]) >= 2:
                return "terminate", 0, f"端口 {port} 反复被旧的开发服务占用，已自动结束旧进程"
        return None

    def _blocked(self, name, now):
        if len(_recent(self.done, now)) >= MAX_PER_HOUR:
            return f"一小时内已自动处理 {MAX_PER_HOUR} 次，暂停自动修复，改为请你确认"
        if len(_recent(self.by_name[name], now)) >= MAX_SAME_PER_HOUR:
            return f"「{name}」一小时内已被自动处理 {MAX_SAME_PER_HOUR} 次，暂停对它自动修复，建议查根因"
        return None

    def run(self, ev, rule):
        """执行自动修复。失败或被刹车时撤销标记、重新发布，界面按普通告警请用户处理。"""
        act, _, why = rule
        with self.lock:
            if ev.get("state") != "open" or ev.get("autofix") == "done":
                return
            name, now = self._who(ev), time.time()
            stop = self._blocked(name, now)
            if stop:
                r = {"ok": False, "msg": stop}
            else:
                r = fix.apply(ev, act)
                if r["ok"]:
                    self.done.append(now)
                    self.by_name[name].append(now)
            ev.pop("autofix_due", None)
            if r["ok"]:
                ev["autofix"], ev["autofix_note"] = "done", f"{why}：{r['msg']}"
                history.record(ev, "autofix", note=r["msg"])
                self.note_fixed(ev)
                self.c.resolve(ev["key"], "autofix")
            else:
                ev["autofix"], ev["autofix_note"] = "skipped", r["msg"]
                if not stop:
                    history.record(ev, "fix_failed", note="自动修复：" + r["msg"])
                self.c._publish(ev)

    def note_fixed(self, ev):
        """手动或自动修复成功后记下，用于复发判定。"""
        self.fixed[(ev["kind"], self._who(ev))] = time.time()

    def tick(self):
        """巡逻每轮调用：到期的延迟类自动修复。"""
        now = time.time()
        for ev in list(self.c.active.values()):
            due = ev.get("autofix_due")
            if due and now >= due:
                ev.pop("autofix_due", None)  # 先取走，避免下一轮重复派发
                rule = self.rule(ev)  # 重新按当前设置判定（用户可能中途关掉）
                if rule:
                    threading.Thread(target=self.run, args=(ev, rule), daemon=True).start()
