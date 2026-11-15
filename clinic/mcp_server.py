"""MCP stdio 适配器：让任意支持 MCP 的智能体框架(Claude Code/Codex/Cursor/DeepSeek 系 Agent 等)
挂载赛博诊所。纯标准库，按行分隔 JSON-RPC 2.0，转发到本地守护进程 HTTP API。"""
import json, sys, time

from .client import Clinic

TOOLS = [
    {"name": "clinic_status", "description": "查看本机健康状态、未结异常、守护进程自身资源占用",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "clinic_events", "description": "拉取最近的诊断事件（含病因与建议）",
     "inputSchema": {"type": "object", "properties": {"since": {"type": "integer"}, "limit": {"type": "integer"}}}},
    {"name": "clinic_report", "description": "智能体遇到报错/异常时上报给诊所，返回诊断事件（id、病因、可用修复 fixes）。"
     "context 里带 cwd（项目绝对路径）和 cmd（触发报错的命令）时，代码类报错会多出“交给 DSH / Codex 修复代码”的动作",
     "inputSchema": {"type": "object", "required": ["message"],
                     "properties": {"message": {"type": "string"}, "source": {"type": "string"},
                                    "context": {"type": "object"}}}},
    {"name": "clinic_fix_request", "description": "申请执行某个事件 fixes 里的修复动作。不会直接执行：小精灵会弹窗请用户确认，"
     "之后用 clinic_fix_result 查结果。用户已经要你处理时直接申请，不必反问",
     "inputSchema": {"type": "object", "required": ["event_id", "action"], "properties": {
         "event_id": {"type": "integer"}, "action": {"type": "string", "description": "fixes 里的 id"},
         "reason": {"type": "string", "description": "给用户看的一句话理由"}, "source": {"type": "string"}}}},
    {"name": "clinic_fix_result", "description": "查询修复闭环结果：用户是否同意、执行是否成功、DSH 改了哪些文件、事件是否已结案。"
     "wait_s（≤60）可等待用户确认或修复完成；返回 request.state=pending 或 repair.state=running 时可再次调用",
     "inputSchema": {"type": "object", "required": ["event_id"], "properties": {
         "event_id": {"type": "integer"}, "wait_s": {"type": "number"}}}},
    {"name": "clinic_diagnose", "description": "只诊断一段报错文本，不记录为事件",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}},
    {"name": "clinic_ask", "description": "以用户口吻向诊所提需求（如“电脑好卡帮我看看”“网络怎么了”），返回体检结论与建议",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}},
    {"name": "clinic_skin_spec", "description": "获取小精灵形象规格：可替换的图层名、调色板键、锚点、台词键与约束",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "clinic_skin_set", "description": "实时修改小精灵形象：pack=切换/导入皮肤包；palette=改色(十六进制)；layers=替换单层(完整SVG，同 viewBox)；lines=改台词；motions=改交互动作(原语组合，见 clinic_skin_spec)；reset=恢复默认",
     "inputSchema": {"type": "object", "properties": {
         "pack": {"type": "string"}, "palette": {"type": "object"}, "layers": {"type": "object"},
         "lines": {"type": "object"}, "motions": {"type": "object"}, "name": {"type": "string"},
         "reset": {"type": "boolean"}}}},
    {"name": "clinic_trust", "description": "把常驻/托管程序设为信任，不再对其指定检测项告警（kinds 省略=全部；remove=true 取消信任）",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string"}, "kinds": {"type": "array", "items": {"type": "string"}}, "remove": {"type": "boolean"}}}},
]


def handle(c, req):
    m, p = req.get("method"), req.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "cyber-clinic", "version": "0.1.0"}}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        a = p.get("arguments") or {}
        n = p.get("name")
        if n == "clinic_status":
            r = c.status()
        elif n == "clinic_events":
            r = c.events(a.get("since", 0), limit=a.get("limit", 20))
        elif n == "clinic_report":
            r = c.report(a["message"], source=a.get("source", "mcp-agent"), context=a.get("context"), t_onset=time.time())
        elif n == "clinic_ask":
            r = c.ask(a["text"])
        elif n == "clinic_skin_spec":
            r = c.skin(spec=True)
        elif n == "clinic_skin_set":
            r = c.set_skin(**a)
        elif n == "clinic_trust":
            r = c.trust(a["name"], a.get("kinds"), a.get("remove", False))
        elif n == "clinic_fix_request":
            r = c.fix_request(a["event_id"], a["action"], by=a.get("source", "mcp-agent"), reason=a.get("reason", ""))
        elif n == "clinic_fix_result":
            r = c.fix_result(a["event_id"], a.get("wait_s", 0))
        elif n == "clinic_diagnose":
            r = c.diagnose(a["text"])
        else:
            raise ValueError(f"unknown tool {n}")
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False, default=str)}]}
    if m == "ping":
        return {}
    raise ValueError(f"unknown method {m}")


def main():
    # MCP 协议要求 UTF-8；Windows 中文系统的管道默认 GBK，会让智能体解码中文失败
    for st in (sys.stdin, sys.stdout):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    c = Clinic()
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        if "id" not in req:  # 通知，无需回复
            continue
        try:
            resp = {"jsonrpc": "2.0", "id": req["id"], "result": handle(c, req)}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": str(e)}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
