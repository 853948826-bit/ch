"""显卡驱动评估（Windows）：只检测和评估，不改驱动。处置须用户授权，且目前只打开厂商官方驱动页。
以后接入 MCP 驱动方案后，再在授权前提下扩展。"""
import time
from datetime import date

from . import history

DRIVER_PAGES = {  # 白名单：只允许打开这几个官方地址
    "NVIDIA": "https://www.nvidia.cn/drivers/lookup/",
    "AMD": "https://www.amd.com/zh-cn/support/download/drivers.html",
    "Intel": "https://www.intel.cn/content/www/cn/zh/support/detect.html",
}
OLD_DAYS, FRESH_DAYS, UNSTABLE_N = 365, 14, 3


def pick(gpus):
    """优先独立显卡（NVIDIA/AMD），其次 Intel；虚拟/远程/基础显示适配器不评估（没有厂商驱动可更新）。"""
    order = {"NVIDIA": 0, "AMD": 1, "Intel": 2}
    real = [g for g in gpus or [] if g.get("vendor") in order]
    return min(real, key=lambda g: order[g["vendor"]]) if real else None


def recent_resets(clinic=None, days=7):
    """近 days 天掉驱动次数：复盘记录里的告警条数 + 当前合并告警里多出来的次数。"""
    n = sum(1 for r in history.query(days, "gpu_reset")["rows"] if r["action"] == "open")
    cur = clinic.active.get("gpu_reset") if clinic else None
    return n + (cur["evidence"].get("count", 1) - 1 if cur else 0)


def assess(gpus, resets_7d=0, gpu_bsod_7d=0, today=None):
    g = pick(gpus)
    if not g:
        return None
    today = today or date.today()
    try:
        age = (today - date.fromisoformat(g["date"])).days
    except (ValueError, TypeError):
        age = None
    issues = resets_7d + gpu_bsod_7d
    if age is not None and age <= FRESH_DAYS and issues:
        verdict, text = "rollback", f"驱动 {age} 天前刚更新，之后出现了 {issues} 次掉驱动/显卡蓝屏，可能是新驱动不稳定，建议回滚到上一版"
    elif issues >= UNSTABLE_N:
        verdict, text = "unstable", f"近 7 天掉驱动/显卡蓝屏 {issues} 次，建议更新到官方最新稳定版，并检查温度、超频和供电"
    elif age is not None and age > OLD_DAYS:
        verdict, text = "outdated", f"驱动已有 {age // 30} 个月没更新，建议到官网更新到最新稳定版"
    elif issues:
        verdict, text = "watch", f"近 7 天掉驱动 {issues} 次，先观察；再出现建议更新驱动"
    else:
        verdict, text = "ok", "驱动状态正常"
    return {"name": g.get("name"), "vendor": g.get("vendor"), "version": g.get("version"), "date": g.get("date"),
            "age_days": age, "resets_7d": resets_7d, "gpu_bsod_7d": gpu_bsod_7d, "verdict": verdict, "text": text,
            "page": DRIVER_PAGES.get(g.get("vendor"))}


def summary(a):
    if not a:
        return ""
    ver = f"{a['name']}，驱动 {a['version'] or '未知'}" + (f"（{a['date']}）" if a.get("date") else "")
    return f"{ver}。评估：{a['text']}"


ADVICE = {
    "rollback": "回滚驱动：设备管理器 → 显示适配器 → 右键显卡 → 属性 → 驱动程序 → 回退驱动程序",
    "outdated": "更新驱动：建议选厂商官网的“稳定版/WHQL”驱动，不用测试版",
    "unstable": "更新驱动前先降低画质、关闭超频，观察是否仍掉驱动",
}
