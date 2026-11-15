"""Local device identity for portal subscription leases."""
import hashlib
import hmac
import os
import plistlib
import secrets
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from . import ed25519

KEY_PATH = Path.home() / ".cyberclinic" / "device.key"


@lru_cache(maxsize=1)
def machine_id():
    if sys.platform.startswith("linux"):
        value = Path("/etc/machine-id").read_text().strip()
    elif sys.platform == "darwin":
        result = subprocess.check_output(
            ["/usr/sbin/ioreg", "-a", "-rd1", "-c", "IOPlatformExpertDevice"], timeout=3,
        )
        value = plistlib.loads(result)[0]["IOPlatformUUID"]
    elif sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value = winreg.QueryValueEx(key, "MachineGuid")[0]
    else:
        raise OSError("平台不支持设备绑定")
    if not isinstance(value, str) or len(value) < 16 or not value.strip("0- "):
        raise ValueError("无法识别本机设备")
    return value.lower().encode()


def installation_key():
    path = Path(KEY_PATH)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    raw = path.read_bytes()
    if len(raw) != 32:
        raise ValueError("本机设备密钥无法读取")
    return raw


@lru_cache(maxsize=4)
def _derive(machine, local):
    fingerprint = hmac.digest(b"cyber-clinic/device-fingerprint/v1", machine, hashlib.sha256).hex()
    seed = hmac.digest(local, b"cyber-clinic/device-signing/v1:" + machine, hashlib.sha256)
    return fingerprint, ed25519.public_key(seed).hex(), seed


def identity():
    fingerprint, public_key, seed = _derive(machine_id(), installation_key())
    return {"fingerprint": fingerprint, "public_key": public_key}, seed
