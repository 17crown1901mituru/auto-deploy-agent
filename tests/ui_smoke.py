"""画面(frontend/index.html)の動作確認。実際のChromiumで、スマホ幅の画面を操作する。

構成: Chromium → Flask製の「ブリッジAPIの写し」(server.pyと同じ経路。中身は termux-bridge/core.py)
                 → 偽のGCPバックエンド（ログイン・解析・デプロイ・権限付与の偽物）
Googleのログイン画面そのものは出せないので、ログインの受け渡し（ブラウザ→ポーリング→セッション）だけを偽物で再現する。
テスト専用の依存: flask, playwright（Termux本体には不要）
実行: python3 tests/ui_smoke.py   （リポジトリのルートで。スクリーンショットは /tmp/shots に出る）
"""
import hashlib
import importlib.util
import base64
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHOTS = Path(os.environ.get("SHOTS_DIR", "/tmp/shots"))
SHOTS.mkdir(parents=True, exist_ok=True)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def serve(app):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


# --- 偽のGCP（結合テストと同じもの）＋ログインの偽物
termux_tests = load("termux_tests", HERE / "test_termux_core.py")
SESSION = termux_tests.SESSION
gcp_app = termux_tests.build_stub()
logins = {}


@gcp_app.after_request
def cors(resp):   # アプリ(別オリジン)から直接 /auth/poll を呼ぶため
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "*"
    return resp


@gcp_app.get("/auth/login")
def fake_login():
    # 本物は Google の画面を経由する。ここでは「ログインできた」状態にするだけ
    logins[request.args["id"]] = request.args["c"]
    return "<h1>ログインできました</h1>"


@gcp_app.route("/auth/poll", methods=["POST", "OPTIONS"])
def fake_poll():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json()
    challenge = logins.get(body["id"])
    if not challenge:
        return jsonify(status="invalid")
    # 本物と同じ検証: verifier の SHA-256(base64url) が、最初に渡された challenge と一致すること
    digest = base64.urlsafe_b64encode(hashlib.sha256(body["verifier"].encode()).digest()).rstrip(b"=").decode()
    if digest != challenge:
        return jsonify(status="invalid")
    return jsonify(status="ready", session=SESSION, email="owner@example.com")


gcp_srv, gcp_url = serve(gcp_app)

tmp = tempfile.mkdtemp()
os.environ.update({
    "AUTO_DEPLOY_HOME": f"{tmp}/home", "WORKSPACE_DIR": f"{tmp}/ws", "LOG_DIR": f"{tmp}/logs",
    "DB_PATH": f"{tmp}/home/jobs.db", "GCP_BACKEND_URL": "",
    "START_TIMEOUT": "15", "BRIDGE_TOKEN": "ui-token", "GRANT_POLL_SECONDS": "0.3",
})
core = load("bridge_core_ui", ROOT / "termux-bridge" / "core.py")
core.init()
core.load_token()

# --- server.py と同じ経路を持つ Flask 版
api = Flask(__name__)


@api.errorhandler(core.JobError)
def job_error(e):
    return jsonify(detail=str(e)), e.status


@api.before_request
def auth():
    if request.path in ("/", "/ui"):
        return None
    if not core.check_token(request.headers.get("X-Bridge-Token")):
        return jsonify(detail="トークンが正しくありません"), 401
    return None


@api.get("/ui")
def ui():
    return send_file(ROOT / "frontend" / "index.html")


@api.get("/")
def health():
    return jsonify(status="running")


@api.get("/session")
def get_session():
    return jsonify(core.session_info())


@api.post("/session")
def put_session():
    b = request.get_json()
    core.set_session(b["session"], b.get("backend_url"))
    return jsonify(core.session_info())


@api.delete("/session")
def del_session():
    core.clear_session()
    return jsonify(core.session_info())


@api.get("/jobs")
def jobs():
    return jsonify(jobs=core.list_jobs())


@api.post("/jobs")
def create():
    b = request.get_json()
    job_id = core.create_job(b["project_name"], b.get("source_content", ""), b.get("files", []), b.get("target_env", "auto"))
    core.start_task(job_id, core.pipeline)
    return jsonify(id=job_id)


@api.get("/jobs/<job_id>")
def detail(job_id):
    return jsonify(core.job_view(core.get_job(job_id)))


@api.get("/jobs/<job_id>/file")
def file(job_id):
    return jsonify(path=request.args["path"], content=core.read_job_file(job_id, request.args["path"]))


@api.get("/jobs/<job_id>/log")
def log(job_id):
    core.get_job(job_id)
    text, off = core.read_log(job_id, int(request.args.get("offset", -1)))
    return jsonify(text=text, offset=off)


@api.post("/jobs/<job_id>/answers")
def answers(job_id):
    core.submit_answers(job_id, request.get_json()["answers"])
    return jsonify(ok=True)


@api.post("/jobs/<job_id>/approve")
def approve(job_id):
    core.approve(job_id, (request.get_json(silent=True) or {}).get("visibility", "private"))
    return jsonify(ok=True)


@api.post("/jobs/<job_id>/permissions/start")
def permissions_start(job_id):
    return jsonify(url=core.start_grant(job_id))


@api.post("/jobs/<job_id>/reject")
def reject(job_id):
    core.reject(job_id)
    return jsonify(ok=True)


@api.post("/jobs/<job_id>/revise")
def revise(job_id):
    core.revise(job_id, (request.get_json() or {}).get("feedback", ""))
    return jsonify(ok=True)


@api.post("/queue/sync")
def sync():
    return jsonify(started=core.sync_queued())


bridge_srv, bridge_url = serve(api)

failures = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


def settings_script(guard=False, **kw):
    """ページを開くたびに実行される。guard=True なら、まだ設定が無いときだけ書き込む（ログイン後の設定を消さない）。"""
    cond = "if (!localStorage.getItem('autoDeploy.settings')) " if guard else ""
    return f"{cond}localStorage.setItem('autoDeploy.settings', JSON.stringify({json.dumps(kw)}));"


def fresh_page():
    """localStorage を共有しない、別のブラウザコンテキストのページ。"""
    return browser.new_context(viewport={"width": 390, "height": 844}).new_page()


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("dialog", lambda d: d.accept())  # confirm() は「OK」

    # ================= 初回起動: 初期設定（Googleログイン → Termuxの準備 → 接続確認）
    TOKEN = "ui-token"
    page.add_init_script(settings_script(guard=True, url=bridge_url, token=TOKEN, repoUrl="https://github.com/me/auto-deploy-agent.git"))
    page.goto(f"{bridge_url}/ui")
    page.wait_for_selector("text=初期設定")
    check("初回起動は初期設定の画面になる", True)
    page.screenshot(path=str(SHOTS / "00_setup.png"), full_page=True)

    cmd = page.inner_text("#cmdBox")
    check("セットアップコマンドにリポジトリとスクリプトが入る",
          "git clone https://github.com/me/auto-deploy-agent.git" in cmd and "termux-bridge/setup.sh" in cmd, cmd)
    check("セットアップコマンドにトークンが入る", f"BRIDGE_TOKEN={TOKEN}" in cmd, cmd)

    page.fill("#b-url", gcp_url)
    with page.expect_popup() as popup:
        page.click("button:has-text(\"Googleでログイン\")")
    login_url = popup.value.url
    check("外部ブラウザでバックエンドのログインURLを開く", login_url.startswith(gcp_url + "/auth/login?id="), login_url)
    page.wait_for_selector("text=ログイン中: owner@example.com", timeout=15000)
    check("ログインの受け渡しが完了し、メールアドレスが出る", True)
    page.wait_for_selector("text=新しいジョブを作る", timeout=15000)
    check("ブリッジに接続できていれば、自動でホームへ進む", True)
    info = core.session_info()
    check("ログイン情報がブリッジに渡される", info["logged_in"] and info["email"] == "owner@example.com"
          and info["backend_url"] == gcp_url, str(info))
    page.screenshot(path=str(SHOTS / "01_home_empty.png"))

    # ================= 静的サイト: 作成 → 承認待ち → 公開範囲(既定は自分だけ) → 承認 → 公開
    page.click("text=新しいジョブを作る")
    page.fill("#pname", "static-site")
    page.fill("#desc", "静的なHTMLサイトです。")
    index = Path(tmp) / "index.html"
    index.write_text("<h1>hello</h1>", encoding="utf-8")
    page.set_input_files("input[type=file]", str(index))
    page.wait_for_selector("text=index.html")
    page.click("text=ジョブを作る")

    page.wait_for_selector("text=この内容で本番にデプロイしますか？", timeout=40000)
    body = page.inner_text("#jobBody")
    check("成果物に index.html と Dockerfile", "index.html" in body and "Dockerfile" in body, body)
    check("仮実行が成功と表示", "成功" in body, body)
    check("公開範囲の既定は『自分だけ』と説明される", "owner@example.com でGoogleログインした人だけ" in body, body)
    check("『自分だけ』が選択状態", "on" in page.get_attribute("button[data-v=private]", "class"))
    page.screenshot(path=str(SHOTS / "03_approval.png"), full_page=True)

    page.click("text=Dockerfile")
    page.wait_for_selector(".overlay pre")
    check("ファイルの中身を表示", "FROM nginx" in page.inner_text(".overlay pre"))
    page.click(".overlay >> text=閉じる")

    page.click("text=承認してデプロイ")
    page.wait_for_selector("text=公開されました", timeout=40000)
    body = page.inner_text("#jobBody")
    check("公開されました＋URL", "https://stub-site-abc-an.a.run.app" in body)
    check("限定公開の案内（そのアカウントだけ）", "だけが開けます" in body, body)
    sent = [b for k, b in gcp_app.calls if k == "deploy" and b["project_name"] == "static-site"][-1]
    check("デプロイ要求の visibility は private", sent["visibility"] == "private", str(sent["visibility"]))
    page.wait_for_function("document.getElementById('logBox').textContent.includes('[gcp] Building container')")
    check("GCP側のログがTermuxのログに入る", True)
    page.screenshot(path=str(SHOTS / "05_deployed.png"), full_page=True)

    # ================= 『誰でも』を選ぶと、警告つきで public になる
    page.go_back()
    page.wait_for_selector("text=新しいジョブを作る")
    page.click("text=新しいジョブを作る")
    page.fill("#pname", "static-site")
    page.fill("#desc", "公開用")
    page.click("text=ジョブを作る")
    page.wait_for_selector("text=この内容で本番にデプロイしますか？", timeout=40000)
    page.click("button[data-v=public]")
    check("『誰でも』を選ぶと警告が出る", "個人情報や秘密を含むものは公開しないでください" in page.inner_text("#jobBody"))
    page.screenshot(path=str(SHOTS / "03b_public_warning.png"), full_page=True)
    page.click("text=承認してデプロイ")
    page.wait_for_selector("text=公開されました", timeout=40000)
    check("公開した場合の案内（知っていれば誰でも）", "知っていれば、誰でも開けます" in page.inner_text("#jobBody") or "知っていれば誰でも開けます" in page.inner_text("#jobBody"))
    sent = [b for k, b in gcp_app.calls if k == "deploy" and b["project_name"] == "static-site"][-1]
    check("公開を選んだときだけ public になる", sent["visibility"] == "public", str(sent["visibility"]))

    # ================= 権限が足りない → 承認カード → Googleで承認 → 自動で再開
    page.goto(f"{bridge_url}/ui")
    page.click("text=新しいジョブを作る")
    page.fill("#pname", "needsperm")
    page.fill("#desc", "権限が足りないケース")
    page.click("text=ジョブを作る")
    page.wait_for_selector("text=この内容で本番にデプロイしますか？", timeout=40000)
    page.click("text=承認してデプロイ")
    page.wait_for_selector("text=GCPの権限の承認が必要です", timeout=40000)
    body = page.inner_text("#jobBody")
    check("必要な権限が、日本語の説明とロール名つきで出る",
          "デプロイ用のアカウントに、基本のロールを付与する" in body and "roles/run.admin" in body, body)
    check("承認するまで何も実行されない旨が出る", "承認するまで、何も変更されません" in body)
    page.screenshot(path=str(SHOTS / "06_permission.png"), full_page=True)

    ctx.route("https://backend.example/**", lambda route: route.fulfill(status=200, content_type="text/html", body="<h1>承認画面</h1>"))
    with page.expect_popup() as popup:
        page.click("text=Googleで承認して付与する")
    check("承認画面（Google）が外部ブラウザで開く", popup.value.url.startswith("https://backend.example/auth/grant?id=g1"), popup.value.url)
    page.wait_for_selector("text=Googleの画面で承認してください。承認が終わると、自動でデプロイを再開します。", timeout=15000)
    check("承認待ちの表示になる", True)
    page.screenshot(path=str(SHOTS / "06b_waiting.png"), full_page=True)
    gcp_app.grant_state["g1"] = "done"          # ユーザーがGoogleの画面で承認した
    page.wait_for_selector("text=公開されました", timeout=40000)
    check("承認されると、自動でデプロイが再開して完了する", True)

    # ================= 質問・却下
    page.goto(f"{bridge_url}/ui")
    page.click("text=新しいジョブを作る")
    page.fill("#pname", "needs")
    page.fill("#desc", "何かのアプリ")
    page.click("text=ジョブを作る")
    page.wait_for_selector("text=確認したいことがあります", timeout=30000)
    page.click(".chip:has-text('Cloud Run')")
    page.click("text=回答を送る")
    page.wait_for_selector("text=この内容で本番にデプロイしますか？", timeout=40000)
    check("質問に答えると承認待ちへ進む", True)
    page.click("text=却下する")
    page.wait_for_selector(".card >> text=やり直す", timeout=10000)
    check("却下するとやり直しボタンが出る", "却下" in page.inner_text("#jobBody"))

    # ================= 設定・ログアウト
    page.goto(f"{bridge_url}/ui")
    page.click("button[aria-label=設定]")
    page.wait_for_selector("#s-url")
    check("設定にログイン中のアカウントが出る", "ログイン中: owner@example.com" in page.inner_text("#app"))
    page.click("text=接続テスト")
    page.wait_for_selector("text=接続できました。")
    check("設定: 接続テスト", True)
    page.screenshot(path=str(SHOTS / "07_settings.png"), full_page=True)
    page.click("text=ログアウト")
    page.wait_for_selector("text=未ログイン")
    check("ログアウトで、アプリとブリッジの両方からセッションが消える", not core.session_info()["logged_in"])
    page.goto(f"{bridge_url}/ui")
    page.wait_for_selector("text=初期設定")
    check("ログアウト後に開くと、初期設定に戻る", True)

    # ================= 期限切れの案内 / トークン違い / ブリッジ未起動
    expired = {"k": "s", "email": "owner@example.com", "exp": 1}
    expired_token = base64.urlsafe_b64encode(json.dumps(expired).encode()).rstrip(b"=").decode() + ".sig"
    exp_page = fresh_page()
    exp_page.add_init_script(settings_script(url=bridge_url, token=TOKEN, backendUrl=gcp_url, session=expired_token, email="owner@example.com"))
    exp_page.goto(f"{bridge_url}/ui")
    exp_page.wait_for_selector("text=Googleにログインしてください")
    check("セッションが期限切れなら、ホームにログイン案内が出る", True)

    ok_session = dict(backendUrl=gcp_url, session=SESSION, email="owner@example.com")
    bad = fresh_page()
    bad.add_init_script(settings_script(url=bridge_url, token="wrong", **ok_session))
    bad.goto(f"{bridge_url}/ui")
    bad.wait_for_selector("text=トークンが一致しません")
    check("トークン違いの案内が出る", True)

    off = fresh_page()
    off.add_init_script(settings_script(url="http://127.0.0.1:9", token="x", **ok_session))
    off.goto(f"{bridge_url}/ui")
    off.wait_for_selector("text=Termuxのブリッジに接続できません", timeout=15000)
    check("未接続の案内が出る", True)
    off.screenshot(path=str(SHOTS / "08_offline.png"))

    real_errors = [e for e in errors if "ERR_CONNECTION_REFUSED" not in e and "Failed to load resource" not in e]
    check("JavaScriptエラーなし", not real_errors, str(real_errors))
    browser.close()

print("\nFAILED: " + ", ".join(failures) if failures else "\nALL PASSED")
sys.exit(1 if failures else 0)
