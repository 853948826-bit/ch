"""Python SDK + `clinic-run` 包装器。
SDK:   from clinic.client import Clinic; Clinic().report("Traceback ...", source="my-gpt-agent")
包装:  python -m clinic.client -- <任意智能体命令>   # 零改造接入：输出原样透传，同时送诊所巡检"""
import json, os, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".cyberclinic", "token")


def headers():
    """本机接口令牌（守护进程首次启动时生成，仅当前用户可读）。"""
    h = {"Content-Type": "application/json"}
    try:
        h["X-Clinic-Token"] = open(TOKEN_PATH).read().strip()
    except OSError:
        pass
    return h


class Clinic:
    def __init__(self, url=None, timeout=35):
        url = url or os.environ.get("CLINIC_URL", "http://127.0.0.1:8765")
        self.url, self.timeout = url.rstrip("/"), timeout

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, headers=headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def status(self):
        return self._call("/status")

    def events(self, since=0, wait=0, limit=50):
        return self._call(f"/events?since={since}&wait={wait}&limit={limit}")

    def report(self, message, source="agent", context=None, t_onset=None):
        return self._call("/report", {"message": message, "source": source, "context": context, "t_onset": t_onset})

    def fix_request(self, eid, action, by="agent", reason=""):
        """申请修复（用户在小精灵上确认后才执行）"""
        return self._call("/fix/request", {"id": eid, "action": action, "by": by, "reason": reason})

    def fix_result(self, eid, wait=0):
        """修复闭环结果；wait 秒内等待用户确认或修复完成（≤60）"""
        w = min(float(wait or 0), 60)
        old, self.timeout = self.timeout, max(self.timeout, w + 10)
        try:
            return self._call(f"/fix/result?id={int(eid)}&wait={w}")
        finally:
            self.timeout = old

    def diagnose(self, text):
        return self._call("/diagnose", {"text": text})

    def ask(self, text):
        return self._call("/ask", {"text": text})

    def skin(self, spec=False):
        return self._call("/skin?spec=1" if spec else "/skin")

    def set_skin(self, **req):
        """set_skin(pack="my_skin") / set_skin(palette={"hair": "#ffb3d9"}) / set_skin(layers={"cap": "<svg ...>"})"""
        return self._call("/skin", req)

    def trust(self, name, kinds=None, remove=False):
        """信任某程序：trust("OrcaTerm", ["zombie"])；不传 kinds 表示全部检测项"""
        return self._call("/trust", {"name": name, "kinds": kinds, "remove": remove})

    def skins(self):
        return self._call("/skins")

    def account(self):
        """门户账号与订阅权益状态"""
        return self._call("/account")

    def login(self):
        """打开门户登录页（浏览器），返回授权地址"""
        return self._call("/account/login", {})

    def history(self, days=7):
        return self._call(f"/history?days={days}")

    def ack(self, eid):
        return self._call("/ack", {"id": eid})


def run_wrapped(cmd):
    """把子进程 stdout/stderr 透传给用户，同时追加写入巡检日志（kqueue 实时感知）。"""
    wd = os.environ.get("CLINIC_WATCH_DIR") or os.path.join(ROOT, "run", "watch")  # 需与守护进程 watch 配置一致
    os.makedirs(wd, exist_ok=True)
    log = os.path.join(wd, f"{os.path.basename(cmd[0])}-{os.getpid()}.log")
    with open(log, "a", buffering=1) as f:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in p.stdout:
            sys.stdout.write(line)
            f.write(line)
        rc = p.wait()
        if rc:
            f.write(f"[clinic-run] ERROR process exited with code {rc}\n\n")
    return rc


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        sys.exit("usage: python -m clinic.client -- <command ...>")
    sys.exit(run_wrapped(args))
