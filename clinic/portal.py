"""门户网站对接：OAuth 2.0 授权码 + PKCE 登录（桌面应用标准做法），登录后拉取签名权益清单（clinic/store.py 验签）。
对接契约见 docs/PORTAL_OAUTH.md。门户地址在 config.json 的 portal.base_url，未填写时为“订阅即将开放”状态。

流程：POST /account/login → 浏览器打开门户授权页 → 门户回跳 http://127.0.0.1:<port>/oauth/callback?code&state
     → 用 code + code_verifier 换令牌 → GET 权益接口 → 验签缓存。令牌存 ~/.cyberclinic/account.json（仅当前用户可读）。
只依赖标准库；令牌与权益不写日志、不发给门户以外的任何地方。"""
import base64, hashlib, json, os, secrets, tempfile, threading, time, urllib.error, urllib.parse, urllib.request

from . import AI_DEFENSE_INCLUDED, device, ed25519, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT = os.path.join(os.path.expanduser("~"), ".cyberclinic", "account.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
DEFAULTS = {"base_url": "", "client_id": "cyber-clinic-desktop", "authorize_path": "/oauth/authorize",
            "token_path": "/oauth/token", "revoke_path": "/oauth/revoke", "entitlements_path": "/api/v1/entitlements",
            "account_url": "/account", "scope": "openid profile entitlements", "refresh_hours": 6,
            "resource": "", "issuer": "", "checkout_path": ""}
_lock, _pending, _state = threading.RLock(), {}, {"error": None, "fetched": None, "attempt": 0}


def cfg():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as source:
            c = json.load(source).get("portal") or {}
    except (OSError, ValueError):
        c = {}
    if not isinstance(c, dict):
        c = {}
    out = {**DEFAULTS, **{k: v for k, v in c.items() if k in DEFAULTS}}
    out["base_url"] = (out["base_url"] or "").rstrip("/")
    for key in ("authorize_path", "token_path", "revoke_path", "entitlements_path", "account_url"):
        path = out[key]
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("门户接口必须使用站内路径")
    checkout = out["checkout_path"]
    if checkout and (not isinstance(checkout, str) or not checkout.startswith("/") or checkout.startswith("//") or "\\" in checkout):
        raise ValueError("收银台必须使用门户站内路径")
    return out


def configured():
    try:
        url = urllib.parse.urlsplit(cfg()["base_url"])
        return bool(url.hostname and not (url.username or url.password or url.query or url.fragment or url.path)
                    and (url.scheme == "https" or (url.scheme == "http" and url.hostname == "127.0.0.1")))
    except (TypeError, ValueError, AttributeError):
        return False


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _read():
    try:
        with open(ACCOUNT, encoding="utf-8") as source:
            data = json.load(source)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    os.makedirs(os.path.dirname(ACCOUNT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".account-", dir=os.path.dirname(ACCOUNT))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, ACCOUNT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def begin(port):
    """生成授权地址（PKCE S256 + state，10 分钟有效）。"""
    if not configured():
        raise ValueError("会员订阅即将开放：门户地址尚未配置")
    c = cfg()
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
    with _lock:
        now = time.time()
        _pending.clear()
        _state["error"] = None
        redirect = f"http://127.0.0.1:{port}/oauth/callback"
        _pending[state] = {"verifier": verifier, "t": now, "redirect": redirect, "config": c}
    q = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": redirect,
         "scope": c["scope"], "state": state, "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
         "code_challenge_method": "S256"}
    if c["resource"]:
        q["resource"] = c["resource"]
    return c["base_url"] + c["authorize_path"] + "?" + urllib.parse.urlencode(q)


def browser_unavailable():
    with _lock:
        _pending.clear()
        _state["error"] = "无法打开系统浏览器，请检查默认浏览器后重试"
        return _state["error"]


def _login_failed(message):
    _state["error"] = message
    return False, message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def _post_form(url, data, timeout=10):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with _opener.open(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}


def _binding(c):
    return [c[k] for k in ("base_url", "client_id", "resource", "issuer")]


def _save_tokens(t, c, prev=None):
    d = {"access_token": t["access_token"], "refresh_token": t.get("refresh_token") or (prev or {}).get("refresh_token"),
         "expires_at": time.time() + max(1, int(t.get("expires_in") or 3600) - 30), "token_type": t.get("token_type", "Bearer"),
         "subject": (prev or {}).get("subject"), "name": (prev or {}).get("name"),
         "binding": _binding(c), "wallet_pending": (prev or {}).get("wallet_pending")}
    _write(d)
    return d


def callback(params):
    """处理门户回跳：校验 state，用授权码换令牌，再拉权益。返回 (ok, 说明)。"""
    with _lock:
        p = _pending.pop(params.get("state", ""), None)
        c = cfg()
        if not p:
            return False, "登录请求已失效，请从桌宠重新登录"
        if time.time() - p["t"] > 600 or p["config"] != c:
            return _login_failed("登录请求已失效，请从桌宠重新登录")
        if params.get("error"):
            return _login_failed("门户授权未完成，可从桌宠重新登录")
        if not params.get("code") or (c["issuer"] and params.get("iss") != c["issuer"]):
            return _login_failed("门户授权回调校验失败，请从桌宠重新登录")
        _state["exchanging"] = True
        try:
            form = {"grant_type": "authorization_code", "code": params["code"], "redirect_uri": p["redirect"],
                    "client_id": c["client_id"], "code_verifier": p["verifier"]}
            if c["resource"]:
                form["resource"] = c["resource"]
            tokens = _post_form(c["base_url"] + c["token_path"], form)
            blob = _fetch(c, tokens["access_token"])
            account = store.verify(blob)
            tokens.update(subject=account["sub"], name=account.get("name") or account["sub"])
            store.clear()
            _save_tokens(tokens, c, tokens)
            try:
                _commit(blob)
            except (OSError, ValueError, KeyError, TypeError):
                logout(remote=False)
                raise
        except (OSError, ValueError, KeyError, TypeError):
            return _login_failed("换取凭证或同步权益失败，请重新登录")
        finally:
            _state["exchanging"] = False
        _state.pop("wallet_quote", None)
        _sync_wallet(c, tokens["access_token"], account["sub"])
        return True, blob.get("device_notice") or "登录成功，权益已同步"


def _token():
    d, c = _read(), cfg()
    if not d.get("access_token"):
        return None
    if (c["resource"] or d.get("binding")) and d.get("binding") != _binding(c):
        logout(remote=False)
        return None
    if time.time() >= d.get("expires_at", 0):
        if not d.get("refresh_token"):
            logout(remote=False)
            return None
        try:
            form = {"grant_type": "refresh_token", "refresh_token": d["refresh_token"], "client_id": c["client_id"]}
            if c["resource"]:
                form["resource"] = c["resource"]
            d = _save_tokens(_post_form(c["base_url"] + c["token_path"], form), c, d)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):  # 刷新令牌失效：需要重新登录
                logout(remote=False)
            return None
        except OSError:
            return None
    return d["access_token"]


def refresh():
    """拉取并验签权益清单。断网时保留旧缓存（到期前仍有效）。返回 (ok, 说明)。"""
    with _lock:
        _state["attempt"] = time.time()
        if not configured():
            return False, "门户地址尚未配置"
        try:
            tok, c = _token(), cfg()
            if not tok:
                return _login_failed("无法续用门户凭证，请检查连接或重新登录")
            blob = _fetch(c, tok)
            saved, account = _read(), store.verify(blob)
            if saved.get("subject") and account["sub"] != saved["subject"]:
                logout(remote=False)
                return _login_failed("门户账号校验失败，请重新登录")
            name = account.get("name") or account["sub"]
            if saved.get("subject") != account["sub"] or saved.get("name") != name:
                _write({**saved, "subject": account["sub"], "name": name})
            p = _commit(blob)
            _sync_wallet(c, tok, p["sub"])
            notice = blob.get("device_notice")
            return (False, notice) if notice else (True, f"已同步：{p.get('plan_name') or p.get('plan') or '免费'}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                logout(remote=False)
                msg = "门户登录已失效，请重新登录"
            elif e.code == 404:
                store.clear()
                msg = "门户权益暂不可用，请稍后重试"
            else:
                msg = f"门户返回 HTTP {e.code}"
        except (OSError, ValueError, KeyError, TypeError):
            msg = "同步权益失败，请检查连接或重新登录"
        _state["attempt"] = time.time()
        return _login_failed(msg)


def _request(c, token, path, data=None, timeout=10):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(c["base_url"] + path,
                                 data=None if data is None else json.dumps(data).encode(), headers=headers)
    with _opener.open(req, timeout=timeout) as response:
        return json.load(response)


def _fetch(c, token):
    def request(path, data=None):
        return _request(c, token, path, data)
    path = c["entitlements_path"]
    blob = request(path)
    if not blob.get("device_required"):
        return blob
    account = store.verify(blob)
    endpoint = path.rsplit("/", 1)[0]
    try:
        binding, seed = device.identity()
        challenge = request(endpoint + "/device-challenge", binding)
        message = json.loads(challenge["message"])
        if (message.get("protocol"), message.get("id"), message.get("issuer"), message.get("sub"),
                message.get("fingerprint"), message.get("public_key")) != (
                "ch-device-v1", challenge["id"], c["base_url"], account["sub"],
                binding["fingerprint"], binding["public_key"]):
            raise ValueError("设备挑战与账号不一致")
        return request(endpoint + "/device-entitlements", {
            "id": challenge["id"], "signature": _b64(ed25519.sign(seed, challenge["message"].encode())),
        })
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 409):
            raise
        blob["device_notice"] = ("账号已登录。权益已绑定另一台终端，或正在等待换机生效。"
                                 if exc.code == 409 else "账号已登录。当前终端的授权已撤销或失效。")
        return blob


def _sync_wallet(c, token, subject):
    if AI_DEFENSE_INCLUDED:
        from .defense_wallet import sync
        sync(c, token, subject)


def _commit(blob):
    payload = store.save(blob)
    now = time.time()
    _state.update(error=blob.get("device_notice"), fetched=now, attempt=now)
    return payload


def logout(remote=True):
    with _lock:
        d, c = _read(), cfg()
        if remote and d.get("refresh_token") and configured() and (d.get("binding") == _binding(c) or not (d.get("binding") or c["resource"])):
            try:
                _post_form(c["base_url"] + c["revoke_path"], {"token": d["refresh_token"], "client_id": c["client_id"]}, timeout=5)
            except (OSError, ValueError):
                pass
        try:
            os.remove(ACCOUNT)
        except FileNotFoundError:
            pass
        store.clear()
        _pending.clear()
        _state.pop("wallet", None)
        _state.pop("wallet_quote", None)
        _state.update(error=None, fetched=None, attempt=0)


def due():
    """是否该定期刷新权益（守护进程慢巡逻调用）。"""
    now = time.time()
    if not configured() or not _read().get("access_token") or now - _state["attempt"] < 30:
        return False
    current = store.current()
    return (not current or (current.get("v") == 2 and current["valid_until"] - now <= 90)
            or now - (_state["fetched"] or 0) >= cfg()["refresh_hours"] * 3600)


def status():
    p, ready = store.current(), configured()
    c = cfg() if ready else DEFAULTS
    saved = _read() if ready else {}
    signed_in = bool(saved.get("access_token") and saved.get("binding") == _binding(c))
    if ready and (not signed_in or (p and saved.get("subject") and p["sub"] != saved["subject"])):
        p = None
    user = (saved.get("name") or saved.get("subject")) if signed_in else None
    pending = next(iter(_pending.copy().values()), None)
    waiting = bool(_state.get("exchanging") or (pending and time.time() - pending["t"] <= 600))
    error = _state["error"]
    if pending and not waiting:
        error = "登录请求已过期，请从桌宠重新登录"
    wallet = _state.get("wallet")
    return {"configured": ready, "logged_in": signed_in, "login_pending": waiting,
            "subject": (p or {}).get("sub") or (saved.get("subject") if signed_in else None),
            "user": (p or {}).get("name") or user or (p or {}).get("sub"), "plan": (p or {}).get("plan"),
            "plan_name": (p or {}).get("plan_name"), "skus": (p or {}).get("skus", []), "features": (p or {}).get("features", []),
            "expires": (p or {}).get("exp"), "fetched": _state["fetched"], "error": error,
            "wallet": wallet[1] if signed_in and wallet and wallet[0] == saved.get("subject") else None,
            "paywall": store.paywall(), "manage_url": (c["base_url"] + c["account_url"]) if ready else None}
