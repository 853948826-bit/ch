"""皮肤部署套件：一张形象图 → 可用皮肤包（本机目录 skins/<名>.local，不随仓库分发）。
用法:
  python3 tools/make_skin.py 形象.png --name 我的形象 [--state happy=开心.png ...] [--apply]
  python3 tools/make_skin.py --sheet 表情拼图.png --grid 3x2 --name 我的形象 [--order normal,happy,worried,alert,sleep,dragging]
  python3 tools/make_skin.py --sheet 三视图.png --grid 3x1 --order dragging,normal,sleep --area 0,0,1,0.92 --name 我的形象
  （--order 中写 skip 可跳过某格）
  （表情拼图可用任意图像模型以素材图为参考一次生成，提示词见 docs/SKIN_PROMPT.md）
- 自动去除纯色背景、裁切、适配画布；整图做漂浮/呼吸/Q弹/跳跃/转身等全身动作。
- --state 可为 happy/worried/alert/sleep/thinking/dragging 提供整张替换图，对应情绪时切换。
- --apply 生成后立即让运行中的小精灵换上（需守护进程在运行）。"""
import argparse, base64, json, os, subprocess, sys, tempfile, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "tools", "skinkit")
CANVAS = (200, 250)  # 皮肤画布（viewBox），可由调用方修改，如正常头身比例用 (200, 340)


def _svg_head():
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS[0]} {CANVAS[1]}" width="{CANVAS[0]}" height="{CANVAS[1]}">'


def _env():
    return dict(os.environ, SKINKIT_SIZE=f"{CANVAS[0] * 2}x{CANVAS[1] * 2}")
STATE_LAYER = {"happy": "eyes_happy", "worried": "eyes_worried", "alert": "eyes_surprised",
               "sleep": "eyes_closed", "dragging": "eyes_surprised"}


def kit():
    src = KIT + ".swift"
    if not os.path.exists(KIT) or os.path.getmtime(KIT) < os.path.getmtime(src):
        subprocess.run(["swiftc", "-O", src, "-o", KIT], check=True, stderr=subprocess.DEVNULL)
    return KIT


def _wrap(png):
    data = base64.b64encode(open(png, "rb").read()).decode()
    os.remove(png)
    return (_svg_head() + f'<image href="data:image/png;base64,{data}" x="0" y="0" width="{CANVAS[0]}" height="{CANVAS[1]}"/></svg>')


def split(img, tol, area, n):
    """按空白间隙自动识别 n 个角色（适合三视图、横排表情），失败返回 None 改用等分。"""
    out = tempfile.mktemp(suffix=".png")
    r = subprocess.run([kit(), img, out, str(tol), area, f"split={n}"], capture_output=True, text=True, env=_env())
    if r.returncode:
        print("自动识别失败，改用等分：", r.stdout.strip())
        return None
    return [_wrap(out.replace(".png", f"_{i}.png")) for i in range(n)]


def cut(img, tol, crop=None):
    out = tempfile.mktemp(suffix=".png")
    r = subprocess.run([kit(), img, out, str(tol)] + ([crop] if crop else []), capture_output=True, text=True, env=_env())
    if r.returncode:
        sys.exit(f"处理失败 {img}: {r.stdout}{r.stderr}")
    data = base64.b64encode(open(out, "rb").read()).decode()
    os.remove(out)
    return (_svg_head() + f'<image href="data:image/png;base64,{data}" x="0" y="0" width="{CANVAS[0]}" height="{CANVAS[1]}"/></svg>')


def _hdr():
    sys.path.insert(0, ROOT)
    from clinic.client import headers
    return headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?")
    ap.add_argument("--sheet", help="表情拼图（一张图含多个状态）")
    ap.add_argument("--grid", default="3x2", help="拼图列x行")
    ap.add_argument("--area", default="0,0,1,1", help="拼图有效区域 x,y,w,h（0~1），用于裁掉标题/文字")
    ap.add_argument("--order", default="normal,happy,worried,alert,sleep,dragging", help="拼图从左到右、从上到下的状态")
    ap.add_argument("--name", required=True)
    ap.add_argument("--state", action="append", default=[], help="情绪=图片，如 happy=a.png")
    ap.add_argument("--tol", type=float, default=38, help="去背景容差，背景残留就调大")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    slug = "".join(c if c.isalnum() else "_" for c in a.name) + ".local"
    d = os.path.join(ROOT, "skins", slug)
    os.makedirs(d, exist_ok=True)
    empty = _svg_head() + '</svg>'
    layers = {k: empty for k in ["hair_back", "arm_l", "arm_r", "face", "blush", "eyes_open", "eyes_closed",
                                 "eyes_happy", "eyes_worried", "eyes_surprised", "mouth_smile", "mouth_open",
                                 "mouth_cat", "mouth_worried", "mouth_o", "bangs", "cap"]}
    whole = []
    if a.sheet:
        cols, rows = (int(x) for x in a.grid.lower().split("x"))
        auto = split(a.sheet, a.tol, a.area, cols) if rows == 1 else None
        for i, st in enumerate(a.order.split(",")):
            if i >= cols * rows:
                break
            c, r = i % cols, i // cols
            ax, ay, aw, ah = (float(v) for v in a.area.split(","))
            crop = f"{ax + aw * c / cols},{ay + ah * r / rows},{aw / cols},{ah / rows}"
            if st == "skip":
                continue
            svg = auto[i] if auto else cut(a.sheet, a.tol, crop)
            if st != "normal":
                whole.append(STATE_LAYER.get(st))
            if st == "normal":
                layers["body"] = svg
            elif st in STATE_LAYER:
                layers[STATE_LAYER[st]] = svg
    elif a.image:
        layers["body"] = cut(a.image, a.tol)
    else:
        sys.exit("需要提供 形象图片 或 --sheet 表情拼图")
    for spec in a.state:
        st, path = spec.split("=", 1)
        if st not in STATE_LAYER:
            sys.exit(f"未知情绪 {st}，可选 {list(STATE_LAYER)}")
        layers[STATE_LAYER[st]] = cut(path, a.tol)  # 整图替换本体显示
        whole.append(STATE_LAYER[st])
    with open(os.path.join(ROOT, "skins", "lumi", "holo.svg")) as f:
        layers["holo"] = f.read()
    for k, v in layers.items():
        with open(os.path.join(d, k + ".svg"), "w") as f:
            f.write(v)
    man = {"name": a.name, "author": "本机自制（不随仓库分发）", "viewBox": [200, 250], "show_led": False,
           "eye_track": False, "whole_states": sorted(set(x for x in whole if x)), "anchors": {"head": [100, 125], "body": [100, 242], "led": [100, 10]},
           "palette": {"accent": "#7cc7ff"}, "layers": {k: k + ".svg" for k in layers},
           "lines": {"poke": ["……", "盯——", "在呢"], "pat": ["嘿嘿", "舒服"], "idle": ["……（巡逻中）"],
                     "drag": ["又被搬走了——"], "drop": ["新据点。"], "wake": ["我在，说吧"]},
           "motions": {"poke": [["jiggle"], ["hop:10"], ["spin"]], "pat": [["jiggle", "particle:💗"]],
                       "idle": [["jiggle"], ["hop:6"], ["say:idle"], ["spin"]], "drag": [["particle:💦"]],
                       "drop": [["squash", "jiggle"]], "wake": [["hop:8"]], "resolved": [["jiggle", "particle:✨"]]}}
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    print(f"已生成皮肤包 {d}（编辑 manifest.json 可改台词/动作）")
    if a.apply:
        req = urllib.request.Request("http://127.0.0.1:8765/skin", json.dumps({"pack": slug}).encode(),
                                     _hdr())
        print("已应用:", urllib.request.urlopen(req).read().decode())


if __name__ == "__main__":
    main()
