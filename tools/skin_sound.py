"""给皮肤包装上音效：音频/视频文件、媒体直链、视频网页链接（需 yt-dlp）→ 裁剪 → 写入皮肤包。
  python3 tools/skin_sound.py <皮肤包名> <来源> [--clip 3.2-5.0] [--name laugh]
  来源可以是：本地 mp3/m4a/wav/aiff；本地 mp4/mov 视频；媒体文件直链；B站/YouTube 等视频页链接。"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA = (".mp3", ".m4a", ".wav", ".aiff", ".aif", ".caf", ".mp4", ".mov", ".m4v", ".aac")


def fetch_source(src, tmp):
    """返回本地媒体文件路径。"""
    if not re.match(r"https?://", src):
        p = os.path.expanduser(src)
        if not os.path.isfile(p):
            sys.exit(f"找不到文件：{p}")
        return p
    ext = os.path.splitext(urllib.parse.urlparse(src).path)[1].lower()
    if ext in MEDIA:  # 媒体直链
        out = os.path.join(tmp, "src" + ext)
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        open(out, "wb").write(urllib.request.urlopen(req, timeout=60).read())
        return out
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        sys.exit("这是视频网页链接，需要 yt-dlp 提取音频：uv tool install yt-dlp（或下载视频后传本地文件）")
    r = subprocess.run([ytdlp, "-f", "bestaudio/best", "-o", os.path.join(tmp, "src.%(ext)s"), "--no-playlist", src],
                       capture_output=True, text=True)
    files = [f for f in os.listdir(tmp) if f.startswith("src.")]
    if r.returncode or not files:
        sys.exit("提取失败（部分平台需要登录 cookies，可先下载视频再传本地文件）：\n" + (r.stderr or r.stdout)[-400:])
    return os.path.join(tmp, files[0])


def duration(path):
    out = subprocess.run(["afinfo", path], capture_output=True, text=True).stdout
    m = re.search(r"estimated duration:\s*([\d.]+)", out)
    return float(m.group(1)) if m else None


def _hdr():
    sys.path.insert(0, ROOT)
    from clinic.client import headers
    return headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack", help="皮肤包目录名，如 我的形象.local")
    ap.add_argument("source")
    ap.add_argument("--clip", help="截取片段 开始-结束（秒），如 3.2-5.0")
    ap.add_argument("--name", default="laugh", help="音效名（动作里用 sound:<名> 触发），默认 laugh")
    a = ap.parse_args()
    d = os.path.join(ROOT, "skins", a.pack)
    man_path = os.path.join(d, "manifest.json")
    if not os.path.isfile(man_path):
        sys.exit(f"找不到皮肤包：{d}")
    with tempfile.TemporaryDirectory() as tmp:
        src = fetch_source(a.source, tmp)
        out = os.path.join(d, f"{a.name}.m4a")
        cmd = ["avconvert", "--source", src, "--preset", "PresetAppleM4A", "--output", out, "--replace"]
        if a.clip:
            s, e = (float(x) for x in a.clip.split("-"))
            cmd += ["--start", str(s), "--duration", str(e - s)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode or not os.path.isfile(out):
            sys.exit("音频转换失败：" + (r.stderr or r.stdout)[-300:])
    dur = duration(out)
    with open(man_path, encoding="utf-8") as f:
        man = json.load(f)
    man.setdefault("sounds", {})[a.name] = os.path.basename(out)
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    print(f"已写入 {out}（{dur:.1f}s）" if dur else f"已写入 {out}")
    try:  # 守护进程在运行就立即热更新
        req = urllib.request.Request("http://127.0.0.1:8765/skin", json.dumps({"pack": a.pack}).encode(),
                                     _hdr())
        urllib.request.urlopen(req, timeout=3)
        print("已热更新到小精灵")
    except OSError:
        pass


if __name__ == "__main__":
    main()
