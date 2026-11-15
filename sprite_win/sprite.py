"""赛博诊所 · Windows 小精灵（tkinter，零第三方依赖）。
支持图片类皮肤（如奶蛙、露米高清版）；矢量皮肤请使用 macOS 原生版。
交互与 macOS 版一致：单击=戳一戳  长按=摸头  双击=静音  右键=菜单  拖动=拎起  Ctrl+Alt+Space=提需求
自检：python sprite_win/sprite.py --selftest"""
import base64, json, os, queue, random, re, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, wave
import tkinter as tk
from tkinter import simpledialog

API = os.environ.get("CLINIC_URL", "http://127.0.0.1:8765")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = sys.platform == "win32"
KEY = "#0a0b0c"  # 透明色键：选接近黑的冷门色，抗锯齿边缘融成深色描边，不出彩边
MOOD = {"normal": "body", "happy": "eyes_happy", "worried": "eyes_worried", "alert": "eyes_surprised",
        "sleep": "eyes_closed", "dragging": "eyes_surprised"}
FIXED = {"wave": 1.8, "hop": 0.45, "squash": 0.35, "jiggle": 0.7, "spin": 0.9, "stretch": 2.0, "rub": 1.5,
         "collapse": 2.2, "side_eye": 2.0, "cheer": 1.4, "boing": 0.8, "blush": 1.5}

if not sys.stdout:  # pythonw 无控制台：日志写入 run/sprite.log
    os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
    sys.stdout = sys.stderr = open(os.path.join(ROOT, "run", "sprite.log"), "a", encoding="utf-8", buffering=1)


def _auth_headers():
    """本机接口令牌（守护进程首次启动生成，仅当前用户可读）。"""
    h = {"Content-Type": "application/json"}
    try:
        h["X-Clinic-Token"] = open(os.path.join(os.path.expanduser("~"), ".cyberclinic", "token")).read().strip()
    except OSError:
        pass
    return h


def http(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Sound:
    """WAV 分段播放：只播动作时长那一段；连续互动从实际播到的位置接着播，播完回到开头。"""

    def __init__(self):
        self.tracks, self.cur, self.flip = {}, None, 0

    def load(self, sounds):
        self.stop()
        self.tracks = {}
        for name, path in (sounds or {}).items():
            if not path.lower().endswith(".wav"):
                print(f"[sound] 跳过非 WAV 音效 {path}（Windows 版仅支持 WAV）")
                continue
            try:
                with wave.open(path) as w:
                    self.tracks[name] = [w.getparams(), w.readframes(w.getnframes()), 0]
            except (OSError, wave.Error) as e:
                print(f"[sound] 读取失败 {path}: {e}")

    def segment(self, name, dur):
        t = self.tracks.get(name)
        if not t:
            return None
        params, frames, off = t
        bpf = params.sampwidth * params.nchannels
        if self.cur and self.cur[0] == name:  # 正在播：从实际播到的位置接续
            played = int((time.time() - self.cur[1]) * params.framerate) * bpf
            if played < self.cur[3]:
                off = self.cur[2] + played
        off -= off % bpf
        n = int(dur * params.framerate) * bpf
        seg = frames[off:off + n]
        t[2] = off + len(seg) if off + len(seg) < len(frames) else 0
        self.cur = (name, time.time(), off, len(seg))
        self.flip ^= 1  # 两个临时文件轮换，避免覆盖正在播放的文件
        path = os.path.join(tempfile.gettempdir(), f"cyberclinic_{os.getpid()}_{self.flip}.wav")
        with wave.open(path, "wb") as w:
            w.setparams(params)
            w.writeframes(seg)
        return path

    def play(self, name, dur):
        path = self.segment(name, dur)
        if not path:
            print(f"[sound] 跳过 {name}：皮肤没有该音效或非 WAV（已加载：{list(self.tracks)}）", flush=True)
            return
        print(f"[sound] 播放 {name} {dur:.1f}s -> {path}", flush=True)
        if WIN:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        self.cur = None
        if WIN:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)


class App:
    def __init__(self, selftest=False):
        self.selftest = selftest
        self.root = tk.Tk()
        self.root.title("CyberClinic")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if WIN:
            self.root.config(bg=KEY)
            self.root.attributes("-transparentcolor", KEY)
            bg = KEY
        else:
            self.root.config(bg="systemTransparent")
            self.root.attributes("-transparent", True)
            bg = "systemTransparent"
        self.cv = tk.Canvas(self.root, highlightthickness=0, bd=0, bg=bg)
        self.cv.pack()
        self.item = self.cv.create_image(0, 0, anchor="nw")
        self.bub = tk.Toplevel(self.root)
        self.bub.overrideredirect(True)
        self.bub.attributes("-topmost", True)
        self.bub.withdraw()
        self.lbl = tk.Label(self.bub, bg="white", fg="#333", justify="left", wraplength=260, padx=10, pady=8,
                            font=("Microsoft YaHei UI" if WIN else "PingFang SC", 10), relief="solid", bd=1)
        self.lbl.pack()
        self.q, self.raw, self.imgs, self.skin, self.skin_ver = queue.Queue(), {}, {}, {}, 0
        self.sub, self.mood, self.layer, self.muted, self.open, self.online = 2, "normal", "body", False, {}, True
        self.press_t, self.drag, self.patted, self.dragging, self.after_poke, self.after_long = 0, None, False, False, None, None
        self.press_zone, self.click_count = None, 1
        self.feedback_offsets = {}
        self.revert, self.bub_after, self.sleeping = None, None, False
        self.sound = Sound()
        self.preview_specs, self.pcache, self.pv = {}, {}, None
        self.last_pack = None
        for ev, fn in (("<ButtonPress-1>", self.on_press), ("<B1-Motion>", self.on_move),
                       ("<ButtonRelease-1>", self.on_release), ("<Double-Button-1>", self.on_double),
                       ("<Triple-Button-1>", self.on_triple), ("<Motion>", self.on_hover), ("<Leave>", self.on_leave),
                       ("<Button-3>", self.on_menu), ("<Button-2>", self.on_menu)):
            self.cv.bind(ev, fn)
        threading.Thread(target=self.poll, daemon=True).start()
        if WIN:
            threading.Thread(target=self.hotkey_thread, daemon=True).start()
        self.root.after(400, self.pump)
        self.root.after(random.randint(14000, 30000), self.idle)
        self.root.after(10000, self.check_sleep)

    # ---------- 皮肤 ----------
    def load_skin(self, sk):
        self.clear_revert()
        self.feedback_offsets.clear()
        self.layer = "body"
        self.skin, self.raw, self.imgs = sk, {}, {}
        for name, svg in sk.get("layers", {}).items():
            b64s = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", svg)
            if b64s:
                self.raw[name] = b64s  # 模块化衣橱：底图 + 各衣物层
        if "body" not in self.raw:
            self.say("当前皮肤是矢量皮肤，Windows 版暂只支持图片皮肤（如奶蛙）", 8)
        self.rescale()
        self.sound.load(sk.get("sounds"))

    def rescale(self):
        self.imgs = {}
        for name, b64s in self.raw.items():
            img = self.compose(b64s)
            self.imgs[name] = img.subsample(self.sub) if self.sub > 1 else img
        self.show(self.layer if self.layer in self.imgs else "body")
        if self.root.winfo_x() == 0 and self.root.winfo_y() == 0:
            self.root.update_idletasks()
            w, h = self.cv.winfo_reqwidth(), self.cv.winfo_reqheight()
            self.root.geometry(f"+{self.root.winfo_screenwidth() - w - 40}+{self.root.winfo_screenheight() - h - 80}")

    def compose(self, b64s):
        img = None
        for b in b64s:
            layer = tk.PhotoImage(data=b)
            if img is None:
                img = layer
            else:
                self.root.tk.call(img, "copy", layer, "-compositingrule", "overlay")
        return img

    def show_placeholder(self):
        """没有任何可显示图片时的保底形象：一个可点击的圆形徽标，右键仍能打开菜单。"""
        self.cv.delete("ph")
        self.cv.config(width=120, height=120)
        self.cv.create_oval(10, 10, 110, 110, fill="#2fd4e6", outline="#1b4a78", width=3, tags="ph")
        self.cv.create_text(60, 60, text="诊所", fill="white", font=("Microsoft YaHei UI", 16, "bold"), tags="ph")

    def show(self, layer):
        img = self.imgs.get(layer) or self.imgs.get("body")
        if not img:
            return
        self.layer = layer if layer in self.imgs else "body"
        self.cv.itemconfig(self.item, image=img)
        self.cv.config(width=img.width(), height=img.height())

    def set_mood(self, m):
        self.mood = m
        layer = MOOD.get(m, "body")
        self.show(layer if layer in self.skin.get("whole_states", []) or layer == "body" else "body")

    def temp(self, layer, secs):
        """临时换图（姿势/表情），到时回到当前情绪图。"""
        if layer not in self.imgs:
            return
        self.clear_revert()
        self.show(layer)
        self.revert = self.root.after(int(secs * 1000), self.end_temp)

    def clear_revert(self):
        if self.revert:
            self.root.after_cancel(self.revert)
            self.revert = None

    def end_temp(self):
        self.revert = None
        self.set_mood(self.base_mood())

    def base_mood(self):
        if self.sleeping:
            return "sleep"
        now = time.time()  # 严重告警保持担心；普通警告只在出现后 60 秒内担心
        return "worried" if any(e.get("severity") == "crit" or (e.get("severity") == "warn" and now - e.get("t_detect", 0) < 60)
                                for e in self.open.values()) else "normal"

    def line(self, key, fb=""):
        opts = self.skin.get("lines", {}).get(key) or [fb]
        return random.choice(opts)

    # ---------- 动作 ----------
    def duration(self, steps):
        d = 0.0
        for st in steps:
            p = st.split(":")
            if p[0] == "pose":
                d = max(d, float(p[2]) if len(p) > 2 else 2)
            elif p[0] in ("shake", "holo"):
                d = max(d, float(p[1]) if len(p) > 1 else 1.6)
            elif p[0] == "tilt":
                d = max(d, float(p[2]) if len(p) > 2 else 1)
            else:
                d = max(d, FIXED.get(p[0], 0))
        return d or 1.5

    def perform(self, key):
        alts = self.skin.get("motions", {}).get(key)
        if not alts:
            return False
        steps = random.choice(alts)
        dur = self.duration(steps)
        for st in steps:
            p = st.split(":")
            op, a = p[0], p[1:]
            if op == "pose" and a:
                self.temp("pose_" + a[0], float(a[1]) if len(a) > 1 else 2)
            elif op == "face" and a and a[0]:
                self.temp(a[0], 2.2)
            elif op in ("shake", "jiggle", "squash", "collapse"):
                self.shake(float(a[0]) if op == "shake" and a else 0.6, 3 if op == "shake" else 2)
            elif op == "hop":
                self.hop(int(float(a[0])) if a else 12)
            elif op == "say":
                self.say(self.line(a[0] if a else "idle"), 3)
            elif op == "sound":
                if self.muted:
                    print("[sound] 跳过：静音中（双击可取消）", flush=True)
                else:
                    self.sound.play(a[0] if a else "laugh", dur)
        return True

    def shake(self, secs, amp):
        x0, y0, n = self.root.winfo_x(), self.root.winfo_y(), max(4, int(secs * 14))
        for i in range(n + 1):
            dx = 0 if i == n else (amp if i % 2 else -amp)
            self.root.after(int(i * secs * 1000 / n), lambda dx=dx: self.root.geometry(f"+{x0 + dx}+{y0}"))

    def hop(self, h):
        x0, y0 = self.root.winfo_x(), self.root.winfo_y()
        for i, dy in enumerate([-h * 0.6, -h, -h * 0.6, 0, -h * 0.2, 0]):
            self.root.after(i * 70, lambda dy=dy: self.root.geometry(f"+{x0}+{int(y0 + dy)}"))

    def say(self, text, secs=4.5):
        if not text:
            return
        self.lbl.config(text=text)
        self.bub.update_idletasks()
        bw, bh = self.bub.winfo_reqwidth(), self.bub.winfo_reqheight()
        x = max(10, self.root.winfo_x() - bw + 40)
        y = max(10, self.root.winfo_y() - bh + 30)
        self.bub.geometry(f"+{x}+{y}")
        self.bub.deiconify()
        self.bub.lift()
        if self.bub_after:
            self.root.after_cancel(self.bub_after)
        self.bub_after = self.root.after(int(secs * 1000), self.bub.withdraw) if secs else None

    # ---------- 手势 ----------
    def interaction_zone(self, e):
        x = e.x / max(1, self.cv.winfo_width())
        y = e.y / max(1, self.cv.winfo_height())
        pose = self.layer.removeprefix("pose_")
        zones = self.skin.get("pose_interaction_zones", {}).get(pose, self.skin.get("interaction_zones", {}))
        for key, bounds in zones.items():
            if len(bounds) == 4 and bounds[0] <= x <= bounds[2] and bounds[1] <= y < bounds[3]:
                return key
        return None

    def feedback(self, key, secs=0.75):
        choices = [pose for pose in self.skin.get("interaction_variants", {}).get(key, [])
                   if "pose_" + pose in self.imgs]
        if choices:
            index = self.feedback_offsets.get(key, 0) % len(choices)
            name = choices[index]
            self.feedback_offsets[key] = (index + 1) % len(choices)
        else:
            name = self.skin.get("interaction_poses", {}).get(key, "")
        layer = "pose_" + name
        if layer not in self.imgs:
            return False
        if secs is None:
            self.clear_revert()
            if self.layer != layer:
                self.show(layer)
        else:
            self.temp(layer, secs)
            if name in self.skin.get("shared_pose_names", []) and key not in ("drag_release",):
                self.cv.move(self.item, -2, 0)
                self.root.after(90, lambda: self.cv.coords(self.item, 0, 0))
        return True

    def on_hover(self, e):
        poses = self.skin.get("gaze_poses", {})
        if not poses or self.drag or self.mood != "normal" or self.revert:
            return
        dx = e.x / max(1, self.cv.winfo_width()) - 0.516
        dy = e.y / max(1, self.cv.winfo_height()) - 0.203
        if (dx * dx + dy * dy) ** 0.5 <= 0.052:
            self.on_leave(e)
            return
        direction = ("left" if dx < 0 else "right") if abs(dx) > abs(dy) * 1.3 else ("up" if dy < 0 else "down")
        layer = "pose_" + poses.get(direction, "")
        if layer in self.imgs and self.layer != layer:
            self.show(layer)

    def on_leave(self, e):
        if self.layer in {"pose_" + pose for pose in self.skin.get("gaze_poses", {}).values()}:
            self.show("body")

    def hold(self):
        self.after_long = None
        if self.feedback("press_hold", None):
            self.patted = True
        else:
            self.pat()

    def on_press(self, e):
        self.press_t, self.drag, self.dragging, self.patted = time.time(), (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y()), False, False
        self.press_zone, self.click_count = self.interaction_zone(e), 1
        self.after_long = self.root.after(550, self.hold)

    def on_move(self, e):
        if not self.drag:
            return
        dx, dy = e.x_root - self.drag[0], e.y_root - self.drag[1]
        if not self.dragging and abs(dx) + abs(dy) > 5:
            self.dragging = True
            if self.after_long:
                self.root.after_cancel(self.after_long)
                self.after_long = None
            self.set_mood("dragging")
            if not self.skin.get("interaction_poses"):
                self.perform("drag") or self.say(self.line("drag", "哇——"), 1.5)
        if self.dragging:
            self.root.geometry(f"+{self.drag[2] + dx}+{self.drag[3] + dy}")
            key = "drag_lift" if abs(dy) > abs(dx) else "drag_sleeve_left" if dx < 0 else "drag_sleeve_right"
            self.feedback(key, None)

    def on_release(self, e):
        if self.after_long:
            self.root.after_cancel(self.after_long)
            self.after_long = None
        self.drag = None
        if self.dragging:
            self.dragging = False
            self.set_mood(self.base_mood())
            self.feedback("drag_release") or self.perform("drop") or self.say(self.line("drop", "到啦"), 1.5)
        elif self.patted and self.skin.get("interaction_poses"):
            self.set_mood(self.base_mood())
        elif not self.patted:
            if self.click_count >= 3 and self.feedback("rapid_taps"):
                return
            zone = self.press_zone
            action = self.toggle_mute if self.click_count == 2 else lambda: self.click_zone(zone)
            self.after_poke = self.root.after(300, action)

    def click_zone(self, zone):
        self.after_poke = None
        if not zone or not self.feedback(zone):
            self.poke()

    def on_triple(self, e):
        if not self.skin.get("interaction_poses", {}).get("rapid_taps"):
            self.on_double(e)
            return
        self.on_double(e)
        self.click_count = 3

    def on_double(self, e):
        if self.after_poke:
            self.root.after_cancel(self.after_poke)
            self.after_poke = None
        if self.skin.get("interaction_poses", {}).get("rapid_taps"):
            self.on_press(e)
            self.click_count = 2
            return
        self.toggle_mute()

    def toggle_mute(self):
        self.after_poke = None
        self.muted = not self.muted
        if self.muted:
            self.sound.stop()
        self.say(self.line("mute_on" if self.muted else "mute_off", "静音" if self.muted else "我回来啦"), 2)

    def poke(self):
        self.after_poke = None
        self.wake()
        if not self.perform("poke"):
            self.hop(10)
            self.say(self.line("poke", "在呢！"), 3)
        if self.open:
            self.root.after(3200, lambda: self.say(f"还有 {len(self.open)} 个异常待处理，右键可体检", 3))

    def pat(self):
        self.patted = True
        self.wake()
        self.perform("pat") or self.say(self.line("pat", "嘿嘿"), 2.5)

    def on_menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="🩺 立即体检", command=lambda: self.ask("帮我全面体检一下"))
        m.add_command(label="💬 提需求…（Ctrl+Alt+Space；也可输入“换成奶蛙”）", command=self.type_request)
        m.add_cascade(label="👗 换装", menu=self.skin_menu(m))
        wm = self.wardrobe_menu(m)
        if wm:
            m.add_cascade(label="🧥 衣橱（单件搭配）", menu=wm)
        m.add_command(label="📋 诊断日志（近 7 天）", command=self.open_log)
        for ev in list(self.open.values())[:5]:
            evd = ev.get("evidence", {})
            name = evd.get("name") or evd.get("parent") or evd.get("app")
            if name:
                m.add_command(label=f"🤝 信任「{name}」的{ev.get('title', '')}，不再提醒",
                              command=lambda n=name, k=ev["kind"]: self.trust(n, k))
        m.add_separator()
        m.add_checkbutton(label="🔇 静音（双击切换）", command=lambda: self.on_double(None), onvalue=True, offvalue=False,
                          variable=tk.BooleanVar(value=self.muted))
        for label, sub in (("大", 1), ("中", 2), ("小", 3)):
            m.add_command(label=f"尺寸：{label}" + (" ✓" if self.sub == sub else ""), command=lambda s=sub: self.resize(s))
        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
            self.root.after(200, self.hide_preview)

    def skin_menu(self, parent):
        """按 group 分组；未解锁显示 🔒 和价格，点击进入兑换。"""
        sm, groups = tk.Menu(parent, tearoff=0), {}
        try:
            r = http("/skins", timeout=2)
        except OSError:
            r = None
            sm.add_command(label="（守护进程未运行）", state="disabled")
        for p in (r or {}).get("packs", []):
            label = ("🔒 " if p["locked"] else ("✓ " if p["id"] == r.get("current") else "   ")) + p["name"] + \
                    (f"  {p.get('price') or ''}" if p["locked"] else "")
            target = sm
            if p.get("group"):
                if p["group"] not in groups:
                    groups[p["group"]] = tk.Menu(sm, tearoff=0)
                    sm.add_cascade(label="🎀 " + p["group"], menu=groups[p["group"]])
                target = groups[p["group"]]
            if not p.get("bitmap", True):  # 矢量皮肤 Windows 轻量版画不出来：置灰，避免切过去后形象消失
                target.add_command(label="   " + p["name"] + "（仅 macOS）", state="disabled")
                continue
            target.add_command(label=label, command=lambda p=p: self.switch_skin(p))
            self.preview_specs.setdefault(str(target), {})[target.index("end")] = (
                "pack=" + urllib.parse.quote(p["id"]), p["name"] + (f"  🔒 {p.get('price') or ''}" if p["locked"] else ""))
        for menu in [sm] + list(groups.values()):
            menu.bind("<<MenuSelect>>", self.on_menu_select)
        sm.add_separator()
        sm.add_command(label="👤 账户与订阅…", command=self.account_prompt)
        sm.add_command(label="同步门户权益", command=self.sync_account)
        return sm

    def wardrobe_menu(self, parent):
        """模块化衣橱：按槽位列单件衣物，悬停预览“当前穿搭换上这件”的效果。"""
        try:
            w = http("/wardrobe", timeout=2)
        except OSError:
            return None
        if not w.get("modular"):
            return None
        wm = tk.Menu(parent, tearoff=0)
        for slot in w.get("slots", []):
            wm.add_command(label=f"— {slot.get('name', slot['id'])} —", state="disabled")
            for it in [i for i in w.get("items", []) if i["slot"] == slot["id"]]:
                label = ("🔒 " if it["locked"] else ("✓ " if it["equipped"] else "   ")) + it["name"] + \
                        ("（部分动作未适配）" if not it.get("complete") else "") + (f"  {it.get('price') or ''}" if it["locked"] else "")
                wm.add_command(label=label, command=lambda it=it: self.wear(it))
                self.preview_specs.setdefault(str(wm), {})[wm.index("end")] = (
                    "item=" + urllib.parse.quote(it["id"]), it["name"] + (f"  🔒 {it.get('price') or ''}" if it["locked"] else ""))
        wm.bind("<<MenuSelect>>", self.on_menu_select)
        return wm

    def wear(self, it):
        if it["locked"]:
            self.account_prompt(f"「{it['name']}」需要订阅解锁。")
            return
        try:
            http("/wardrobe", {"equip": it["id"]}, timeout=15)
            self.pcache.clear()
        except OSError as e:
            self.say(f"换装失败：{e}", 3)

    # ---------- 悬停预览 ----------
    def on_menu_select(self, e):
        try:
            idx = e.widget.index("active")
        except tk.TclError:
            idx = None
        spec = self.preview_specs.get(str(e.widget), {}).get(idx)
        if spec:
            self.show_preview(*spec)
        else:
            self.hide_preview()

    def show_preview(self, key, title):
        if key not in self.pcache:
            try:
                svg = http("/preview?" + key, timeout=3).get("svg", "")
            except OSError:
                return
            img = self.compose(re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", svg))
            self.pcache[key] = img.subsample(2) if img else None
        if self.pv is None:
            self.pv = tk.Toplevel(self.root)
            self.pv.overrideredirect(True)
            self.pv.attributes("-topmost", True)
            self.pv_img = tk.Label(self.pv, bg="white")
            self.pv_img.pack()
            self.pv_txt = tk.Label(self.pv, bg="white", fg="#444", font=("Microsoft YaHei UI" if WIN else "PingFang SC", 9))
            self.pv_txt.pack(fill="x")
        img = self.pcache.get(key)
        self.pv_img.config(image=img or "", text="" if img else "此款为矢量皮肤，预览请用 macOS 版", width=0 if img else 30)
        self.pv_txt.config(text=title)
        x, y = self.root.winfo_pointerxy()
        self.pv.update_idletasks()
        w, h = self.pv.winfo_reqwidth(), self.pv.winfo_reqheight()
        self.pv.geometry(f"+{max(0, x - w - 20)}+{max(0, y - h // 2)}")
        self.pv.deiconify()
        self.pv.lift()

    def hide_preview(self):
        if self.pv is not None:
            self.pv.withdraw()

    def switch_skin(self, p):
        if p["locked"]:
            self.account_prompt(f"「{p['name']}」需要订阅解锁。")
            return
        def run():
            try:
                http("/skin", {"pack": p["id"]}, timeout=15)
            except OSError as e:
                self.q.put(("said", f"换装失败：{e}"))
        threading.Thread(target=run, daemon=True).start()

    def account_prompt(self, prefix=""):
        """账户与订阅：门户 OAuth 登录在浏览器里完成；未配置门户时提示即将开放。"""
        try:
            st = http("/account", timeout=5)
        except OSError:
            return self.say("连不上诊所守护进程", 4)
        if not st.get("configured"):
            return self.say(prefix + ("会员订阅即将开放～" if st.get("paywall") else "测试期全部款式免费开放～"), 5)
        if st.get("logged_in"):
            try:
                r = http("/account/manage", {}, timeout=10)
                return self.say(prefix + ("已打开门户。兑换后请同步权益。" if r.get("ok") else "暂时无法打开门户账户中心。"), 5)
            except OSError:
                return self.say("连不上诊所守护进程", 4)
        try:
            r = http("/account/login", {}, timeout=10)
            self.say(prefix + ("已在浏览器打开门户登录页～" if r.get("ok") else r.get("error", "暂时无法登录")), 5)
        except OSError:
            self.say("连不上诊所守护进程", 4)

    def sync_account(self):
        def run():
            try:
                r = http("/account/refresh", {}, timeout=25)
                self.q.put(("said", r.get("msg") or "暂时无法同步权益。"))
            except OSError:
                self.q.put(("said", "连不上诊所守护进程"))
        threading.Thread(target=run, daemon=True).start()

    def open_log(self):
        try:
            import webbrowser
            webbrowser.open("file:///" + http("/history/report", {"days": 7}, timeout=10)["path"].replace("\\", "/"))
        except OSError:
            self.say("暂时打不开日志：守护进程未运行", 4)

    def resize(self, s):
        self.sub = s
        self.rescale()

    def trust(self, name, kind):
        threading.Thread(target=lambda: self.q.put(("said", http("/trust", {"name": name, "kinds": [kind]}) and
                                                          f"好的，以后不再提醒「{name}」这一项～")), daemon=True).start()

    def type_request(self):
        self.root.attributes("-topmost", False)
        text = simpledialog.askstring("赛博诊所", self.line("wake", "我在！哪里不舒服？"), parent=self.root)
        self.root.attributes("-topmost", True)
        if text:
            self.ask(text)

    def ask(self, text):
        self.say(f"收到：「{text}」\n正在体检…", 0)

        def run():
            try:
                self.q.put(("reply", http("/ask", {"text": text}, timeout=20)))
            except OSError:
                self.q.put(("reply", None))
        threading.Thread(target=run, daemon=True).start()

    # ---------- 空闲 / 睡眠 ----------
    def idle(self):
        if self.mood == "normal" and not self.dragging and not self.sleeping:
            self.perform("idle")
        self.root.after(random.randint(14000, 30000), self.idle)

    def idle_seconds(self):
        if not WIN:
            return 0
        import ctypes

        class LII(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = LII()
        li.cbSize = ctypes.sizeof(li)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
        return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000

    def check_sleep(self):
        if self.mood == "worried" and not self.dragging and self.base_mood() == "normal":
            self.set_mood("normal")  # 普通警告 60 秒后恢复常态
        idle = self.idle_seconds()
        if idle > 300 and not self.sleeping and self.mood == "normal":
            self.sleeping = True
            self.set_mood("sleep")
        elif idle < 5 and self.sleeping:
            self.wake()
        self.root.after(10000, self.check_sleep)

    def wake(self):
        if self.sleeping:
            self.sleeping = False
            self.set_mood(self.base_mood())

    # ---------- 全局快捷键（Windows：Ctrl+Alt+Space） ----------
    def hotkey_thread(self):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        if not u32.RegisterHotKey(None, 1, 0x0002 | 0x0001, 0x20):  # MOD_CONTROL|MOD_ALT, VK_SPACE
            print("[hotkey] Ctrl+Alt+Space 已被占用")
            return
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.q.put(("hotkey", None))

    # ---------- 事件同步（长轮询，空闲时阻塞） ----------
    def poll(self):
        since = 0
        while True:
            try:
                obj = http(f"/events?since={since}&wait=25", timeout=35)
            except OSError:
                self.q.put(("offline", None))
                time.sleep(3)
                continue
            since = obj.get("seq", since)
            if obj.get("skin_ver") != self.skin_ver:
                try:
                    sk = http("/skin", timeout=15)
                    self.skin_ver = obj.get("skin_ver")
                    self.q.put(("skin", sk))
                except OSError:
                    pass
            for e in obj.get("events", []):
                self.q.put(("event", e))

    def pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "on_" + kind)(data)
        except queue.Empty:
            pass
        self.root.after(400, self.pump)

    def on_offline(self, _):
        if self.online:
            self.online = False
            self.say("等待诊所守护进程启动…", 5)

    def on_skin(self, sk):
        first = not self.skin
        self.online = True
        has_bitmap = any(re.search(r"data:image/png;base64,", v or "") for v in (sk.get("layers") or {}).values())
        if not has_bitmap:  # 兜底：绝不让形象消失到无法点击
            self.say(f"「{sk.get('name')}」是矢量皮肤，Windows 版暂不支持，已切换到可显示的形象", 5)

            def fallback():
                target = self.last_pack
                if not target:  # 首次启动无“上一个”：挑第一个已解锁的图片皮肤
                    try:
                        packs = http("/skins", timeout=5).get("packs", [])
                        target = next((p["id"] for p in packs if p.get("bitmap") and not p["locked"]), None)
                    except OSError:
                        target = None
                if target:
                    http("/skin", {"pack": target}, timeout=15)
            threading.Thread(target=fallback, daemon=True).start()
            if self.imgs:
                return
            self.show_placeholder()
            return
        try:
            self.last_pack = http("/skins", timeout=3).get("current") or self.last_pack
        except OSError:
            pass
        self.load_skin(sk)
        if not first:
            self.say("换装完成～", 2)

    def on_hotkey(self, _):
        self.wake()
        self.type_request()

    def on_said(self, text):
        self.say(text, 3)

    def on_reply(self, r):
        if not r:
            self.say("连不上诊所守护进程，请先运行 启动.bat", 5)
            return
        lines = [r.get("reply", "")] + r.get("details", []) + ["• " + x for x in r.get("advice", [])]
        if r.get("note"):
            lines.append(r["note"])
        self.say("\n".join(x for x in lines if x), 9)
        self.perform("resolved" if not r.get("advice") else "alert_warn")

    def on_event(self, e):
        try:
            http("/ack", {"id": e["id"]}, timeout=3)
        except OSError:
            pass
        if not self.online:
            self.online = True
        eid, sev = e["id"], e.get("severity", "info")
        if e.get("state") == "open":
            new = eid not in self.open
            self.open[eid] = e
            if not new or sev == "info":
                return
            self.wake()
            d = e.get("diagnosis_llm") or e.get("diagnosis") or {}
            adv = "\n".join("• " + x for x in (d.get("advice") or [])[:2])
            self.set_mood("alert" if sev == "crit" else "worried")
            self.say(f"{e.get('title', '')}\n病因：{d.get('cause', '')}\n{adv}\n（常驻程序可右键设为信任）", 8)
            self.root.after(2500, lambda: self.set_mood(self.base_mood()))
        elif self.open.pop(eid, None):
            self.set_mood(self.base_mood())
            self.perform("resolved") or self.say(self.line("resolved", "恢复啦！"), 2.5)

    # ---------- 自检 ----------
    def run_selftest(self):
        t0, report = time.time(), {}
        while not self.skin and time.time() - t0 < 15:
            self.root.update()
            self.pump_once()
            time.sleep(0.1)
        report["skin"] = self.skin.get("name")
        report["bitmap_layers"] = sorted(self.imgs)
        report["layers_composited"] = {k: len(v) for k, v in self.raw.items() if len(v) > 1}
        report["window"] = [self.cv.winfo_reqwidth(), self.cv.winfo_reqheight()]
        for key in self.skin.get("motions", {}):
            self.perform(key)
            self.root.update()
        report["motions_ok"] = list(self.skin.get("motions", {}))
        snd = list(self.sound.tracks)
        report["sounds"] = snd
        if snd:  # 只生成分段、不出声：验证接续逻辑
            a = self.sound.segment(snd[0], 2.0) and self.sound.tracks[snd[0]][2]
            time.sleep(0.5)
            b = self.sound.segment(snd[0], 2.0) and self.sound.tracks[snd[0]][2]
            report["sound_resume"] = bool(a and b and b > a)
        self.on_event({"id": -1, "state": "open", "severity": "warn", "kind": "cpu_hog", "title": "自检：模拟告警",
                       "evidence": {"name": "selftest"}, "diagnosis": {"cause": "自检", "advice": ["无需处理"]}})
        self.root.update()
        report["mood_after_alert"] = self.mood
        try:  # 悬停预览：取当前皮肤自身的整套预览并合成
            self.show_preview("pack=" + urllib.parse.quote(self.skin_pack()), "自检预览")
            img = self.pcache.get("pack=" + urllib.parse.quote(self.skin_pack()))
            report["preview"] = [img.width(), img.height()] if img else "vector"
            self.hide_preview()
        except Exception as ex:
            report["preview"] = f"error: {ex}"
        report["ok"] = bool(self.imgs) and report["mood_after_alert"] == "worried"
        print(json.dumps(report, ensure_ascii=False))
        self.root.destroy()

    def skin_pack(self):
        try:
            return http("/skins", timeout=3).get("current") or "lumi"
        except OSError:
            return "lumi"

    def pump_once(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "on_" + kind)(data)
        except queue.Empty:
            pass


if __name__ == "__main__":
    app = App(selftest="--selftest" in sys.argv)
    if app.selftest:
        app.root.after(100, app.run_selftest)
    app.root.mainloop()
