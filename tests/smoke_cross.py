"""跨平台冒烟测试（Windows / macOS）：独立端口 + 独立日志目录启动一个守护进程，真实注入故障，测检出与延迟。
不干扰正在运行的小精灵。结果打印并保存到 reports/smoke-<平台>-<时间>.json。
用法：python tests/smoke_cross.py"""
import json, os, platform, socket, subprocess, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = 18765
os.environ["CLINIC_URL"] = f"http://127.0.0.1:{PORT}"
os.environ["CLINIC_WATCH_DIR"] = os.path.join(ROOT, "run", "smoke")
from clinic.client import Clinic  # noqa: E402

PY, WIN = sys.executable, sys.platform == "win32"
for _s in (sys.stdout, sys.stderr):  # Windows 中文系统重定向输出时默认 GBK，统一 UTF-8 防止打印符号崩溃
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
LOGDIR = os.path.join(ROOT, "run", "smoke")
WATCH = os.path.join(LOGDIR, "agent.log")
seen = []


def listener(c):
    since = 0
    while True:
        try:
            r = c.events(since, wait=25, limit=500)
            since = r["seq"]
            seen.extend(r["events"])
        except OSError:
            time.sleep(0.3)


def wait_kind(kinds, t0, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        for e in list(seen):
            if e["kind"] in kinds and e["t_detect"] >= t0 - 0.01:
                return e
        time.sleep(0.05)
    return None


def append(text):
    with open(WATCH, "a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def f_traceback():
    append('Traceback (most recent call last):\n  File "agent.py", line 1\nKeyError: \'tool_calls\'')
    return {"log_exception"}, None


def f_ratelimit():
    append("ERROR RateLimitError: Error code: 429 - rate_limit_error")
    return {"log_ratelimit"}, None


def f_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 18799))
    s.listen()
    append("Error: listen EADDRINUSE: address already in use 127.0.0.1:18799")
    return {"log_port"}, s.close


def f_mcp():
    p = subprocess.Popen([PY, "-m", "clinic.mcp_server"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, encoding="utf-8")
    for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "clinic_report", "arguments": {
                  "source": "smoke-agent", "message": "AuthenticationError: Error code: 401 - invalid api key"}}}):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
        p.stdout.readline()
    return {"log_auth"}, p.kill


def f_wrapped():
    subprocess.run([PY, "-m", "clinic.client", "--", PY, "-c", "import json; json.loads('{bad')"], cwd=ROOT,
                   capture_output=True)
    return {"log_exception"}, None


def f_cpu():
    p = subprocess.Popen([PY, "-c", "while True: pass"])
    return {"cpu_hog"}, p.kill


def f_leak():
    p = subprocess.Popen([PY, "-c", "import time\nb=[]\nfor _ in range(24):\n b.append(b'\x01'*(100*2**20)); time.sleep(0.25)\ntime.sleep(60)"])
    return {"mem_leak"}, p.kill


def f_hung():  # Windows：窗口不处理消息 → 系统判定“未响应”
    p = subprocess.Popen([PY, "-c", "import tkinter as tk,time\nr=tk.Tk();r.title('hung-test');r.update();time.sleep(60)"])
    return {"hung"}, p.kill


FAULTS = [("日志：Python 异常栈", f_traceback), ("日志：API 429 限流", f_ratelimit), ("日志：端口占用+定位", f_port),
          ("MCP 智能体上报 401", f_mcp), ("命令包装报错", f_wrapped), ("CPU 死循环", f_cpu), ("内存泄漏 2.4GB", f_leak)]
if WIN:
    FAULTS.append(("窗口未响应", f_hung))


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    open(WATCH, "a").close()
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    cfg.update(port=PORT, watch=["run/smoke/*.log"])
    cfgp = os.path.join(tempfile.gettempdir(), "cyberclinic_smoke.json")
    json.dump(cfg, open(cfgp, "w", encoding="utf-8"))
    d = subprocess.Popen([PY, "-m", "clinic.daemon", "--config", cfgp], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = Clinic()
    for _ in range(60):
        try:
            c.status()
            break
        except OSError:
            time.sleep(0.5)
    else:
        sys.exit("守护进程未能启动")
    threading.Thread(target=listener, args=(c,), daemon=True).start()
    time.sleep(3)
    rows = []
    for name, fn in FAULTS:
        t0 = time.time()
        kinds, cleanup = fn()
        ev = wait_kind(kinds, t0, 45 if "未响应" in name else 40)
        rows.append({"fault": name, "detected": bool(ev), "detect_ms": round((ev["t_detect"] - t0) * 1000) if ev else None,
                     "cause": ev["diagnosis"]["cause"] if ev else None})
        if not ev:  # 失败诊断：列出测试窗口内收到的事件
            rows[-1]["events_seen"] = [(e["kind"], e["title"]) for e in seen if e["t_detect"] >= t0][:8]
            print("   诊断：期间收到事件", rows[-1]["events_seen"], flush=True)
        if cleanup:
            cleanup()
        print(("✅" if ev else "❌"), name, f'{rows[-1]["detect_ms"]}ms' if ev else "未检出", "|", rows[-1]["cause"] or "", flush=True)
        time.sleep(1.5)
    st = c.status()
    report = {"platform": platform.platform(), "python": sys.version.split()[0], "cpu": os.cpu_count(),
              "detected": f"{sum(r['detected'] for r in rows)}/{len(rows)}", "faults": rows,
              "daemon": st["overhead"], "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    d.kill()
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out = os.path.join(ROOT, "reports", f"smoke-{'windows' if WIN else sys.platform}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n检出 {report['detected']}，守护进程平均 CPU {st['overhead']['avg_cpu_pct']}%，内存 {st['overhead']['rss_mb']}MB")
    print("报告已保存：", out)
    sys.exit(0 if all(r["detected"] for r in rows) else 1)


if __name__ == "__main__":
    main()
