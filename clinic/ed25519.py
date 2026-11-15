"""Ed25519 签名/验签（RFC 8032 参考实现，纯 Python、零依赖，Mac/Windows 通用）。
用于付费皮肤授权码：服务器用私钥签发，客户端只内置公钥验签。性能足够（单次验签约数毫秒级）。"""
import hashlib

p = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
I = pow(2, (p - 1) // 4, p)


def _sha512(b):
    return hashlib.sha512(b).digest()


def _xrecover(y):
    xx = (y * y - 1) * pow(d * y * y + 1, p - 2, p)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p:
        x = x * I % p
    return p - x if x & 1 else x


_By = 4 * pow(5, p - 2, p) % p
_B = (_xrecover(_By) % p, _By, 1, _xrecover(_By) * _By % p)
_ZERO = (0, 1, 1, 0)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    A, B = (y1 - x1) * (y2 - x2) % p, (y1 + x1) * (y2 + x2) % p
    C, D = 2 * t1 * t2 * d % p, 2 * z1 * z2 % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return E * F % p, G * H % p, F * G % p, E * H % p


def _mul(s, P):
    Q = _ZERO
    while s:
        if s & 1:
            Q = _add(Q, P)
        P, s = _add(P, P), s >> 1
    return Q


def _encode(P):
    x, y, z, _ = P
    zi = pow(z, p - 2, p)
    x, y = x * zi % p, y * zi % p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(b):
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= p:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = p - x
    P = (x, y, 1, x * y % p)
    if (-x * x + y * y - 1 - d * x * x * y * y) % p:
        raise ValueError("point not on curve")
    return P


def _expand(seed):
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed):
    a, _ = _expand(seed)
    return _encode(_mul(a, _B))


def sign(seed, msg):
    a, prefix = _expand(seed)
    A = _encode(_mul(a, _B))
    r = int.from_bytes(_sha512(prefix + msg), "little") % L
    R = _encode(_mul(r, _B))
    k = int.from_bytes(_sha512(R + A + msg), "little") % L
    return R + ((r + k * a) % L).to_bytes(32, "little")


def verify(pub, msg, sig):
    if len(sig) != 64 or len(pub) != 32:
        return False
    try:
        A, R = _decode(pub), _decode(sig[:32])
    except ValueError:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= L:
        return False
    k = int.from_bytes(_sha512(sig[:32] + pub + msg), "little") % L
    return _encode(_mul(s, _B)) == _encode(_add(R, _mul(k, A)))
