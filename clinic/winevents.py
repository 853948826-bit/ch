"""Windows 系统事件分类（纯解析，跨平台可测）：显卡驱动重置、蓝屏、意外关机。
采集在 sensors_win.sys_events（wevtapi 直调）；这里只把事件 XML 归类成诊所事件。"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
GPU_LIVEKERNEL = {"117", "141"}  # VIDEO_TDR_TIMEOUT_DETECTED / VIDEO_ENGINE_TIMEOUT_DETECTED

# 每个通道的 XPath 条件（再拼上记录号/时间范围）
QUERIES = {
    "System": "(Provider[@Name='Display'] and EventID=4101)"
              " or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41)"
              " or (Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001)",
    "Application": "Provider[@Name='Windows Error Reporting'] and EventID=1001",
}


def xpath(channel, after_rid, lookback_ms=86400000):
    """after_rid 为 0 表示首次：只回看 lookback_ms；之后按记录号增量（仍限 7 天，防日志被清空后重放旧事件）。"""
    rng = f"EventRecordID > {after_rid} and TimeCreated[timediff(@SystemTime) <= {604800000 if after_rid else lookback_ms}]"
    return f"*[System[({QUERIES[channel]}) and {rng}]]"


def _epoch(s):
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", s or "")
    if not m:
        return None
    frac = float(m.group(2) or 0)
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() + frac


def _hex(code):
    return f"0x{code:X}"


def parse(xml):
    """事件 XML → (记录号, 诊所事件 dict 或 None)。"""
    root = ET.fromstring(xml)
    sys_ = root.find(NS + "System")
    prov = sys_.find(NS + "Provider").get("Name", "")
    eid = int(sys_.findtext(NS + "EventID") or 0)
    rid = int(sys_.findtext(NS + "EventRecordID") or 0)
    t = _epoch(sys_.find(NS + "TimeCreated").get("SystemTime"))
    data = [(d.get("Name"), (d.text or "").strip()) for d in root.iter(NS + "Data")]
    named = {k: v for k, v in data if k}
    vals = [v for _, v in data]
    base = {"rid": rid, "time": t, "via": f"{prov} {eid}"}

    if prov == "Display" and eid == 4101:
        drv = vals[0] if vals else ""
        m = re.search(r"driver (\S+) stopped", drv)  # 经典事件源会把整句描述写进第一个字段
        drv = m.group(1) if m else ("" if len(drv) > 40 or " " in drv else drv)
        return rid, dict(base, kind="gpu_reset", driver=drv)
    if prov == "Windows Error Reporting" and eid == 1001 and "LiveKernelEvent" in vals:
        i = vals.index("LiveKernelEvent")
        code = next((v for v in vals[i + 1:i + 7] if v.lower() in GPU_LIVEKERNEL), None)
        return rid, (dict(base, kind="gpu_reset", driver="", via=f"LiveKernelEvent {code}") if code else None)
    if prov == "Microsoft-Windows-WER-SystemErrorReporting" and eid == 1001:
        p1 = named.get("param1") or (vals[0] if vals else "")
        m = re.match(r"\s*0x([0-9a-fA-F]+)", p1)
        if not m:
            return rid, None
        return rid, dict(base, kind="bsod", code=_hex(int(m.group(1), 16)), param=p1[:200],
                         dump=named.get("param2") or (vals[1] if len(vals) > 1 else ""))
    if prov == "Microsoft-Windows-Kernel-Power" and eid == 41:
        try:
            code = int(named.get("BugcheckCode") or 0)
        except ValueError:
            code = 0
        if code:
            return rid, dict(base, kind="bsod", code=_hex(code), param="", dump="")
        return rid, dict(base, kind="unexpected_shutdown",
                         power_button=named.get("PowerButtonTimestamp", "0") not in ("0", ""))
    return rid, None
