"""模块化衣橱：角色每个姿势一张“身体图”，每件衣物在每个姿势一张透明叠加图，按当前穿搭逐姿势合成。
换装后做任何动作都使用同一套穿搭的对应姿势，不会切回基础款。规范见 docs/WARDROBE.md。

manifest（modular: true）：
  poses:  {姿势: 身体图文件}，姿势名 normal/happy/worried/alert/sleep 对应情绪，其余作为 pose_<名>
  slots:  [{id, name, z}]，z 越大越靠上
  items:  {物品ID: {slot, name, default?, sku?, price?, layers: {姿势: 叠加图文件}}}
穿搭状态保存在 run/wardrobe/<皮肤包>.json。"""
import json, os, re, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "run", "wardrobe")
POSE_LAYER = {"normal": "body", "happy": "eyes_happy", "worried": "eyes_worried", "alert": "eyes_surprised",
              "sleep": "eyes_closed"}
PREVIEW_ORDER = ["hair_back", "body", "arm_l", "arm_r", "face", "blush", "eyes_open", "mouth_smile", "braid", "bangs",
                 "ahoge", "cap"]


def layer_of(pose):
    return POSE_LAYER.get(pose, "pose_" + pose)


def _inner(svg):
    """去掉外层 <svg> 标签，只留内容，便于同 viewBox 的多张图层叠合成。"""
    s = re.sub(r"^\s*(<\?xml[^>]*\?>)?\s*<svg[^>]*>", "", svg, count=1)
    return re.sub(r"</svg>\s*$", "", s)


def _head(vb):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb[0]} {vb[1]}" width="{vb[0]}" height="{vb[1]}">'


def _read(path, fn, sanitize):
    p = os.path.realpath(os.path.join(path, fn))
    if not p.startswith(os.path.realpath(path) + os.sep):
        raise ValueError(f"文件路径越出皮肤包：{fn}")
    with open(p, encoding="utf-8") as f:
        return sanitize(f.read())


def unlisted(path, man, have):
    """未上架的付费款：动作帧（含自动合成帧）没覆盖全部姿势，换装后做某些动作会露出基础款腿脚。
    已真实购买的款保留，不因下架而失效。"""
    from . import store
    man = with_auto(path, man)
    poses = set(man.get("poses", {}))
    out = set()
    for iid, it in man.get("items", {}).items():
        cov = (set(it.get("frames", {})) | set(it.get("layers", {}))) - set(it.get("pose_aliases", {}))
        if it.get("sku") and not it.get("default") and not poses <= cov and not store.owns(it["sku"], have):
            out.add(iid)
    return out


def equipped(pack_id, man):
    from . import store
    try:
        with open(os.path.join(STATE_DIR, pack_id + ".json"), encoding="utf-8") as f:
            cur = json.load(f)
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError):
        cur = {}
    items = man.get("items", {})
    have = store.owned()
    hidden = unlisted(os.path.join(ROOT, "skins", pack_id), man, have)
    out = {}
    for slot in man.get("slots", []):
        sid = slot["id"]
        want = cur.get(sid)
        if isinstance(want, str) and want in items and items[want]["slot"] == sid and want not in hidden \
                and store.entitled(items[want].get("sku"), have):
            out[sid] = want
        elif sid in cur and want is None and sid != man.get("frame_slot"):
            out[sid] = None
        else:  # 未设置/失效 → 该槽位的默认物品（没有默认就空着）
            out[sid] = next((k for k, v in items.items() if v["slot"] == sid and v.get("default")
                             and store.entitled(v.get("sku"), have)), None)
    return out


def save(pack_id, eq):
    os.makedirs(STATE_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=STATE_DIR, encoding="utf-8", delete=False) as f:
        try:
            json.dump(eq, f, ensure_ascii=False)
            f.close()
            os.replace(f.name, os.path.join(STATE_DIR, pack_id + ".json"))
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)


def compose(path, man, eq, sanitize, poses=None):
    """按穿搭合成。衣物缺少动作帧时，使用同一穿搭的常态图或眼神。"""
    vb = man.get("viewBox", [200, 250])
    z = {s["id"]: s.get("z", 0) for s in man.get("slots", [])}
    items = man.get("items", {})
    frame_slot = man.get("frame_slot")
    frame_item = items.get(eq.get(frame_slot), {}) if frame_slot else {}
    frames = frame_item.get("frames", {})
    worn = sorted((items[i] for slot, i in eq.items() if i and slot != frame_slot),
                  key=lambda item: z.get(item["slot"], 0))
    base = man.get("poses", {})
    eye_paths = man.get("eye_frames", {}).get(frame_item.get("eye_anchor"), {})
    cached = {}

    def inner(file):
        if file not in cached:
            cached[file] = _inner(_read(path, file, sanitize))
        return cached[file]

    out = {}
    for pose in dict.fromkeys([*base, *frames]):
        if poses is not None and pose not in poses:
            continue
        # 回到同一套衣物的常态帧，避免把常态衣物贴到动作中的错误位置。
        layer_pose = "normal" if any(pose not in item.get("layers", {}) for item in worn) else pose
        if frames and layer_pose not in frames:
            layer_pose = "normal"
        parts = [inner(frames.get(layer_pose) or base[layer_pose])]
        for item in worn:
            file = item.get("layers", {}).get(layer_pose)
            if file:
                parts.append(inner(file))
        if frames and pose != layer_pose and pose in man.get("pose_eyes", {}):
            eye = eye_paths.get(man["pose_eyes"][pose])
            if eye:
                parts.append(inner(eye))
        out[layer_of(pose)] = sanitize(_head(vb) + "".join(parts) + "</svg>")
    return out


def with_auto(path, man):
    """合并 tools/frame_adapt 生成的 auto_frames.json：只给各款补缺失的姿势帧，manifest 里的真实帧始终优先。"""
    try:
        with open(os.path.join(path, "auto_frames.json"), encoding="utf-8") as f:
            auto = json.load(f).get("frames", {})
    except (OSError, ValueError):
        return man
    items = dict(man.get("items", {}))
    for iid, frames in auto.items():
        if iid in items and isinstance(frames, dict):
            it = dict(items[iid])
            it["frames"] = {**frames, **it.get("frames", {})}
            items[iid] = it
    return {**man, "items": items}


def build(path, man, sanitize, equipment=None):
    """供 skin._read_pack 调用：把模块化皮肤合成为普通皮肤的图层与字段。"""
    man = with_auto(path, man)
    pack_id = os.path.basename(path)
    eq = equipped(pack_id, man) if equipment is None else equipment
    layers = compose(path, man, eq, sanitize)
    man = dict(man)
    if slot := man.get("frame_slot"):
        item = man.get("items", {}).get(eq.get(slot), {})
        frames = set(item.get("frames", {}))
        exact = frames - set(item.get("pose_aliases", {}))
        man["shared_pose_names"] = sorted(set(man.get("poses", {})) - exact)
        if "interaction_zones" in item:
            man["interaction_zones"] = item["interaction_zones"]
        if "pose_interaction_zones" in item:
            man["pose_interaction_zones"] = item["pose_interaction_zones"]
        if variants := item.get("interaction_variants"):
            man["interaction_variants"] = variants
            man["interaction_poses"] = {**man.get("interaction_poses", {}),
                                        **{key: choices[0] for key, choices in variants.items()}}
        if idle := item.get("idle_poses"):
            motions = man.get("motions", {})
            man["motions"] = {**motions, "idle": [[f"pose:{pose}:3"] for pose in idle]}
        if not item.get("eye_anchor"):
            man["gaze_poses"] = {direction: pose for direction, pose in man.get("gaze_poses", {}).items()
                                 if pose in frames}
    man["whole_states"] = sorted(v for k, v in POSE_LAYER.items() if k != "normal" and k in man.get("poses", {}))
    man.setdefault("show_led", False)
    man.setdefault("eye_track", False)
    return man, layers


def catalog(path, man, have):
    """衣橱清单：每件物品的槽位、价格、是否锁定、是否穿着、姿势覆盖情况。"""
    from . import store
    man = with_auto(path, man)
    eq = equipped(os.path.basename(path), man)
    poses = set(man.get("poses", {}))
    hidden = unlisted(path, man, have)  # 只上架动作帧齐全的款
    items = []
    for iid, it in man.get("items", {}).items():
        if iid in hidden:
            continue
        cov = (set(it.get("frames", {})) | set(it.get("layers", {}))) - set(it.get("pose_aliases", {}))
        required = set(man.get("required_outfit_poses", poses)) if it.get("frames") else poses
        items.append({"id": iid, "slot": it["slot"], "name": it.get("name", iid), "sku": it.get("sku"),
                      "price": it.get("price") or (store.price_label(it.get("prices")) if it.get("sku") else None), "locked": not store.entitled(it.get("sku"), have),
                      "equipped": eq.get(it["slot"]) == iid, "default": bool(it.get("default")),
                      "complete": required <= cov, "missing_poses": sorted(required - cov),
                      "exact_poses": sorted((poses | set(it.get("frames", {}))) & cov),
                      "shared_poses": sorted(poses - cov)})
    tree = categorize(path, items)
    return {"slots": man.get("slots", []), "items": items, "equipped": eq, "categories": tree}


def categorize(path, items):
    """按 wardrobe_categories.json 给衣物标注“大类/子类”，返回带数量的分类树；未登记的款归入“未分类”。"""
    try:
        with open(os.path.join(path, "wardrobe_categories.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    tree = [{"id": c["id"], "name": c.get("name", c["id"]), "count": 0,
             "children": [{"id": s["id"], "name": s.get("name", s["id"]), "count": 0} for s in c.get("children", [])]}
            for c in cfg.get("categories", [])]
    index = {(c["id"], s["id"]): (c, s) for c in tree for s in c["children"]}
    other = {"id": "other", "name": "未分类", "count": 0, "children": [{"id": "other", "name": "未分类", "count": 0}]}
    for it in items:
        cid, _, sid = str(cfg.get("items", {}).get(it["id"], "")).partition("/")
        c, sub = index.get((cid, sid), (other, other["children"][0]))
        c["count"] += 1
        sub["count"] += 1
        it["category"] = f"{c['id']}/{sub['id']}"
        it["category_name"] = [c["name"], sub["name"]]
    if other["count"]:
        tree.append(other)
    return tree


def preview(path, man, sanitize, swap=None):
    """常态预览：当前穿搭（可临时换上 swap 物品，不保存）。"""
    man = with_auto(path, man)
    eq = dict(equipped(os.path.basename(path), man))
    if swap:
        item = man.get("items", {}).get(swap)
        if not item:
            raise ValueError(f"没有这件衣物：{swap}")
        eq[item["slot"]] = swap
    return compose(path, man, eq, sanitize, poses={"normal"}).get("body", "")


def flat_preview(layers, vb):
    """普通皮肤的常态预览：按渲染顺序把各层叠成一张 SVG。"""
    return _head(vb) + "".join(_inner(layers[k]) for k in PREVIEW_ORDER if layers.get(k)) + "</svg>"
