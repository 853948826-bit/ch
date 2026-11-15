"""一条命令的标准客制化向导：网页 / 图片 → 带交互的皮肤包。
  python3 tools/skin_wizard.py --page <角色百科/设定页链接> --name 我的形象 [--sound 笑声.mp3] [--apply]
  python3 tools/skin_wizard.py --image 正面.png --image 狂笑.png=laugh --name 我的形象 --apply
流程：抓取页面图片及说明 → 按说明关键词自动分配状态/姿势（跳过二创馆藏）→ 下载 → 抠图/切分
     → 按页面文字匹配性格预设（如“狂笑型”）→ 生成 skins/<名>.local 并应用。--dry-run 只看分配结果。"""
import argparse, html, json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_skin  # noqa: E402  复用抠图/切分

ROOT = make_skin.ROOT
UA = {"User-Agent": "Mozilla/5.0 (CyberClinic skin wizard)"}
SKIP = re.compile(r"馆藏|展厅|仿|museum|变体|variant|蛋形|幼态|juvenile|icon|logo|头像", re.I)
# (角色, 正则) 按顺序匹配；角色: sheet=多视图切分, normal/happy/worried/alert/sleep=标准状态, 其他=pose_<名>
RULES = [("sheet", r"三视图|turnaround|front.*side.*back"),
         ("laugh", r"狂笑|大笑|爆笑|laugh"),
         ("worried", r"低落|难过|伤心|沮丧|sad|downcast|cry"),
         ("wave", r"挥手|招手|wave"),
         ("sleep", r"睡觉|入睡|睡着|sleeping|asleep"),
         ("alert", r"惊讶|惊吓|surpris|shock"),
         ("happy", r"开心|微笑|smile|happy"),
         ("normal", r"正面|标准|经典|站立|front|classic|standard")]
STATE = {"happy": "eyes_happy", "worried": "eyes_worried", "alert": "eyes_surprised", "sleep": "eyes_closed"}
PRESETS = {  # 性格预设：页面文字命中关键词即套用
    "狂笑型": (r"狂笑|大笑|笑到|捧腹", {
        "lines": {"poke": ["哈哈哈哈哈哈", "哈哈哈哈哈哈哈哈！", "（笑到停不下来）"], "pat": ["哈哈……哈……"], "idle": ["……"],
                  "drag": ["哈哈哈哈放我下来哈哈"], "drop": ["……哈。"], "wake": ["……？"], "listen": ["……（在听）"],
                  "alert_warn": ["……不笑了，出事了"], "alert_crit": ["！！！"], "resolved": ["哈哈哈哈好了"]},
        "motions": {"poke": [["pose:laugh:2.4", "shake:2.2", "sound:laugh", "say:poke"],
                             ["pose:laugh2:2.4", "shake:2.0", "sound:laugh", "collapse", "say:poke"]],
                    "pat": [["pose:laugh:1.4", "shake:1.2", "sound:laugh"]],
                    "idle": [["jiggle"], ["hop:6"], ["pose:wave:2.2"], ["pose:laugh:1.2", "sound:laugh"]],
                    "drag": [["shake:1.2", "sound:laugh"]], "drop": [["squash", "jiggle"]],
                    "wake": [["pose:wave:2.2", "hop:6"]], "resolved": [["pose:laugh:1.6", "sound:laugh"]]}}),
    "护士型": (r"护士|nurse", {
        "lines": {"poke": ["呀！被戳到了～", "嗯哼？要看病吗？", "护士在此✨", "再戳要收挂号费咯～", "嘿嘿，抓到你偷懒了"],
                  "pat": ["嘿嘿…好舒服", "摸头杀…运行效率 +10%！"], "idle": ["今天也要健健康康地运行哦", "巡房中，一切安好！", "哼哼～♪"],
                  "alert_warn": ["发现一点小状况，我看看…"], "alert_crit": ["紧急情况！快来看看！"], "resolved": ["治好啦，干得漂亮！"],
                  "wake": ["我在！哪里不舒服？"], "listen": ["我在听，说吧～"], "drag": ["哇啊——放我下来！"], "drop": ["稳稳落地～"],
                  "focus": ["你忙，我小声守着～"], "mute_on": ["好的，我安静待着～"], "mute_off": ["我回来啦！"]},
        "motions": {"poke": [["pose:wink:2", "say:poke"], ["pose:wave:2", "say:poke"], ["face:eyes_surprised", "hop:10", "say:poke"]],
                    "pat": [["face:eyes_happy", "jiggle", "particle:💗"]],
                    "idle": [["pose:wave:2.2"], ["pose:think:3"], ["hop:6", "say:idle"], ["pose:cheer:2"], ["jiggle"]],
                    "drag": [["particle:💦"]], "drop": [["squash", "jiggle"]], "wake": [["pose:wave:2.2"]],
                    "resolved": [["pose:cheer:2", "particle:✨"]]}}),
    "通用": (r"", {
        "lines": {"poke": ["在呢～", "嗯？"], "pat": ["嘿嘿"], "idle": ["……"], "drag": ["哇——"], "drop": ["到啦"],
                  "wake": ["我在"], "resolved": ["好啦"]},
        "motions": {"poke": [["jiggle"], ["hop:10"], ["pose:wave:2"]], "pat": [["jiggle", "particle:💗"]],
                    "idle": [["jiggle"], ["hop:6"], ["pose:wave:2"]], "drop": [["squash", "jiggle"]],
                    "wake": [["pose:wave:2", "hop:6"]], "resolved": [["jiggle", "particle:✨"]]}}),
}


def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()


def scan_page(url):
    doc = fetch(url).decode("utf-8", "ignore")
    imgs = []
    for tag in re.findall(r"<img\b[^>]*>", doc, re.I):
        src = re.search(r'\bsrc="([^"]+)"', tag)
        alt = re.search(r'\balt="([^"]*)"', tag)
        if src:
            imgs.append((urllib.parse.urljoin(url, html.unescape(src.group(1))), html.unescape(alt.group(1)) if alt else ""))
    if not imgs:  # 外壳页：尝试内嵌 iframe 内容页
        fr = re.search(r'<iframe[^>]+src="([^"]+)"', doc, re.I)
        if fr:
            return scan_page(urllib.parse.urljoin(url, html.unescape(fr.group(1))))
    text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[\s\S]*?</\1>", " ", doc))
    return imgs, text


def assign(imgs):
    """说明文字 → 角色；每个角色取第一张，笑可取两张（laugh/laugh2）。"""
    got, seen = {}, set()
    for src, alt in imgs:
        if src in seen or SKIP.search(alt) or SKIP.search(src):
            continue
        seen.add(src)
        key = alt + " " + os.path.basename(src)
        for role, rx in RULES:
            if re.search(rx, key, re.I):
                if role == "laugh" and "laugh" in got and "laugh2" not in got:
                    role = "laugh2"
                if role not in got:
                    got[role] = (src, alt)
                break
    return got


def _hdr():
    sys.path.insert(0, ROOT)
    from clinic.client import headers
    return headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", help="角色百科/设定页链接")
    ap.add_argument("--image", action="append", default=[], help="本地或网络图片，可写 路径=角色（normal/laugh/wave/worried/sleep/alert/happy/sheet）")
    ap.add_argument("--name", required=True)
    ap.add_argument("--sound", help="笑声来源：音频/视频文件、媒体直链或视频网页链接；不给则用系统语音占位")
    ap.add_argument("--sound-clip", help="截取片段 开始-结束（秒），如 3.2-5.0")
    ap.add_argument("--preset", choices=list(PRESETS), help="性格预设，默认按页面文字自动匹配")
    ap.add_argument("--tol", type=float, default=38)
    ap.add_argument("--sheet", help="角色设定拼版（一张图含多个状态）")
    ap.add_argument("--grid", default="3x3", help="拼版列x行")
    ap.add_argument("--order", default="normal,happy,wave,cheer,wink,worried,alert,sleep,think", help="拼版从左到右、从上到下对应的角色")
    ap.add_argument("--area", default="0,0,1,1", help="拼版有效区域 x,y,w,h（0~1），用于裁掉标题文字")
    ap.add_argument("--canvas", default="200x250", help="皮肤画布，正常头身比例角色建议 200x340")
    ap.add_argument("--builtin", action="store_true", help="生成为随仓库发布的内置皮肤（仅限自有版权素材）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    imgs, text = scan_page(a.page) if a.page else ([], "")
    got = assign(imgs)
    for spec in a.image:  # 手动指定优先
        path, _, role = spec.partition("=")
        got[role or "normal"] = (path, "手动指定")
    preset = a.preset or next(k for k, (rx, _) in PRESETS.items() if re.search(rx, text + " ".join(x[1] for x in imgs)))
    print(f"性格预设：{preset}")
    for role, (src, alt) in got.items():
        print(f"  {role:<8} ← {alt[:30]}  {src[-60:]}")
    if a.sheet:
        print(f"  拼版 {a.grid} ← {a.sheet}：{a.order}")
    if not got and not a.sheet:
        sys.exit("没有识别到可用图片；请用 --image 指定")
    if a.dry_run:
        return

    cw, ch = (int(x) for x in a.canvas.lower().split("x"))
    make_skin.CANVAS = (cw, ch)
    slug = "".join(c if c.isalnum() else "_" for c in a.name) + ("" if a.builtin else ".local")
    d = os.path.join(ROOT, "skins", slug)
    src_dir = os.path.join(d, "src")
    os.makedirs(src_dir, exist_ok=True)
    local = {}
    for role, (src, _) in got.items():
        ext = os.path.splitext(urllib.parse.urlparse(src).path)[1] or ".png"
        dst = os.path.join(src_dir, role + ext)
        if re.match(r"https?://", src):
            with open(dst, "wb") as f:
                f.write(fetch(src))
        else:
            shutil.copy(os.path.expanduser(src), dst)
        local[role] = dst

    empty = make_skin._svg_head() + '</svg>'
    layers = {k: empty for k in make_skin.STATE_LAYER.values()}
    layers.update({k: empty for k in ["hair_back", "arm_l", "arm_r", "face", "blush", "eyes_open", "mouth_smile",
                                      "mouth_open", "mouth_cat", "mouth_worried", "mouth_o", "bangs", "cap", "body"]})
    whole = []
    if "sheet" in local:  # 三视图：侧面=被拎起，正面=常态，背面=睡觉
        views = make_skin.split(local["sheet"], a.tol, "0,0,1,0.92", 3)
        if views:
            layers["body"], layers["eyes_closed"] = views[1], views[2]
            layers["eyes_surprised"] = views[0]
            whole += ["eyes_closed", "eyes_surprised"]
    svgs = {}  # 角色 → 处理好的整图层
    if a.sheet:  # 拼版：横排一行按间隙自动识别，多行按网格等分
        cols, rows = (int(x) for x in a.grid.lower().split("x"))
        order = a.order.split(",")
        auto = make_skin.split(a.sheet, a.tol, a.area, cols) if rows == 1 else None
        ax, ay, aw, ah = (float(v) for v in a.area.split(","))
        for i, role in enumerate(order[:cols * rows]):
            c, r = i % cols, i // cols
            crop = f"{ax + aw * c / cols},{ay + ah * r / rows},{aw / cols},{ah / rows}"
            svgs[role] = auto[i] if auto else make_skin.cut(a.sheet, a.tol, crop)
    for role, path in local.items():
        if role != "sheet":
            svgs[role] = make_skin.cut(path, a.tol)
    if "normal" in svgs:
        layers["body"] = svgs["normal"]
    if layers["body"] == empty and svgs:
        layers["body"] = next(iter(svgs.values()))
    for role, svg in svgs.items():
        if role in STATE:
            layers[STATE[role]] = svg; whole.append(STATE[role])
        elif role not in ("sheet", "normal", "skip"):
            layers["pose_" + role] = svg
    with open(os.path.join(ROOT, "skins", "lumi", "holo.svg")) as f:
        layers["holo"] = f.read()
    for k, v in layers.items():
        with open(os.path.join(d, k + ".svg"), "w") as f:
            f.write(v)

    sounds = {}  # 不合成任何占位声音；只有给了 --sound 才装音效

    cfg = json.loads(json.dumps(PRESETS[preset][1]))
    poses = {k[5:] for k in layers if k.startswith("pose_")}
    for key, alts in cfg["motions"].items():  # 去掉缺图的姿势步骤
        cfg["motions"][key] = [[st for st in steps if not st.startswith("pose:") or st.split(":")[1] in poses]
                               for steps in alts]
        cfg["motions"][key] = [st for st in cfg["motions"][key] if st] or [["jiggle"]]
    man = {"name": a.name, "author": "Cyber Clinic" if a.builtin else "本机自制（不随仓库分发）",
           "source": a.page or "本地图片", "preset": preset,
           "viewBox": [cw, ch], "show_led": False, "eye_track": False, "whole_states": sorted(set(whole)),
           "anchors": {"head": [cw / 2, ch / 2], "body": [cw / 2, ch - 8], "led": [cw / 2, 10]}, "palette": {"accent": "#2fd4e6"},
           "layers": {k: k + ".svg" for k in layers}, "sounds": sounds, **cfg}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    print(f"已生成 {d}：{len(layers)} 层，姿势 {sorted(poses)}，状态 {sorted(set(whole))}，音效 {'自定义' if a.sound else '无（纯文字）'}")
    if a.sound:
        cmd = [sys.executable, os.path.join(ROOT, "tools", "skin_sound.py"), slug, a.sound]
        subprocess.run(cmd + (["--clip", a.sound_clip] if a.sound_clip else []), check=False)
    if a.apply:
        req = urllib.request.Request("http://127.0.0.1:8765/skin", json.dumps({"pack": slug}).encode(),
                                     _hdr())
        print("已应用:", urllib.request.urlopen(req).read().decode())


if __name__ == "__main__":
    main()
