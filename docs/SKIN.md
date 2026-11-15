# 形象客制化接口（皮肤包）

## ⚡ 标准流程：一条命令生成（零门槛）

```bash
python3 tools/skin_wizard.py --page <角色百科/设定页链接> --name 我的形象 --sound 笑声.mp3 --apply
```

| 步骤 | 自动完成的事 |
|---|---|
| 1 读页面 | 抓取页面图片及其说明文字（外壳页自动进入内嵌内容页） |
| 2 分配 | 按说明关键词分配：三视图→正面/侧面(拎起)/背面(睡觉)；狂笑/大笑→戳一戳姿势；挥手→唤醒；低落→告警；名画二创、展厅、变体自动跳过 |
| 3 处理 | 下载 → 去背景（边缘众数背景色 + 杂边裁除）→ 按间隙切分多视图 → 统一比例 |
| 4 性格 | 按页面文字匹配性格预设（如“狂笑型”：戳一下仰头狂笑+颤抖+笑声，台词只有笑声） |
| 5 应用 | 生成 `skins/<名>.local/`（原图存于 `src/` 可复现），立即热更新到小精灵 |

- 先看分配结果不落盘：加 `--dry-run`；分错了用 `--image 路径或链接=角色` 覆盖（角色：normal / laugh / laugh2 / wave / worried / sleep / alert / happy / sheet）。
- 没有网页时只给图片：`--image 正面.png --image 狂笑.png=laugh --image 挥手.png=wave`。
- 音效：`--sound` 支持本地音频、本地视频（mp4/mov 自动抽音轨）、媒体直链、B站/YouTube 等视频页链接（需 `uv tool install yt-dlp`），配合 `--sound-clip 3.2-5.0` 截取片段；不给则用系统语音合成占位。
- 已生成的皮肤单独换音效：`python3 tools/skin_sound.py 我的形象.local <音频/视频/链接> --clip 3.2-5.0`（`--name giggle` 设第二种笑声），自动热更新。
- 抠图：边缘众数背景色 + 外框杂线裁除 + 下半身封闭背景洞（胯下/腿间）+ 脚底浅色地面阴影，牙齿与眼白不受影响。
- 实测：一个角色百科页 → 7 张设定图全部正确分配并抠图，端到端约 4 秒。

小精灵形象完全由守护进程下发，**任何智能体都能通过 HTTP / MCP / Python SDK 实时替换**，小精灵按版本号热更新，无需重启。

## 皮肤包格式

```
skins/<皮肤名>/
  manifest.json      名称、作者、许可、viewBox、锚点、调色板、图层文件、台词
  *.svg              图层（分层矢量或内嵌位图，同一 viewBox 叠放）
```

- 画布：`viewBox` 可按角色比例自定义（如 `[200, 340]` 适合正常头身比例），小精灵窗口自动适配
- 图层（缺失的层自动回退到默认皮肤“露米”）：`hair_back body arm_l arm_r face blush eyes_open eyes_closed eyes_happy eyes_worried eyes_surprised mouth_smile mouth_open mouth_cat mouth_worried mouth_o bangs cap holo`
- 每层是完整 `<svg>`，viewBox 默认 `0 0 200 250`，层与层按坐标叠放；可用 `{{调色板键}}` 占位，换色无需改图。
- 锚点 `anchors`：`head`（头部摆动支点）、`arm_l`/`arm_r`（挥手支点）、`body`（呼吸支点）、`led`（状态灯位置），均为 SVG 坐标。
- 台词 `lines`：`poke pat idle alert_warn alert_crit resolved wake listen mute_on mute_off drag drop focus`，每类随机抽取。
- 安全：单层 ≤ 256KB；脚本、事件属性、foreignObject、外链引用会被剥离；图层路径不得逃逸皮肤包目录。

## 实时修改

| 操作 | HTTP | MCP 工具 | Python |
|---|---|---|---|
| 查看规格 | `GET /skin?spec=1` | `clinic_skin_spec` | `Clinic().skin(spec=True)` |
| 导入整包 | `POST /skin {"pack": "my_skin"}` | `clinic_skin_set {pack}` | `set_skin(pack="my_skin")` |
| 改色 | `POST /skin {"palette": {"hair": "#ffc8e4"}}` | `clinic_skin_set {palette}` | `set_skin(palette={...})` |
| 换单层 | `POST /skin {"layers": {"cap": "<svg ...>"}}` | `clinic_skin_set {layers}` | `set_skin(layers={...})` |
| 改台词 | `POST /skin {"lines": {"poke": ["..."]}}` | `clinic_skin_set {lines}` | `set_skin(lines={...})` |
| 恢复默认 | `POST /skin {"reset": true}` | `clinic_skin_set {reset}` | `set_skin(reset=True)` |

示例：让智能体把露米换成樱花配色

```bash
curl -s -H "X-Clinic-Token: $(cat ~/.cyberclinic/token)" localhost:8765/skin -d '{"palette":{"hair":"#ffc8e4","hair_dark":"#f29cc8","accent":"#ff6fb0","eye":"#ff7fbf"}}'
```

## 默认形象“露米 Lumi”

动漫风护士整图形象（画布 200×320），由 9 张状态图（normal/happy/worried/alert/sleep/wave/cheer/wink/think）走标准皮肤流程生成：
`python3 tools/skin_wizard.py --preset 护士型 …` → 抠图、对齐脚底、写 manifest。露米形象保留所有权利（见 `skins/lumi/LICENSE`），自己的形象请另建皮肤包。

- 整图状态：`eyes_happy` `eyes_worried` `eyes_surprised` `eyes_closed` 直接替换本体；`pose_wave` `pose_cheer` `pose_wink` `pose_think` 由 `pose:` 原语调用
- 默认无音效、不朗读（`tts: false`），只用文字气泡
- 付费袜鞋款 `skins/lumi_*.local` 以 `"base": "lumi"` 继承本包，只覆盖本体图
- 素材版权归 Cyber Clinic，品牌使用见 README 商标说明

## 交互动作也可客制化（motions）

皮肤可以自定义每个交互触发什么动作，由内置“动作原语”组合而成，每个键给多组候选、随机抽一组执行：

```json
"motions": {
  "poke": [["side_eye:-5", "say:poke"], ["jiggle", "particle:👀"]],
  "pat":  [["rub", "jiggle"]],
  "idle": [["rub"], ["face:eyes_closed:mouth_smile", "particle:💤"]]
}
```

- 触发键：`poke pat idle drag drop wake resolved alert_warn alert_crit`
- 原语：`tilt:弧度:秒` `wave` `hop:高度` `squash` `jiggle` `spin` `side_eye:像素` `stretch` `rub` `holo:秒` `particle:emoji` `face:眼睛层:嘴巴层` `say:台词键` `shake:秒`（狂笑颤抖） `collapse`（笑到脱力） `sound:音效名` `pose:姿势名:秒`（临时切换到整图姿势层 `pose_<名>`，数量不限，如 pose_laugh、pose_wave）
- 软弹（仅新客户端）：manifest 顶层 `"jiggle"`，`false` 关闭，`{"strength": 系数}` 调强弱；胸部按 `click_chest` 区域弹动，裙摆按 `click_skirt` 区域只在拖动时轻摆；臀部肉感晃动只在 `"hip_poses": [姿势名…]` 列出的、能看到臀部的姿势里启用。
- 音效：manifest 中 `"sounds": {"laugh": "laugh.mp3"}`，文件放在皮肤包目录内（aiff/wav/mp3/m4a/caf），用 `sound:laugh` 触发；静音和全屏勿扰时不播放
- 开关：`"show_led": false` 隐藏状态灯；`"eye_track": false` 关闭眼神跟随（整图皮肤建议关闭）
- 实时修改：`POST /skin {"motions": {...}}` 或 MCP `clinic_skin_set`；未知原语会被拒绝

## 本机皮肤与一键部署套件

- 目录名以 `.local` 结尾的皮肤（如 `skins/我的形象.local/`）只在本机使用，已加入 `.gitignore`，不会随开源仓库分发；用户自定义的当前形象保存在 `run/skin_current.json`，重启后保留。
- **一张图片直接生成皮肤**（自动去纯色背景、裁切、适配画布，零依赖）：

```bash
python3 tools/make_skin.py 形象.png --name 我的形象 --state happy=开心.png --state sleep=睡觉.png --apply
```

整图皮肤的表情由 `--state` 提供的整张图切换（happy/worried/alert/sleep/dragging），动作使用漂浮、呼吸、Q弹、跳跃、转身等全身原语；背景没去干净时调大 `--tol`。需要眼睛/嘴巴独立动的分层效果，可在生成的包里继续替换对应 SVG 层。

## 案例：狂笑型角色（按设定客制化）

假设角色设定是“点一下就狂笑、捧腹颤抖、笑到脱力、没有台词”：戳一戳 = `shake` + `sound:laugh` + `collapse`；台词只有笑声；用素材原图一比一生成整图皮肤。三视图可直接切成多视角状态：

```bash
python3 tools/make_skin.py --sheet 三视图.png --grid 3x1 --order dragging,normal,sleep --area 0,0,1,0.92 --name 我的形象
```

品牌投放时把官方笑声/口播音频放进皮肤包、换掉 `sounds` 即可，无需改代码。
