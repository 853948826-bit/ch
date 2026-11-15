"""形象客制化（皮肤包）：manifest.json + 分层 SVG + 调色板 + 台词。
智能体可经 HTTP/MCP 整包导入、单层替换、实时改色/改台词；小精灵按 ver 热更新。缺失的层回退到默认皮肤。"""
import json, os, re, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIN_DIR = os.path.join(ROOT, "skins")
DEFAULT = "lumi"
LUMI_WARDROBE = "露米配套衣橱.local"
FROG_PACKS = {"naiwa_frog", "奶蛙百科版.local"}
LAYERS = ["hair_back", "body", "arm_l", "arm_r", "face", "blush", "eyes_open", "eyes_closed", "eyes_happy",
          "eyes_worried", "eyes_surprised", "mouth_smile", "mouth_open", "mouth_cat", "mouth_worried", "mouth_o",
          "bangs", "cap", "holo",
          "ahoge", "braid", "eyes_sparkle", "eyes_wink", "mouth_grin"]  # 后 5 个可选：会摆动的呆毛/发辫、额外表情
MAX_SVG = 256 * 1024
OPTIONAL = ("ahoge", "braid", "eyes_sparkle", "eyes_wink", "mouth_grin")
# 动作原语（小精灵内置实现），皮肤用 "原语:参数" 组合成交互：{"poke": [["jiggle", "particle:👀"], [...]]}
MOTION_OPS = {"tilt": "tilt:弧度:秒 头部摆动", "wave": "挥右手", "hop": "hop:高度 跳", "squash": "落地压扁",
              "jiggle": "全身Q弹抖动", "spin": "转一圈", "side_eye": "side_eye:像素 斜眼并保持2秒(负=向左)",
              "stretch": "伸懒腰", "rub": "双手揉肚子", "holo": "holo:秒 全息屏", "particle": "particle:emoji 飘出",
              "face": "face:眼睛层:嘴巴层 换表情", "say": "say:台词键 说一句",
              "shake": "shake:秒 狂笑式颤抖", "pose": "pose:姿势名:秒 临时切换到整图姿势 pose_<名>", "collapse": "笑到脱力（瘫软后恢复）", "sound": "sound:音效名 播放皮肤自带音效",
              "boing": "呆毛弹一下", "cheer": "双手举高欢呼", "blush": "blush:秒 脸红"}
SOUND_EXT = (".aiff", ".wav", ".mp3", ".m4a", ".caf")
MOTION_KEYS = ["poke", "pat", "idle", "drag", "drop", "wake", "resolved", "alert_warn", "alert_crit"]
_HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")
POSE = re.compile(r"^pose_[A-Za-z0-9_]{1,24}$")  # 任意数量的整图姿势层（狂笑、挥手…）


def sanitize(svg):
    """只接受纯 SVG：去脚本、事件属性、外链引用（允许 #内部引用 与 data:image）。"""
    s = str(svg).strip()
    s = re.sub(r"^<\?xml[^>]*\?>\s*", "", s)
    if not s.startswith("<svg") or len(s) > MAX_SVG:
        raise ValueError("layer must be an <svg> document under 256KB")
    s = re.sub(r"<script[\s\S]*?</script\s*>", "", s, flags=re.I)
    s = re.sub(r"<foreignObject[\s\S]*?</foreignObject\s*>", "", s, flags=re.I)
    s = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", "", s, flags=re.I)
    s = re.sub(r"(?:xlink:)?href\s*=\s*([\"'])(?!#|data:image/)[^\"']*\1", "", s, flags=re.I)
    return s


def check_motions(m):
    out = {}
    for key, alts in (m or {}).items():
        if key not in MOTION_KEYS:
            raise ValueError(f"unknown motion key {key}; valid: {MOTION_KEYS}")
        clean = []
        for steps in list(alts)[:12]:
            steps = [str(x)[:40] for x in list(steps)[:8]]
            for st in steps:
                if st.split(":")[0] not in MOTION_OPS:
                    raise ValueError(f"unknown motion op {st}; valid: {list(MOTION_OPS)}")
            clean.append(steps)
        out[key] = clean
    return out


def _read_pack(path, depth=0, equipment=None):
    with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    if os.path.basename(os.path.realpath(path)) in FROG_PACKS:
        from .store import SHARE_REWARD_SKU
        man["sku"] = SHARE_REWARD_SKU
    if man.get("base") and depth < 3:  # 换装包：继承基础皮肤，只覆盖自己提供的图层/字段
        bman, blayers = _read_pack(os.path.join(SKIN_DIR, os.path.basename(man["base"])), depth + 1)
        merged = dict(bman)
        merged.update({k: v for k, v in man.items() if k != "layers"})
        own_layers = man.get("layers", {})
        man = merged
        man["layers"] = own_layers
        layers = dict(blayers)
        if man.get("keep_outfit") and "body" in own_layers:
            for name in list(layers):
                if name.startswith("pose_") or name in bman.get("whole_states", []):
                    layers.pop(name)
            man["whole_states"] = [name for name in man.get("whole_states", []) if name in own_layers]
            man["motions"] = {
                key: [kept for steps in alternatives
                      if (kept := [step for step in steps
                                  if step.split(":")[0] not in ("pose", "face")])] or [["jiggle"]]
                for key, alternatives in man.get("motions", {}).items()
            }
    else:
        layers = {}
    if man.get("modular"):  # 模块化衣橱：按当前穿搭逐姿势合成
        from . import wardrobe
        man, mod_layers = wardrobe.build(path, man, sanitize, equipment)
        layers.update(mod_layers)
    for name, fn in man.get("layers", {}).items():
        p = os.path.realpath(os.path.join(path, fn))
        if not p.startswith(os.path.realpath(path) + os.sep):
            raise ValueError(f"layer path escapes pack: {fn}")
        with open(p, encoding="utf-8") as f:
            layers[name] = sanitize(f.read())
    man["motions"] = check_motions(man.get("motions"))
    sounds = dict(man.get("sound_paths") or {})  # 换装包沿用基础皮肤已解析的音效
    own = json.load(open(os.path.join(path, "manifest.json"), encoding="utf-8")).get("sounds", {})
    for name, fn in own.items():  # 音效仅限皮肤包目录内的本机文件
        p = os.path.realpath(os.path.join(path, fn))
        if p.startswith(os.path.realpath(path) + os.sep) and p.lower().endswith(SOUND_EXT) and os.path.isfile(p):
            sounds[name] = p
    man["sound_paths"] = sounds
    return man, layers


def _lumi_outfit_actions(man, layers):
    """旧版独立袜装沿用配套衣橱中同款 SKU 的现有动作。"""
    sku = man.get("sku")
    if not (man.get("base") == DEFAULT and man.get("keep_outfit")
            and isinstance(sku, str) and sku.startswith("lumi.legwear.")):
        return man, layers
    from . import wardrobe
    path = os.path.join(SKIN_DIR, LUMI_WARDROBE)
    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as file:
            source = wardrobe.with_auto(path, json.load(file))
        slot = source.get("frame_slot")
        item_id = next((key for key, item in source.get("items", {}).items()
                        if item.get("slot") == slot and item.get("sku") == sku), None)
        if not slot or not item_id:
            return man, layers
        item = source["items"][item_id]
        poses = set(source.get("poses", {}))
        if not poses or not poses <= set(item.get("frames", {})):
            return man, layers
        rendered, action_layers = wardrobe.build(path, source, sanitize, equipment={slot: item_id})
    except (OSError, ValueError):
        return man, layers
    fields = ("viewBox", "anchors", "whole_states", "interaction_zones", "pose_interaction_zones",
              "interaction_poses", "interaction_variants", "shared_pose_names", "gaze_poses")
    return {**man, **{key: rendered[key] for key in fields if key in rendered}}, action_layers


def is_whole(man):
    """整图类皮肤：用整张图表现状态，不应混入默认矢量皮肤的任何图层。"""
    return bool(man.get("modular") or man.get("keep_outfit") or man.get("whole_states"))


def is_bitmap(pack_dir, depth=0):
    """是否图片皮肤（Windows 轻量版只能显示图片皮肤）：看常态图层是否内嵌位图；换装包看其基础皮肤。"""
    try:
        m = json.load(open(os.path.join(pack_dir, "manifest.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if m.get("base") and depth < 3:
        return is_bitmap(os.path.join(SKIN_DIR, os.path.basename(m["base"])), depth + 1)
    fn = (m.get("poses") or {}).get("normal") if m.get("modular") else (m.get("layers") or {}).get("body")
    if not fn:
        return False
    try:
        with open(os.path.join(pack_dir, fn), encoding="utf-8") as f:
            return "data:image/" in f.read(600)
    except OSError:
        return False


def _sku_categories():
    """模块化衣橱的款式分类按 sku 共享给同款的单独换装包（两边 sku 相同），换装列表与衣橱用同一套分类。"""
    names, out = {}, {}
    for d in os.listdir(SKIN_DIR):
        cp = os.path.join(SKIN_DIR, d, "wardrobe_categories.json")
        if not os.path.isfile(cp):
            continue
        try:
            cfg = json.load(open(cp, encoding="utf-8"))
            man = json.load(open(os.path.join(SKIN_DIR, d, "manifest.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in cfg.get("categories", []):
            for sub in c.get("children", []):
                names[f"{c['id']}/{sub['id']}"] = [c.get("name", c["id"]), sub.get("name", sub["id"]), len(names)]
        for iid, path in cfg.get("items", {}).items():
            sku = man.get("items", {}).get(iid, {}).get("sku")
            if sku and path in names:
                out[sku] = (path, names[path][:2], names[path][2])
    return out


def _unlisted_skus(have):
    """模块化衣橱里未上架款的 sku：同 sku 的单独换装包也一并不上架。"""
    from . import wardrobe
    out = set()
    for d in os.listdir(SKIN_DIR):
        try:
            man = json.load(open(os.path.join(SKIN_DIR, d, "manifest.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if man.get("modular"):
            out |= {man["items"][i].get("sku") for i in wardrobe.unlisted(os.path.join(SKIN_DIR, d), man, have)}
    return out


def list_packs(current=None):
    """皮肤列表（只读 manifest，开销小）：id、名称、作者、sku、价格、是否锁定、款式分类。只列已上架的款。"""
    from . import store
    have, out = store.owned(), []
    rights = store.current() or {}
    individual = set(rights.get("skus", []))
    subscription = store.subscription("all_skins")
    cats, hidden = _sku_categories(), _unlisted_skus(have)
    for d in sorted(os.listdir(SKIN_DIR)):
        mp = os.path.join(SKIN_DIR, d, "manifest.json")
        if not os.path.isfile(mp):
            continue
        try:
            m = json.load(open(mp, encoding="utf-8"))
        except ValueError:
            continue
        if m.get("sku") in hidden:
            continue
        sku = store.SHARE_REWARD_SKU if d in FROG_PACKS else m.get("sku")
        reward = sku == store.SHARE_REWARD_SKU
        locked = not store.entitled(sku, have)
        access = ("free" if not sku else "account" if store.owns(sku, individual)
                  else "locked" if locked else "subscription" if subscription["active"] else "trial")
        out.append({"id": d, "name": m.get("name") or d, "author": m.get("author"), "sku": sku,
                    "reward": reward,
                    "price": "分享奖励" if reward else m.get("price") or (store.price_label(m.get("prices")) if sku else None),
                    "price_cny": (m.get("prices", {}).get("CNY", None if m.get("price") else store.DEFAULT_PRICES["CNY"])
                                  if sku and not reward else None),
                    "access": access, "access_expires": subscription["exp"] if access == "subscription" else None,
                    "base": m.get("base"), "group": m.get("group"), "locked": locked,
                    "bitmap": is_bitmap(os.path.join(SKIN_DIR, d)),
                    "category": cats.get(sku, (None,) * 3)[0], "category_name": cats.get(sku, (None,) * 3)[1],
                    "category_rank": cats.get(sku, (None,) * 3)[2]})
    # The bundled frog and the original local import contain the same art.
    # Keep the current alias so a saved skin stays selected in existing clients.
    if FROG_PACKS.issubset({pack["id"] for pack in out}):
        duplicate = "naiwa_frog" if current == "奶蛙百科版.local" else "奶蛙百科版.local"
        out = [pack for pack in out if pack["id"] != duplicate]
    return out


class Skin:
    def __init__(self, state_path=None):
        self.lock, self.ver, self.pack_id = threading.RLock(), 1, DEFAULT
        self.base_man, self.base_layers = _read_pack(os.path.join(SKIN_DIR, DEFAULT))
        self._reset()
        self.state_path = state_path or os.path.join(ROOT, "run", "skin_current.json")
        try:
            st = json.load(open(self.state_path, encoding="utf-8"))
            self.man, self.layers = st["man"], {k: sanitize(v) for k, v in st["layers"].items()}
            self.pack_id = st.get("pack_id", DEFAULT)
            if self.pack_id == DEFAULT and self.layers.get("body") == self.base_layers.get("body"):
                self.man.setdefault("interaction_zones", self.base_man.get("interaction_zones", {}))
            if self.pack_id in FROG_PACKS or self.man.get("sku") or self.man.get("modular") or self.man.get("keep_outfit") or self.man.get("group") == "露米衣橱":
                try:
                    self.apply({"pack": self.pack_id})
                except (OSError, ValueError, PermissionError):
                    self._reset()
                    self.pack_id = DEFAULT
        except (OSError, ValueError, KeyError):
            pass

    def _reset(self):
        self.man = json.loads(json.dumps(self.base_man))
        self.layers = dict(self.base_layers)

    def apply(self, req, equipment=None):
        """req: {reset} | {pack: 名称或目录} | {palette:{}} | {layers:{name: svg}} | {lines:{}} | {name, author}"""
        with self.lock:
            if req.get("reset"):
                self._reset()
                self.pack_id = DEFAULT
            if req.get("pack"):
                from . import store
                p = req["pack"]
                path = p if os.path.isabs(p) else os.path.join(SKIN_DIR, os.path.basename(p))
                man, layers = _read_pack(path, equipment=equipment)
                if man.get("sku") and man["sku"] in _unlisted_skus(store.owned()):
                    raise ValueError(f"「{man.get('name', p)}」还没上架（动作帧未齐全）")
                if not store.entitled(man.get("sku")):
                    if man.get("sku") == store.SHARE_REWARD_SKU:
                        raise PermissionError("奶蛙百科版需在门户提交分享奖励申请，审核通过后同步权益。")
                    raise PermissionError(f"「{man.get('name', p)}」需要解锁（{man.get('price') or store.price_label(man.get('prices'))}），可在右键菜单“账户与订阅”登录门户后解锁")
                man, layers = _lumi_outfit_actions(man, layers)
                self.pack_id = os.path.basename(path)
                self._reset()
                self.man.pop("interaction_zones", None)
                self.man.update({k: v for k, v in man.items() if k != "layers"})
                self.man["palette"] = {**self.base_man.get("palette", {}), **man.get("palette", {})}
                self.layers.update(layers)
                # 可选装饰层（呆毛/发辫/额外表情）只属于提供它的皮肤，缺失时留空，不借用默认皮肤
                vw, vh = self.man.get("viewBox", [200, 250])
                empty = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw} {vh}" width="{vw}" height="{vh}"></svg>'
                # 整图类皮肤（模块化/有整图状态）：缺的图层一律留空；矢量分层皮肤只对可选装饰层留空
                for k in (LAYERS if is_whole(self.man) else OPTIONAL):
                    if k not in layers:
                        self.layers[k] = empty
            for k, v in (req.get("palette") or {}).items():
                if not _HEX.match(str(v)):
                    raise ValueError(f"palette {k} must be a hex color")
                self.man.setdefault("palette", {})[k] = v
            for k, v in (req.get("layers") or {}).items():
                if k not in LAYERS and not POSE.match(k):
                    raise ValueError(f"unknown layer {k}; valid: {LAYERS}")
                self.layers[k] = sanitize(v)
            if "motions" in req:
                self.man["motions"] = {**self.man.get("motions", {}), **check_motions(req["motions"])}
            for k, v in (req.get("lines") or {}).items():
                self.man.setdefault("lines", {})[k] = [str(x)[:60] for x in (v if isinstance(v, list) else [v])][:20]
            for k in ("name", "author"):
                if req.get(k):
                    self.man[k] = str(req[k])[:40]
            self.ver += 1
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"man": self.man, "layers": self.layers, "pack_id": self.pack_id}, f, ensure_ascii=False)
            return self.ver

    def render(self):
        with self.lock:
            pal = self.man.get("palette", {})
            sub = lambda s: re.sub(r"\{\{(\w+)\}\}", lambda m: pal.get(m.group(1), "#ff00ff"), s)
            return {"ver": self.ver, "name": self.man.get("name"), "author": self.man.get("author"),
                    "viewBox": self.man.get("viewBox", [200, 250]), "anchors": self.man.get("anchors", {}),
                    "palette": pal, "lines": self.man.get("lines", {}), "motions": self.man.get("motions", {}),
                    "show_led": self.man.get("show_led", True), "eye_track": self.man.get("eye_track", True),
                    "sounds": self.man.get("sound_paths", {}),
                    "whole_states": self.man.get("whole_states", []),
                    "interaction_zones": self.man.get("interaction_zones", {}),
                    "pose_interaction_zones": self.man.get("pose_interaction_zones", {}),
                    "interaction_poses": self.man.get("interaction_poses", {}),
                    "interaction_variants": self.man.get("interaction_variants", {}),
                    "gaze_poses": self.man.get("gaze_poses", {}),
                    "shared_pose_names": self.man.get("shared_pose_names", []),
                    "tts": bool(self.man.get("tts", False)),  # 系统语音合成，默认关闭（纯文字气泡）
                    "jiggle": self.man.get("jiggle", True),  # 软弹：false 关闭；{"strength": 系数} 调强弱
                    "layers": {k: sub(v) for k, v in self.layers.items()}}

    def current_path(self):
        return os.path.join(SKIN_DIR, os.path.basename(self.pack_id))

    def wardrobe(self):
        """当前皮肤的衣橱清单（非模块化皮肤返回 modular=False）。"""
        from . import store, wardrobe
        path = self.current_path()
        try:
            man = json.load(open(os.path.join(path, "manifest.json"), encoding="utf-8"))
        except (OSError, ValueError):
            return {"pack": self.pack_id, "modular": False}
        if not man.get("modular"):
            return {"pack": self.pack_id, "modular": False}
        return dict(wardrobe.catalog(path, man, store.owned()), pack=self.pack_id, modular=True)

    def wear(self, item=None, unequip=None):
        """穿上/脱下一件衣物（付费物品需已解锁），保存穿搭并重新合成当前皮肤。"""
        from . import store, wardrobe
        with self.lock:
            path = self.current_path()
            with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
                man = json.load(f)
            if not man.get("modular"):
                raise ValueError("当前皮肤不支持单件换装")
            if bool(item) == bool(unequip):
                raise ValueError("请选择穿上物品或卸下槽位")
            before = wardrobe.equipped(self.pack_id, man)
            eq = dict(before)
            if item:
                it = man.get("items", {}).get(item)
                if not it:
                    raise ValueError(f"没有这件衣物：{item}")
                if item in wardrobe.unlisted(path, man, store.owned()):
                    raise ValueError(f"「{it.get('name', item)}」还没上架（动作帧未齐全）")
                if not store.entitled(it.get("sku")):
                    raise PermissionError(f"「{it.get('name', item)}」需要解锁（{it.get('price') or store.price_label(it.get('prices'))}），可在右键菜单“账户与订阅”登录门户后解锁")
                eq[it["slot"]] = item
            else:
                if unequip not in eq:
                    raise ValueError(f"没有这个槽位：{unequip}")
                eq[unequip] = None
            wardrobe.save(self.pack_id, eq)
            try:
                # 整套造型槽卸下时回到同槽位免费默认款。
                return self.apply({"pack": self.pack_id})
            except (OSError, ValueError, PermissionError):
                wardrobe.save(self.pack_id, before)
                raise

    def preview(self, pack=None, item=None):
        """预览 SVG（不改变当前皮肤）：pack=整套皮肤常态；item=当前模块化皮肤换上该物品后的常态。"""
        from . import wardrobe
        path = os.path.join(SKIN_DIR, os.path.basename(pack)) if pack else self.current_path()
        man = json.load(open(os.path.join(path, "manifest.json"), encoding="utf-8"))
        if man.get("modular") and not man.get("base"):
            return wardrobe.preview(path, man, sanitize, swap=item)
        rman, layers = _read_pack(path)
        pal = {**self.base_man.get("palette", {}), **rman.get("palette", {})}
        full = {} if is_whole(rman) else dict(self.base_layers)
        full.update(layers)
        for k in OPTIONAL:
            if k not in layers:
                full.pop(k, None)
        svg = wardrobe.flat_preview(full, rman.get("viewBox", [200, 250]))
        return re.sub(r"\{\{(\w+)\}\}", lambda m: pal.get(m.group(1), "#ff00ff"), svg)

    def describe(self):
        """给智能体看的规格说明：可改哪些层、调色板键、锚点、台词键。"""
        r = self.render()
        return {"ver": r["ver"], "name": r["name"], "viewBox": r["viewBox"], "layers": LAYERS,
                "palette": r["palette"], "anchors": r["anchors"], "line_keys": list(r["lines"]),
                "motions": r["motions"], "motion_keys": MOTION_KEYS, "motion_ops": MOTION_OPS,
                "packs": sorted(d for d in os.listdir(SKIN_DIR) if os.path.isfile(os.path.join(SKIN_DIR, d, "manifest.json"))),
                "rules": "每层是同一 viewBox 的完整 <svg>，层间按坐标叠放；可用 {{调色板键}} 占位；单层≤256KB；禁止脚本与外链。"}
