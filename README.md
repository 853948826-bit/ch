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

## 通过 npm 安装本机巡检命令

从这一源码目录安装时，在外部项目目录执行 `npm install /path/to/cyber-clinic-community`。GitHub 公开仓库建立并发布版本标签后，可以使用 `npm install github:组织名/仓库名#版本标签`。安装只复制文件，首次运行以下命令才启动服务。

```sh
npm exec -- cyber-clinic --help
npm exec -- cyber-clinic serve
```

`serve` 在前台启动本机巡检与自动重启，按 Ctrl+C 停止。桌宠仍需要按上面的步骤构建。通过 npm 启动前，确认 8765 端口空闲。

## 许可与运营主体

代码适用 [Apache License 2.0](LICENSE)。露米形象和品牌素材适用[素材许可](skins/lumi/LICENSE)。公司及官网公示的备案信息见 [NOTICE](NOTICE)。安装包、账号与商业版服务入口以[万有智流官网](https://wanyouzhiliu.com/)为准。
