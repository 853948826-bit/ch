"""用户提需求（快捷键/语音/文字）→ 意图识别 → 即时体检 → 口语化回复。本地规则优先，0 Token。"""
import re, socket, time

from . import sensors

INTENTS = [  # (意图, 关键词)，越具体越靠前
    ("skin", ("换成", "换装", "换皮肤", "切换皮肤", "换回", "换上", "穿上", "换个形象")),
    ("network", ("网络", "网速", "断网", "上不了网", "连不上", "延迟", "ping")),
    ("disk", ("磁盘", "硬盘", "空间", "存储")),
    ("port", ("端口",)),
    ("status", ("状态", "怎么样", "还好吗", "健康")),
    ("slow", ("卡", "慢", "优化", "加速", "风扇", "发热", "烫", "掉帧", "流畅", "内存")),
]


def intent(text):
    for name, kws in INTENTS:
        if any(k in text for k in kws):
            return name
    return "checkup"


def _net():
    out = {}
    for key, fn in (("dns_ms", lambda: socket.getaddrinfo("www.apple.com", 443)),
                    ("tcp_ms", lambda: socket.create_connection(("1.1.1.1", 443), 2).close())):
        t = time.time()
        try:
            fn()
            out[key] = round((time.time() - t) * 1000)
        except OSError:
            out[key] = None
    return out


def handle(clinic, text):
    t0, text = time.time(), text or ""
    it, m = intent(text), clinic.metrics
    top_cpu, top_mem = m.get("top_cpu", []), m.get("top_mem", [])
    mem, disk = m.get("mem") or sensors.memory(), m.get("disk") or sensors.disk()
    opened = list(clinic.active.values())
    lines, advice = [], []

    if it == "skin":
        from .skin import list_packs
        packs = list_packs()
        norm = lambda x: re.sub(r"[\s·_.\-]|local", "", (x or "").lower())
        t = norm(text)

        def score(p):  # 说中的最长名称片段（全名、按 · 和空格拆开的各段、包名）
            parts = [p["name"], p["id"]] + re.split(r"[·\s]+", p["name"])
            return max((len(norm(x)) for x in parts if norm(x) and norm(x) in t), default=0)
        cands = [(score(p), -len(p["name"]), not p.get("sku"), p) for p in packs]
        best = max(cands, key=lambda c: (c[0], c[2], c[1]), default=None)
        hit = best[3] if best and best[0] >= 2 else None
        if not hit:
            names = "、".join(p["name"] + ("🔒" if p["locked"] else "") for p in packs)
            return {"intent": it, "reply": f"没找到这个形象，现有：{names}", "details": [], "advice": [], "note": "",
                    "ms": round((time.time() - t0) * 1000, 1)}
        try:
            clinic.apply_skin({"pack": hit["id"]})
            reply = f"好的，换成「{hit['name']}」啦～"
        except PermissionError as e:
            reply = str(e)
        return {"intent": it, "reply": reply, "details": [], "advice": [], "note": "", "skin": hit["id"],
                "ms": round((time.time() - t0) * 1000, 1)}
    if it == "network":
        n = _net()
        lines.append(f"DNS {n['dns_ms']}ms / TCP {n['tcp_ms']}ms")
        if n["tcp_ms"] is None:
            reply = "网络好像断了，外网连不上" + ("，域名解析也失败了" if n["dns_ms"] is None else "")
            advice = ["检查 Wi-Fi 或网线", "关闭或切换代理/VPN 再试"]
        else:
            reply = f"网络是通的，解析 {n['dns_ms']} 毫秒，连接 {n['tcp_ms']} 毫秒"
            if n["tcp_ms"] > 300:
                reply += "，延迟有点高"
                advice = ["关掉占带宽的下载和同步", "游戏时换有线或更近的节点"]
    elif it == "disk":
        reply = f"磁盘还剩 {disk['free_gb']}GB，占比 {disk['free_pct']}%"
        if disk["free_pct"] < 10:
            advice = ["清理下载目录、Xcode/Docker 缓存、旧模型权重"]
    elif it == "port":
        p = re.search(r"\d{2,5}", text)
        owner = sensors.port_owner(int(p.group())) if p else None
        reply = (f"端口 {p.group()} 被 {owner[1]} 占着，进程号 {owner[0]}" if owner else
                 f"端口 {p.group()} 现在是空闲的" if p else "告诉我端口号，我帮你查是谁占用的")
    else:  # slow / status / checkup：全面体检
        lines += ["CPU 前三：" + "、".join(f"{n} {p}%" for n, _, p in top_cpu[:3]),
                  "内存前三：" + "、".join(f"{n} {g}GB" for n, _, g in top_mem[:3]),
                  f"负载 {m.get('load1')} / {sensors.NCPU} 核，内存可用 {mem.get('free_pct')}%，磁盘余 {disk['free_gb']}GB"]
        problems = []
        hot = [t for t in top_cpu if t[2] >= 30]
        if hot:
            problems.append(f"{hot[0][0]} 正在占用 {hot[0][2]}% 的 CPU")
            advice.append(f"不需要的话结束 {hot[0][0]}（pid {hot[0][1]}）")
        if (mem.get("pressure") or 1) >= 2 or (mem.get("free_pct") or 100) < 15:
            problems.append(f"内存紧张，{top_mem[0][0]} 占了 {top_mem[0][2]}GB" if top_mem else "内存紧张")
            advice.append("关掉不用的大内存应用或浏览器标签")
        if opened:
            problems.append(f"还有 {len(opened)} 个异常没处理：{opened[0]['title']}")
            advice += opened[0]["diagnosis"].get("advice", [])[:1]
        if disk["free_pct"] < 10:
            problems.append("磁盘快满了")
        if problems:
            reply = "体检完成，发现" + "；".join(problems)
        else:
            reply = "一切正常，我会继续帮你盯着" if it == "status" else "体检完成，电脑很健康，没有发现异常哦"
    return {"intent": it, "reply": reply, "details": lines, "advice": advice[:3],
            "note": "右键菜单里可以一键修复" if any(e.get("fixes") for e in opened) else "", "ms": round((time.time() - t0) * 1000, 1)}
