# 接入 DeepSeek Harness（DSH）

1. 把本目录复制到任意位置，修改 `cordis.patch.yml` 里的 `command`（Python 路径）和 `cwd`（赛博诊所目录）。
2. 安装到 DSH：
   - Web 版：`dsh plugin --profile web add link:/绝对路径/dsh-bundle`
   - 桌面版：`"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" plugin --profile desktop add link:/绝对路径/dsh-bundle`
3. 重启 DSH Web（桌面版会热加载）。插件页出现 `local-cyber-clinic-web-bundle` 即成功，模型可用 clinic_status、clinic_events、clinic_report、clinic_fix_request、clinic_fix_result 等 10 个工具。

需要赛博诊所守护进程在运行（127.0.0.1:8765）。`toolCallTimeoutMs` 保持 90000，`clinic_fix_result` 会等待最多 60 秒。

## 修复闭环（嵌套）

1. 智能体运行出错 → `clinic_report`，`context` 带 `cwd`（项目绝对路径）和 `cmd`（触发命令）→ 返回事件 id 和可用修复 `fixes`。
2. 智能体 `clinic_fix_request` 申请某个修复 → **小精灵弹窗请用户“允许 / 拒绝”**（MCP 不能直接执行任何修复）。
3. 允许后执行：进程类修复由诊所处理；代码类报错的 `dsh_repair` 由诊所调起本机 DSH 后台模式（`--profile headless`，workspace-write、不询问），只能改该项目目录，不提交 git。诊所前后给目录拍快照，核实实际改动的文件。
4. 智能体 `clinic_fix_result`（`wait_s` ≤ 60）拿到结果：`request.state`（pending/approved/rejected/done/failed）、`repair`（改动文件、DSH 总结、耗时）、事件是否结案，然后自己复跑确认。

`dsh_repair` 只在以下条件都满足时出现：DSH 桌面版已安装（macOS 默认路径；其他路径在 config.json 填 `"dsh": {"bin": …}`）、报错来自智能体上报且带 `cwd`、目录位于用户目录下（不含用户目录本身、隐藏目录和诊所自身）。DeepSeek 密钥用诊所设置里的那把，只经子进程环境传给 DSH。
卸载：`dsh plugin --profile web remove local-cyber-clinic-web-bundle`。
