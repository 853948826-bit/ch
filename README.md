# 赛博诊所 Cyber Clinic 社区版

免费自部署的桌面巡检工具。通过露米查看本机进程、资源占用和诊断记录，按需确认本地修复。你也可以配置自己的模型密钥，或通过 MCP 接入现有智能体。默认露米随软件提供。衣橱可同步万有智流门户签发的皮肤权益并解锁已安装的对应素材。付费及活动素材的安装或下载方式以该商品的发布说明为准。

## 在本机运行

需要 Python 3.9 或更新版本。桌宠依赖 Tauri 开发环境，先在 `desktop/src-tauri` 执行 `cargo tauri build`。macOS 本地构建应用包可用 `cargo tauri build --bundles app`。构建完毕后，从项目根目录运行。

```sh
python3 launch.py start
python3 launch.py status
python3 launch.py stop
```

Windows 可用 `python launch.py`，也可使用系统已安装的 `py -3`。社区版和 AI Defense 桌面正式版使用同一本机 API 端口及当前用户的账号数据，请先退出并停止另一版后台再启动。Linux 可用下方命令运行本机巡检进程，桌宠构建和各平台的签名安装包以发布说明为准。

## 通过 npm 部署到本机

npm 包只含本机巡检守护进程（诊断、修复申请、MCP 接口、默认露米素材），不含桌宠界面；需要桌宠请按上一节从源码构建。前提：Node.js 18+、Python 3.9+（Windows 需勾选“Add to PATH”）。

```sh
# 方式一：npm 官方源（发布后可用）
npm install -g cyber-clinic-community
cyber-clinic serve                      # 前台运行，Ctrl+C 停止

# 方式二：不安装，临时运行一次
npx cyber-clinic-community serve

# 方式三：直接从 GitHub 仓库安装（仓库公开后可用；可加 #版本标签 固定版本）
npm install -g github:853948826-bit/ch
```

启动前确认 8765 端口空闲（已运行桌面版时先退出）。安装目录可用 `npm root -g` 查到，下面接入示例里的“赛博诊所目录”填 `$(npm root -g)/cyber-clinic-community`。

## 接入智能体

守护进程运行后，智能体可通过 MCP 读取本机状态、上报报错、申请修复（修复必须由你在小精灵上确认）：

- 通用 MCP 客户端：[examples/mcp.json](examples/mcp.json)
- OpenAI Codex：[examples/codex.toml](examples/codex.toml)，追加到 `~/.codex/config.toml`
- DeepSeek Harness：[examples/dsh-bundle/](examples/dsh-bundle/README.md)，含报错 → 申请 → 确认 → 修复 → 复核的完整闭环说明

示例里的 `/path/to/cyber-clinic` 换成你的源码目录或上面的 npm 安装目录。

## 形象客制化

用自己的角色图一条命令生成皮肤包，详见 [docs/SKIN.md](docs/SKIN.md)；用图像模型批量出表情图的提示词见 [docs/SKIN_PROMPT.md](docs/SKIN_PROMPT.md)。抠图工具 `tools/skinkit.swift` 目前需要 macOS。自制皮肤放在 `skins/<名字>.local/`，只在本机使用。

## 许可与运营主体

代码适用 [Apache License 2.0](LICENSE)。露米形象和品牌素材适用[素材许可](skins/lumi/LICENSE)。公司及官网公示的备案信息见 [NOTICE](NOTICE)。安装包、账号与商业版服务入口以[万有智流官网](https://wanyouzhiliu.com/)为准。
