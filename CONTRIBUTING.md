# 贡献指南

1. 新检测规则：`clinic/kb.py` 中 `log_rule(kind, regex, severity, cause, advice)`；越具体的规则放越前。
2. 新诊断函数：`@diagnoser("kind")`，返回 `{"cause", "advice", "confidence"}`。
3. 新模型服务商：`clinic/agentcfg.py` 的 `PRESETS` 加一项（OpenAI 兼容的 `/chat/completions` 接口，含 `base_url`、`models`、`prices`）。
4. 提交前运行 `python3 tests/inject.py --idle 30`，在 PR 中附检出率、延迟与空闲占用；不允许让空闲 CPU 明显上升。
5. 守护进程核心保持零第三方依赖；模型 SDK 只能作为可选依赖。
6. 不提交任何密钥、日志或 `run/`、`reports/` 下的本机数据。
