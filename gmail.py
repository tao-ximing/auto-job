"""Gmail（只读）：OAuth 授权 + 读邮件。只用标准库。

凭据：credentials.json（GCP Personal mode 项目（ID 见 LOCAL.md）的 Desktop client，只用 gmail.readonly）。
Personal mode：只有创建者本人的账号能授权，不需要验证，refresh token 没有 7 天过期。
授权后 refresh token 存在 token.json（权限 600，不进 git）。

  python3 gmail.py auth      # 打印授权链接，在浏览器里同意后自动保存 token
  python3 gmail.py test      # 读取账号信息和最近 1 天的邮件数
"""
import base64, hashlib, json, os, secrets, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CREDS = os.path.join(HERE, "credentials.json")
TOKEN = os.path.join(HERE, "token.json")
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


def _client():
    d = json.load(open(CREDS))
    return d.get("installed") or d["web"]


def _post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} → {e.code}: {e.read().decode()[:300]}")


def _save_token(tok):
    fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def authorize(port=0, timeout=600):
    """本地回调 + PKCE。返回授权链接后阻塞等待浏览器回调，拿到 refresh token 存进 token.json。"""
    c = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") != state:
                self.send_response(400); self.end_headers(); return
            got.update(q)
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "授权完成，可以关掉这个页面了。" if "code" in q else f"授权失败：{q.get('error')}"
            self.wfile.write(f"<p style='font:18px sans-serif;padding:40px'>{msg}</p>".encode())

    srv = HTTPServer(("127.0.0.1", port), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("在浏览器里打开并同意：\n" + url, flush=True)
    srv.timeout = 1
    t0 = time.time()
    while not got and time.time() - t0 < timeout:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise SystemExit(f"没有拿到授权码：{got.get('error') or '超时'}")
    tok = _post(c["token_uri"], {"code": got["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
                                 "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
    if "refresh_token" not in tok:
        raise SystemExit("返回里没有 refresh_token")
    _save_token({"refresh_token": tok["refresh_token"], "scope": tok.get("scope"), "created": int(time.time())})
    print("token 已保存到 token.json，权限：", tok.get("scope"))


_access = {"token": None, "exp": 0}


def access_token():
    if _access["token"] and time.time() < _access["exp"] - 60:
        return _access["token"]
    if not os.path.exists(TOKEN):
        raise RuntimeError("还没授权：运行 python3 gmail.py auth")
    c, t = _client(), json.load(open(TOKEN))
    r = _post(c["token_uri"], {"client_id": c["client_id"], "client_secret": c["client_secret"],
                               "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    _access.update(token=r["access_token"], exp=time.time() + r.get("expires_in", 3600))
    return _access["token"]


def get(path, **params):
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token()})
    return json.load(urllib.request.urlopen(req, timeout=20))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "auth":
        authorize()
    elif cmd == "test":
        p = get("profile")
        r = get("messages", q="newer_than:1d", maxResults=100)
        print(f"Gmail 连接正常：historyId {p['historyId']}，邮件总数 {p['messagesTotal']}，最近 1 天 {r.get('resultSizeEstimate', 0)} 封")
