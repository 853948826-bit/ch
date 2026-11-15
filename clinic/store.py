"""付费权益：统一由门户网站下发（OAuth 登录 + 套餐订阅，见 clinic/portal.py 与 docs/PORTAL_OAUTH.md），本地只验签与缓存。
活动兑换码在门户个人中心绑定账号，软件同步相同的权益清单。

权益清单 = 门户用 Ed25519 私钥签名的载荷，客户端只内置公钥（clinic/portal_entitlements_pubkey.txt）验签，防止本地篡改：
  {"payload": base64url(JSON), "sig": base64url(64 字节签名)}，签名对象为 ENT_DOMAIN + payload 原始字节。
  载荷 {"v":1,"aud":"cyber-clinic","sub":用户ID,"name":显示名,"plan":套餐ID,"plan_name":套餐名,
        "skus":["lumi.legwear.*", ...],"features":["defense", ...],"iat":签发时间,"exp":到期时间}
sku 以 ".*" 结尾表示系列通票。缓存于 ~/.cyberclinic/entitlements.json；断网时到期前（含 GRACE_S 宽限）仍有效。"""
import base64, json, os, re, tempfile, time

from . import device, ed25519

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB_PATH = os.path.join(ROOT, "clinic", "store_pubkey.txt")
ENT_PUB_PATH = os.path.join(ROOT, "clinic", "portal_entitlements_pubkey.txt")
ENT_PATH = os.path.join(os.path.expanduser("~"), ".cyberclinic", "entitlements.json")
ENT_DOMAIN = b"CCENT1\n"  # 区分权益签名用途
GRACE_S = 3 * 86400       # 到期宽限：断网或续费延迟时不立刻停用
SHARE_REWARD_SKU = "promo.naiwa_frog"
DEFAULT_PRICES = {"CNY": 14.99, "USD": 2.00}  # 单款皮肤/衣物参考价（实际以门户套餐为准）


def price_label(prices=None):
    p = prices or DEFAULT_PRICES
    return f"¥{p['CNY']:.2f} / ${p['USD']:.2f}"


def pubkey(path=None):
    try:
        with open(path or PUB_PATH, encoding="ascii") as source:
            return bytes.fromhex(source.read().strip())
    except (OSError, ValueError):
        return None


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(blob):
    """验签并返回载荷；失败抛 ValueError（中文原因）。不检查到期（由调用方按宽限期判断）。"""
    key_path = ENT_PUB_PATH if os.path.exists(ENT_PUB_PATH) else PUB_PATH
    pk = pubkey(key_path)
    if not pk:
        raise ValueError("本客户端未配置权益验签公钥")
    try:
        raw, sig = _b64d(blob["payload"]), _b64d(blob["sig"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("权益清单格式不正确")
    if len(sig) != 64 or not ed25519.verify(pk, ENT_DOMAIN + raw, sig):
        raise ValueError("权益清单签名无效")
    p = json.loads(raw)
    if not isinstance(p, dict) or p.get("v") not in (1, 2) or p.get("aud") != "cyber-clinic":
        raise ValueError("权益清单不是发给本客户端的")
    if (not isinstance(p.get("sub"), str) or not p["sub"] or
            any(not isinstance(p.get(k), list) or any(not isinstance(s, str) for s in p[k])
                for k in ("skus", "features")) or
            any(p.get(k) is not None and (type(p[k]) is not int or p[k] <= 0)
                for k in ("exp", "valid_until"))):
        raise ValueError("权益清单字段不正确")
    if p["v"] == 2:
        binding = p.get("device")
        if (not isinstance(binding, dict) or set(binding) != {"fingerprint", "public_key"}
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in binding.values())
                or type(p.get("iat")) is not int or not p.get("valid_until")
                or (p.get("exp") is None and (p.get("plan") != "free" or p["features"]))
                or not p["iat"] < p["valid_until"] <= min(p["iat"] + 120, p.get("exp") or float("inf"))):
            raise ValueError("终端权益清单格式不正确")
        try:
            local, _ = device.identity()
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise ValueError("无法校验本机授权") from exc
        if binding != local:
            raise ValueError("权益清单绑定了另一台终端")
    return p


def save(blob):
    """验签通过才写入缓存（仅当前用户可读）。"""
    p = verify(blob)
    parent = os.path.dirname(ENT_PATH)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".entitlements-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, ENT_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _CACHE["mtime"] = None
    return p


def clear():
    try:
        os.remove(ENT_PATH)
    except OSError:
        pass
    _CACHE["mtime"] = None


_CACHE = {"mtime": None, "p": None}


def current():
    """缓存的权益载荷（验签通过、未超宽限期），否则 None。按文件修改时间缓存，开销小。"""
    try:
        stat = os.stat(ENT_PATH)
        mt = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if mt != _CACHE["mtime"]:
        try:
            _CACHE["p"] = verify(json.load(open(ENT_PATH, encoding="utf-8")))
        except (OSError, ValueError):
            _CACHE["p"] = None  # 被篡改或损坏的缓存直接忽略
        _CACHE["mtime"] = mt
    p = _CACHE["p"]
    if p:
        now = time.time()
        if p.get("valid_until"):
            return p if now < p["valid_until"] else None
        if p.get("exp") and now > p["exp"] + GRACE_S:
            return None
    return p


def owned():
    """当前账号已订阅的 sku 集合（含系列通票）。"""
    p = current()
    skus = set(p.get("skus") or []) if p and p["v"] == 2 else set()
    if _active(p, "all_skins"):
        skus.add("*")
    return skus


_PAYWALL = {"mtime": None, "on": True}


def paywall():
    """config.json 的 store.paywall：false 时全部付费内容视为已解锁（测试期临时解禁，改回 true 即恢复）。"""
    p = os.path.join(ROOT, "config.json")
    try:
        mt = os.path.getmtime(p)
        if mt != _PAYWALL["mtime"]:
            with open(p, encoding="utf-8") as f:
                _PAYWALL.update(mtime=mt, on=json.load(f).get("store", {}).get("paywall", True) is not False)
    except (OSError, ValueError):
        _PAYWALL.update(mtime=None, on=True)
    return _PAYWALL["on"]


def owns(sku, have=None):
    """真实持有权益（不受测试期 paywall 开关影响）。"""
    have = owned() if have is None else have
    if sku == SHARE_REWARD_SKU:
        return sku in have
    return bool(sku) and ("*" in have or sku in have or any(s.endswith(".*") and sku.startswith(s[:-1]) for s in have))


def entitled(sku, have=None):
    if sku == SHARE_REWARD_SKU:
        return owns(sku, have)
    if not sku or not paywall():
        return True
    return owns(sku, have)


def _active(p, feature):
    return bool(p and p["v"] == 2 and p.get("exp") and feature in p["features"] and time.time() < p["exp"])


def subscription(feature):
    """功能订阅状态：{"feature","active","exp","grace","plan"}（供扩展等付费功能判断）。"""
    p = current()
    on = _active(p, feature)
    exp = p.get("exp") if on else None
    return {"feature": feature, "active": on, "exp": exp, "plan": p.get("plan") if on else None,
            "grace": False}
