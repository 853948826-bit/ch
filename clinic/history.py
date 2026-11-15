"""诊断维护日志：每个异常的发现/恢复写入 run/history/YYYY-MM.jsonl（追加写，保留 6 个月），可汇总并生成 HTML 报告。"""
import glob, html, json, os, threading, time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "run", "history")
KEEP_MONTHS = 6
_lock = threading.Lock()


HOW = {"auto": "自动恢复", "fix": "一键修复", "autofix": "自动修复", "dsh": "DSH 修复", "codex": "Codex 修复", "dismiss": "已知悉", "expired": "到期结案", "trust": "设为信任"}


def record(ev, action, note=None):
    """action: open / resolved / fix / fix_failed。只记录诊断所需字段，不记录日志全文以外的隐私数据。"""
    d = ev.get("diagnosis_llm") or ev.get("diagnosis") or {}
    row = {"t": round(time.time(), 3), "action": action, "id": ev.get("id"), "kind": ev.get("kind"),
           "severity": ev.get("severity"), "title": ev.get("title"), "cause": d.get("cause"),
           "advice": d.get("advice"), "tier": d.get("tier"), "source": ev.get("source")}
    if action == "resolved":
        row["how"] = ev.get("resolved_by", "auto")
        if ev.get("t_detect"):
            row["duration_s"] = round(time.time() - ev["t_detect"], 1)
    if note:
        row["note"] = note
    os.makedirs(DIR, exist_ok=True)
    with _lock, open(os.path.join(DIR, time.strftime("%Y-%m") + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def prune():
    files = sorted(glob.glob(os.path.join(DIR, "*.jsonl")))
    for p in files[:-KEEP_MONTHS]:
        os.remove(p)


def query(days=7, kind=None, limit=500):
    since, rows = time.time() - days * 86400, []
    for p in sorted(glob.glob(os.path.join(DIR, "*.jsonl")))[-(days // 28 + 2):]:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["t"] >= since and (not kind or r["kind"] == kind):
                    rows.append(r)
    opened = [r for r in rows if r["action"] == "open"]
    durs = [r["duration_s"] for r in rows if r.get("duration_s") is not None]
    closed = {r["id"]: r for r in rows if r["action"] == "resolved"}
    fixes = [r for r in rows if r["action"] in ("fix", "fix_failed")]
    repeat = [(t, n) for t, n in Counter(r["title"] for r in opened).most_common(5) if n >= 3]
    return {"days": days, "total": len(opened), "by_kind": Counter(r["kind"] for r in opened).most_common(),
            "by_severity": dict(Counter(r["severity"] for r in opened)),
            "by_how": dict(Counter(HOW.get(r.get("how", "auto"), r.get("how")) for r in closed.values())),
            "open_now": len([r for r in opened if r["id"] not in closed]),
            "fix_ok": sum(r["action"] == "fix" for r in fixes), "fix_failed": sum(r["action"] == "fix_failed" for r in fixes),
            "autofix": sum(r["action"] == "autofix" for r in rows), "recur": sum(r["title"].startswith("复发：") for r in opened),
            "repeat": repeat,
            "avg_recover_s": round(sum(durs) / len(durs), 1) if durs else None, "rows": rows[-limit:]}


def _conclusions(q):
    """L0 复盘结论：从统计里提炼可执行的建议，0 Token。"""
    out = []
    if not q["total"]:
        return ["这段时间没有发现异常。"]
    out.append(f"共发现 {q['total']} 个异常（严重 {q['by_severity'].get('crit', 0)}，警告 {q['by_severity'].get('warn', 0)}），"
               + "，".join(f"{k} {v}" for k, v in q["by_how"].items()) + "。")
    if q["fix_ok"] or q["fix_failed"]:
        out.append(f"一键修复 {q['fix_ok'] + q['fix_failed']} 次，成功 {q['fix_ok']} 次。")
    if q.get("autofix"):
        out.append(f"自动修复 {q['autofix']} 次（你在设置里打开的免确认项）。")
    if q.get("recur"):
        out.append(f"修复后复发 {q['recur']} 次：这些问题治标没治本，建议查根因或设为信任。")
    for t, n in q["repeat"]:
        tip = "；如果是正常的常驻程序，可以设为信任" if "：" in t else ""
        out.append(f"「{t}」反复出现 {n} 次：这是常态问题，建议从根上处理{tip}。")
    if q["open_now"]:
        out.append(f"还有 {q['open_now']} 个异常未结案。")
    return out


def summary(days=1, open_now=None):
    q = query(days)
    if open_now is not None:  # 以守护进程当前状态为准（历史里上次关机时没结案的不算）
        q["open_now"] = open_now
    return {"days": days, "total": q["total"], "lines": _conclusions(q)}


def weekly(open_now=None):
    """每周复盘：本地统计结论 + （可用时）模型汇总，按自然周缓存，一周只调用一次模型。"""
    from . import agentcfg, llm
    week = time.strftime("%G-W%V")
    cache = os.path.join(DIR, "weekly.json")
    try:
        c = json.load(open(cache, encoding="utf-8"))
        if c.get("week") == week:
            return c
    except (OSError, ValueError):
        pass
    q = query(7)
    if open_now is not None:
        q["open_now"] = open_now
    out = {"week": week, "total": q["total"], "lines": _conclusions(q), "llm": None}
    cfg = agentcfg.load()
    if q["total"] and llm.online(cfg) and llm.budget_ok(cfg)[0]:
        stats = {k: q[k] for k in ("total", "by_kind", "by_severity", "by_how", "fix_ok", "fix_failed", "autofix", "recur", "repeat")}
        try:
            msg, u = llm.chat([
                {"role": "system", "content": "你是“赛博诊所”的电脑医生，根据一周的维护统计写复盘。只依据给出的数据，"
                 "用 3~4 条口语短句：哪些问题最常见、哪些反复出现说明没根治、下一步怎么根治或设为信任。纯文本，每条以“• ”开头。"},
                {"role": "user", "content": json.dumps(stats, ensure_ascii=False, default=str)}],
                cfg["model_chat"], cfg=cfg, max_tokens=300)
            out.update(llm=(msg.get("content") or "").strip()[:600], model=cfg["model_chat"], tokens=u)
        except Exception as e:  # 模型不可用时只给本地结论
            out["llm_error"] = str(e)[:120]
    os.makedirs(DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


ACT = {"open": "发现", "resolved": "结案", "fix": "修复成功", "autofix": "自动修复", "dsh": "DSH 修复代码", "codex": "Codex 修复代码", "fix_failed": "修复失败", "guide": "已授权指引"}


def report(days=7, open_now=None):
    """生成 run/history/report.html 并返回路径。"""
    q = query(days)
    if open_now is not None:
        q["open_now"] = open_now
    esc = html.escape
    kinds = "".join(f"<tr><td>{esc(str(k))}</td><td>{n}</td></tr>" for k, n in q["by_kind"])
    rows = "".join(
        f"<tr class='{esc(r.get('severity') or '')}'><td>{time.strftime('%m-%d %H:%M:%S', time.localtime(r['t']))}</td>"
        f"<td>{ACT.get(r['action'], r['action'])}{('·' + HOW.get(r['how'], r['how'])) if r.get('how') else ''}</td>"
        f"<td>{esc(r.get('title') or '')}{('<br><small>' + esc(r['note']) + '</small>') if r.get('note') else ''}</td>"
        f"<td>{esc(r.get('cause') or '')}</td><td>{esc('；'.join(r.get('advice') or []))}</td>"
        f"<td>{r.get('duration_s') if r.get('duration_s') is not None else ''}</td></tr>" for r in reversed(q["rows"]))
    page = f"""<!doctype html><meta charset="utf-8"><title>赛博诊所 · 诊断日志</title>
<style>body{{font:14px -apple-system,"Microsoft YaHei",sans-serif;margin:24px;color:#222}}h1{{font-size:20px}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f4f6f8}}tr.crit td:first-child{{border-left:4px solid #e5484d}}tr.warn td:first-child{{border-left:4px solid #f5a524}}
.k{{display:inline-block;margin-right:24px}}.k b{{font-size:22px}}</style>
<h1>赛博诊所 · 诊断维护日志（近 {days} 天）</h1>
<div><span class="k">异常 <b>{q['total']}</b> 次</span><span class="k">严重 <b>{q['by_severity'].get('crit', 0)}</b></span>
<span class="k">警告 <b>{q['by_severity'].get('warn', 0)}</b></span><span class="k">平均恢复 <b>{q['avg_recover_s'] or '-'}</b> 秒</span></div>
<h2>复盘结论</h2><ul>{"".join(f"<li>{esc(x)}</li>" for x in _conclusions(q))}</ul>
<h2>按类型</h2><table><tr><th>类型</th><th>次数</th></tr>{kinds}</table>
<h2>明细</h2><table><tr><th>时间</th><th>动作</th><th>异常</th><th>病因</th><th>建议</th><th>持续(秒)</th></tr>{rows}</table>"""
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path
