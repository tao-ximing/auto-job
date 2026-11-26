"""Web Push：给订阅了的手机（iPhone 主屏幕 PWA / Android Chrome）发通知。不依赖第三方推送库。

- VAPID 密钥（RFC 8292）：首次使用时生成，存在 push_keys.json（权限 600，不进 git）。
- 消息加密：RFC 8291 aes128gcm。推送经过 Apple / Google 的推送服务器中转，但它们看不到内容。
- 订阅存在 jobs.db 的 push_subscriptions 表；推送服务返回 404/410（订阅失效）就删掉。

  python3 push.py test "标题" "内容"     # 给所有订阅发一条测试通知
"""
import base64, json, os, struct, sys, time, urllib.error, urllib.parse, urllib.request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "push_keys.json")
# VAPID 的联系方式（sub）。Apple 要求是 mailto: 或 https:，用 tailnet 上这个网页的地址，不放个人邮箱。
# 优先读环境变量 AUTO_JOB_PUSH_SUBJECT，否则读 push_keys.json 里的 "subject"（不进 git）。
def _subject():
    if os.environ.get("AUTO_JOB_PUSH_SUBJECT"):
        return os.environ["AUTO_JOB_PUSH_SUBJECT"]
    try:
        with open(KEY_FILE) as f:
            return json.load(f).get("subject") or "https://localhost"
    except OSError:
        return "https://localhost"


SUBJECT = _subject()


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_public(pub):
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def keys():
    """(私钥对象, 公钥 base64url)。没有就生成。"""
    if not os.path.exists(KEY_FILE):
        priv = ec.generate_private_key(ec.SECP256R1())
        pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"private_pem": pem}, f)
    priv = serialization.load_pem_private_key(json.load(open(KEY_FILE))["private_pem"].encode(), None)
    return priv, b64u(_raw_public(priv.public_key()))


def vapid_header(endpoint):
    priv, pub = keys()
    u = urllib.parse.urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": SUBJECT}).encode())
    r, s = decode_dss_signature(priv.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={pub}"


def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291 aes128gcm，单个 record。"""
    ua_pub_raw, auth_secret = unb64u(p256dh), unb64u(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_raw)
    as_priv = ec.generate_private_key(ec.SECP256R1())
    as_pub_raw = _raw_public(as_priv.public_key())
    shared = as_priv.exchange(ec.ECDH(), ua_pub)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_pub_raw + as_pub_raw, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02 = 最后一个 record 的分隔符
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub_raw)]) + as_pub_raw + ct


def send_one(sub, message: dict, ttl=86400, urgency="normal"):
    """返回 HTTP 状态码。"""
    body = encrypt(json.dumps(message, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "Authorization": vapid_header(sub["endpoint"]), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": urgency})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410):
            print(f"[push] {urllib.parse.urlparse(sub['endpoint']).netloc} → {e.code} {e.read()[:200]!r}", file=sys.stderr)
        return e.code


def send_all(conn, title, body, url="/", urgency="normal"):
    """给所有订阅发；失效的订阅删掉。返回 (成功数, 总数)。"""
    subs = [dict(r) for r in conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions")]
    ok, gone = 0, []
    for s in subs:   # 网络请求期间不碰数据库，免得一直占着写锁
        try:
            code = send_one(s, {"title": title, "body": body, "url": url}, urgency=urgency)
        except Exception as e:
            print(f"[push] 发送失败: {e}", file=sys.stderr)
            continue
        if code in (404, 410):
            gone.append(s["endpoint"])
        elif 200 <= code < 300:
            ok += 1
    if gone:
        conn.executemany("DELETE FROM push_subscriptions WHERE endpoint=?", [(e,) for e in gone])
        conn.commit()
    return ok, len(subs)


# 只接受浏览器厂商的推送服务地址，别让人登记任意 URL（服务器会往那里发请求）
PUSH_HOSTS = ("fcm.googleapis.com", ".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")


def valid_endpoint(url):
    u = urllib.parse.urlparse(url or "")
    h = (u.hostname or "").lower()
    return u.scheme == "https" and any(h == p.lstrip(".") or h.endswith(p) if p.startswith(".") else h == p for p in PUSH_HOSTS)


if __name__ == "__main__":
    from db import connect
    if sys.argv[1:2] == ["test"]:
        title = sys.argv[2] if len(sys.argv) > 2 else "测试通知"
        body = sys.argv[3] if len(sys.argv) > 3 else "推送正常"
        print("成功 %d / 共 %d 个订阅" % send_all(connect(), title, body))
    else:
        print("公钥:", keys()[1])
