"""termux-bridge/core.py の結合テスト。
GCPバックエンドの代わりに Flask 製の偽サーバーを立て、解析→成果物→仮実行→承認→デプロイ までを通す。
（テスト専用の依存: flask。Termux本体には不要）
実行: python3 -m unittest discover -s tests -p "test_termux_core.py"   （リポジトリのルートで）
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

HERE = os.path.dirname(os.path.abspath(__file__))


def make_session(email="owner@example.com", exp=None):
    body = base64.urlsafe_b64encode(json.dumps({"k": "s", "email": email, "exp": exp or int(time.time()) + 86400}).encode()).rstrip(b"=").decode()
    return body + ".sig"


SESSION = make_session()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 偽のGCPバックエンド

def build_stub():
    app = Flask(__name__)
    app.calls = []
    app.grant_state = {}
    app.granted = False

    def authed():
        return request.headers.get("Authorization") == "Bearer " + SESSION

    def ready(**over):
        base = {
            "status": "ready", "summary": "テスト用のサイト", "stack": "static html",
            "files": [{"path": "Dockerfile", "content": "FROM nginx\n"}],
            "run": {"install": "", "command": "python3 -m http.server {port}", "port": 18765, "health_path": "/"},
            "deploy": {"service_name": "stub-site", "region": "asia-northeast1"}, "notes": "",
        }
        base.update(over)
        return base

    @app.post("/api/analyze")
    def analyze():
        if not authed():
            return jsonify(detail="ログインが必要です"), 401
        body = request.get_json()
        app.calls.append(("analyze", body))
        name = body["project_name"]
        if name == "needs":
            if not body["answers"]:
                return jsonify(status="needs_info", questions=[
                    {"id": "deploy_target", "question": "デプロイ先は？", "options": ["Cloud Run", "他"]}])
            return jsonify(ready())
        if name == "badrun":
            return jsonify(ready(run={"install": "", "command": "python3 missing_app.py", "port": 18766}))
        if name == "shellop":
            return jsonify(ready(run={"install": "", "command": "npm install && npm start", "port": 18767}))
        if name == "venv":
            return jsonify(ready(run={"install": "", "command": "python3 app.py", "port": 18768}))
        if name == "portbusy":
            return jsonify(ready(run={"install": "", "command": "python3 -m http.server {port}", "port": 18769}))
        if name == "notrial":
            return jsonify(ready(run={"install": "", "command": "", "port": 8080}))
        return jsonify(ready())

    @app.post("/api/deploy")
    def deploy():
        if not authed():
            return jsonify(detail="ログインが必要です"), 401
        body = request.get_json()
        app.calls.append(("deploy", body))
        name = body["project_name"]

        def gen():
            yield json.dumps({"event": "log", "line": "ファイルを展開しました"}) + "\n"
            yield json.dumps({"event": "ping"}) + "\n"
            yield json.dumps({"event": "log", "line": "Building container..."}) + "\n"
            if name == "needsperm" and not app.granted:
                req = {"id": "r1", "type": "grant_deployer_roles", "reason": "デプロイの基本ロール",
                       "params": {"member": "backend@p.iam.gserviceaccount.com", "roles": ["roles/run.admin"]}}
                yield json.dumps({"event": "permission_needed", "requests": [req], "hints": [],
                                  "service": "stub-site", "region": "asia-northeast1"}) + "\n"
                yield json.dumps({"event": "result", "ok": False, "error": "gcloud が異常終了しました (code 3)",
                                  "needs_permission": True}) + "\n"
            elif name == "hintsonly":
                yield json.dumps({"event": "permission_needed", "requests": [], "hints": ["コンソールでIAPを有効にしてください"]}) + "\n"
                yield json.dumps({"event": "result", "ok": False, "error": "iap", "needs_permission": False}) + "\n"
            elif name == "failing":
                yield json.dumps({"event": "result", "ok": False, "error": "gcloud が異常終了しました (code 3)"}) + "\n"
            elif name == "silent":
                return  # resultを返さずに終了
            else:
                yield json.dumps({"event": "result", "ok": True, "url": "https://stub-site-abc-an.a.run.app"}) + "\n"

        return Response(gen(), mimetype="application/x-ndjson")

    @app.post("/api/grants/start")
    def grants_start():
        if not authed():
            return jsonify(detail="ログインが必要です"), 401
        body = request.get_json()
        app.calls.append(("grants_start", body))
        app.grant_state["g1"] = "pending"
        return jsonify(grant_id="g1", url="https://backend.example/auth/grant?id=g1", requests=body["requests"])

    @app.get("/api/grants/<gid>")
    def grants_status(gid):
        if not authed():
            return jsonify(detail="ログインが必要です"), 401
        if gid not in app.grant_state:
            return jsonify(detail="not found"), 404
        state = app.grant_state[gid]
        if state == "done":
            app.granted = True
        ok = state == "done"
        return jsonify(status=state, requests=[], results=[{"id": "r1", "type": "grant_deployer_roles", "ok": ok, "output": ""}]
                       if state in ("done", "failed") else [])

    @app.get("/api/service/<name>")
    def service(name):
        if not authed():
            return jsonify(detail="ログインが必要です"), 401
        if name == "stub-site":
            return jsonify(service=name, url="https://stub-site-abc-an.a.run.app")
        return jsonify(detail="not found"), 404

    return app


def stat_mode(path):
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = build_stub()
        cls.server = make_server("127.0.0.1", 0, cls.stub, threaded=True)
        cls.stub_url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        cls.tmp = tempfile.mkdtemp()
        os.environ.update({
            "AUTO_DEPLOY_HOME": os.path.join(cls.tmp, "home"),
            "WORKSPACE_DIR": os.path.join(cls.tmp, "ws"),
            "LOG_DIR": os.path.join(cls.tmp, "logs"),
            "DB_PATH": os.path.join(cls.tmp, "home", "jobs.db"),
            "GCP_BACKEND_URL": cls.stub_url,
            "GRANT_POLL_SECONDS": "0.2",
            "START_TIMEOUT": "15",
            "BRIDGE_TOKEN": "bridge-secret",
        })
        cls.core = load_module("bridge_core", os.path.join(HERE, "..", "termux-bridge", "core.py"))
        cls.core.init()
        cls.core.load_token()
        cls.core.set_session(SESSION, cls.stub_url)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.core.GCP_BACKEND_URL = self.stub_url
        self.core.set_session(SESSION)
        self.core.TRIAL_RUN = True
        self.stub.grant_state.clear()
        self.stub.granted = False

    # ---- ヘルパー
    def wait_for(self, job_id, statuses, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            job = self.core.get_job(job_id)
            if job["status"] in statuses:
                return job
            time.sleep(0.2)
        self.fail(f"timeout: status={self.core.get_job(job_id)['status']} wanted {statuses}")

    def make_job(self, name, files=None, text=""):
        job_id = self.core.create_job(name, text, files or [], "auto")
        self.assertTrue(self.core.start_task(job_id, self.core.pipeline))
        return job_id

    def log_text(self, job_id):
        text, _ = self.core.read_log(job_id, 0)
        return text

    # ---- テスト
    def test_full_flow_static_site(self):
        c = self.core
        job_id = self.make_job("static-site", [{"path": "index.html", "content": "<h1>hello</h1>"}])
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertEqual(job["status"], "awaiting_approval", job["message"])
        paths = {a["path"]: a["origin"] for a in job["artifacts"]}
        self.assertEqual(paths, {"index.html": "user", "Dockerfile": "generated"})
        self.assertTrue(job["trial"]["ok"], job["trial"])
        self.assertEqual(job["trial"]["status_code"], 200)
        self.assertEqual(c.read_job_file(job_id, "Dockerfile"), "FROM nginx\n")
        with self.assertRaises(c.JobError):
            c.read_job_file(job_id, "../../etc/passwd")
        with self.assertRaises(c.JobError):
            c.read_job_file(job_id, "missing.txt")

        c.approve(job_id)
        with self.assertRaises(c.JobError):  # 二重承認は拒否
            c.approve(job_id)
        job = self.wait_for(job_id, ("deployed", "failed"))
        self.assertEqual(job["status"], "deployed", job["message"])
        self.assertEqual(job["deploy_url"], "https://stub-site-abc-an.a.run.app")

        sent = [b for k, b in self.stub.calls if k == "deploy" and b["project_name"] == "static-site"][-1]
        self.assertEqual({f["path"] for f in sent["files"]}, {"index.html", "Dockerfile"})
        self.assertEqual(sent["service_name"], "stub-site")
        log = self.log_text(job_id)
        for needle in ("[trial]", "[gcp] Building container...", "[user] 承認しました", "deployed"):
            self.assertIn(needle, log)
        # ログは ~/deploy-logs/<project>/ に置かれる
        self.assertIn(os.path.join("logs", "static-site"), job["log_path"])

    def test_needs_info_then_answers(self):
        c = self.core
        job_id = self.make_job("needs", text="何かのアプリ")
        job = self.wait_for(job_id, ("needs_info", "failed"))
        self.assertEqual(job["status"], "needs_info")
        self.assertEqual(job["questions"][0]["id"], "deploy_target")
        with self.assertRaises(c.JobError):
            c.submit_answers(job_id, {"deploy_target": "  "})
        c.submit_answers(job_id, {"deploy_target": "Cloud Run"})
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertEqual(job["status"], "awaiting_approval", job["message"])
        sent = [b for k, b in self.stub.calls if k == "analyze" and b["project_name"] == "needs"][-1]
        self.assertEqual(sent["answers"], {"deploy_target": "Cloud Run"})

    def test_trial_failure_is_reported_not_fatal(self):
        job_id = self.make_job("badrun", [{"path": "x.txt", "content": "x"}])
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertEqual(job["status"], "awaiting_approval")
        self.assertFalse(job["trial"]["ok"])
        self.assertIn("終了", job["trial"]["detail"])

    def test_shell_operators_skip_trial(self):
        job_id = self.make_job("shellop", [{"path": "x.txt", "content": "x"}])
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertTrue(job["trial"]["skipped"])
        self.assertIn("シェル", job["trial"]["detail"])

    def test_trial_disabled_or_no_command(self):
        self.core.TRIAL_RUN = False
        job = self.wait_for(self.make_job("static-site", [{"path": "a", "content": "b"}]), ("awaiting_approval", "failed"))
        self.assertTrue(job["trial"]["skipped"])
        self.core.TRIAL_RUN = True
        job = self.wait_for(self.make_job("notrial", [{"path": "a", "content": "b"}]), ("awaiting_approval", "failed"))
        self.assertTrue(job["trial"]["skipped"])

    def test_venv_and_requirements(self):
        app = ("import os, http.server, socketserver\n"
               "port = int(os.environ['PORT'])\n"
               "socketserver.TCPServer.allow_reuse_address = True\n"
               "socketserver.TCPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
        job_id = self.make_job("venv", [{"path": "app.py", "content": app},
                                        {"path": "requirements.txt", "content": "# no deps\n"}])
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertTrue(job["trial"]["ok"], (job["trial"], self.log_text(job_id)))
        self.assertEqual([s["name"] for s in job["trial"]["steps"]], ["install", "start"])
        self.assertTrue(os.path.isdir(os.path.join(self.core.src_dir(job_id), ".venv")))
        # 承認 → .venv は送られない
        self.core.approve(job_id)
        self.wait_for(job_id, ("deployed", "failed"))
        sent = [b for k, b in self.stub.calls if k == "deploy" and b["project_name"] == "venv"][-1]
        self.assertFalse(any(f["path"].startswith(".venv") for f in sent["files"]))
        self.assertIn("app.py", {f["path"] for f in sent["files"]})

    def test_busy_port_is_replaced(self):
        import socket
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 18769))
        blocker.listen(5)
        try:
            job_id = self.make_job("portbusy", [{"path": "index.html", "content": "hi"}])
            job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        finally:
            blocker.close()
        self.assertTrue(job["trial"]["ok"], job["trial"])
        self.assertNotEqual(job["trial"]["port"], 18769)
        self.assertIn("使用中", self.log_text(job_id))

    def test_offline_queue_and_sync(self):
        c = self.core
        c.GCP_BACKEND_URL = "http://127.0.0.1:9"  # 誰も待っていないポート
        job_id = self.make_job("static-site", [{"path": "index.html", "content": "hi"}])
        job = self.wait_for(job_id, ("queued", "failed", "awaiting_approval"))
        self.assertEqual(job["status"], "queued", job["message"])
        c.GCP_BACKEND_URL = self.stub_url
        deadline = time.time() + 5
        while time.time() < deadline and c.sync_queued() == 0:  # 前回のスレッドが終わるまで少し待つ
            time.sleep(0.2)
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertEqual(job["status"], "awaiting_approval")

    def test_expired_session_fails_with_login_message(self):
        self.core.set_session("bad.token")
        job = self.wait_for(self.make_job("static-site", [{"path": "a", "content": "b"}]), ("failed", "awaiting_approval"))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "auth_required")
        self.assertIn("ログイン", job["message"])

    def test_no_session_does_not_call_the_backend(self):
        self.core.SESSION_FILE.unlink()
        before = len(self.stub.calls)
        job = self.wait_for(self.make_job("static-site", [{"path": "a", "content": "b"}]), ("failed", "awaiting_approval"))
        self.assertEqual((job["status"], job["error"]), ("failed", "auth_required"))
        self.assertEqual(len(self.stub.calls), before)

    def test_deploy_failure_then_revise(self):
        c = self.core
        job_id = self.make_job("failing", [{"path": "a.txt", "content": "x"}])
        self.wait_for(job_id, ("awaiting_approval",))
        c.approve(job_id)
        job = self.wait_for(job_id, ("failed", "deployed"))
        self.assertEqual(job["status"], "failed")
        self.assertIn("code 3", job["message"])
        c.revise(job_id, "もう少し軽くして")
        job = self.wait_for(job_id, ("awaiting_approval", "failed"))
        self.assertEqual(job["status"], "awaiting_approval")
        sent = [b for k, b in self.stub.calls if k == "analyze" and b["project_name"] == "failing"][-1]
        self.assertEqual(sent["answers"], {"revision_feedback": "もう少し軽くして"})

    def test_missing_result_then_check_deploy(self):
        c = self.core
        job_id = self.make_job("silent", [{"path": "a.txt", "content": "x"}])
        self.wait_for(job_id, ("awaiting_approval",))
        c.approve(job_id)
        job = self.wait_for(job_id, ("failed", "deployed"))
        self.assertEqual(job["status"], "failed")  # 結果イベントが無い → 失敗扱い
        url = c.check_deploy(job_id)                # GCP側に問い合わせて確認
        self.assertEqual(url, "https://stub-site-abc-an.a.run.app")
        self.assertEqual(c.get_job(job_id)["status"], "deployed")

    def test_private_by_default_and_public_on_request(self):
        c = self.core
        job_id = self.make_job("static-site", [{"path": "a", "content": "b"}])
        self.wait_for(job_id, ("awaiting_approval",))
        c.approve(job_id)
        job = self.wait_for(job_id, ("deployed", "failed"))
        self.assertEqual(job["visibility"], "private")
        self.assertIn("ログインしたアカウント限定", job["message"])
        sent = [b for k, b in self.stub.calls if k == "deploy"][-1]
        self.assertEqual(sent["visibility"], "private")

        job_id = self.make_job("static-site", [{"path": "a", "content": "b"}])
        self.wait_for(job_id, ("awaiting_approval",))
        with self.assertRaises(c.JobError):
            c.approve(job_id, "everyone")
        c.approve(job_id, "public")
        job = self.wait_for(job_id, ("deployed", "failed"))
        self.assertEqual(job["visibility"], "public")
        self.assertEqual([b for k, b in self.stub.calls if k == "deploy"][-1]["visibility"], "public")
        self.assertIn("[user] 承認しました（公開範囲: 誰でも（公開））", self.log_text(job_id))

    def _to_needs_permission(self, name="needsperm"):
        c = self.core
        job_id = self.make_job(name, [{"path": "a", "content": "b"}])
        self.wait_for(job_id, ("awaiting_approval",))
        c.approve(job_id)
        return job_id, self.wait_for(job_id, ("needs_permission", "deployed", "failed"))

    def test_permission_flow_pauses_then_resumes_after_user_approval(self):
        c = self.core
        job_id, job = self._to_needs_permission()
        self.assertEqual(job["status"], "needs_permission", job["message"])
        perm = job["permissions"]
        self.assertEqual(perm["requests"][0]["type"], "grant_deployer_roles")
        self.assertFalse(perm["waiting"])
        self.assertIn("[permission] 承認が必要", self.log_text(job_id))

        url = c.start_grant(job_id)                       # 承認画面のURLが返る
        self.assertTrue(url.startswith("https://backend.example/auth/grant?id=g1"))
        started = [b for k, b in self.stub.calls if k == "grants_start"][-1]
        self.assertEqual((started["service"], started["region"]), ("stub-site", "asia-northeast1"))
        job = c.get_job(job_id)
        self.assertEqual(job["status"], "needs_permission")   # 承認するまで進まない
        self.assertTrue(job["permissions"]["waiting"])
        self.assertEqual(c.start_grant(job_id), url)          # 待機中に押しても同じURL

        self.stub.grant_state["g1"] = "done"                  # ユーザーがGoogleの画面で承認した
        job = self.wait_for(job_id, ("deployed", "failed"))
        self.assertEqual(job["status"], "deployed", job["message"])
        self.assertEqual(job["deploy_url"], "https://stub-site-abc-an.a.run.app")
        log = self.log_text(job_id)
        self.assertIn("[grant] 権限を付与しました: grant_deployer_roles", log)
        self.assertEqual(len([1 for k, b in self.stub.calls if k == "deploy" and b["project_name"] == "needsperm"]), 2)

    def test_permission_grant_failure_and_lost_grant(self):
        c = self.core
        job_id, job = self._to_needs_permission()
        c.start_grant(job_id)
        self.stub.grant_state["g1"] = "failed"
        end = time.time() + 10
        while time.time() < end and c.get_job(job_id)["permissions"]["waiting"]:
            time.sleep(0.2)
        job = c.get_job(job_id)
        self.assertEqual(job["status"], "needs_permission")
        self.assertIn("付与できませんでした", job["message"])
        self.assertFalse(job["permissions"]["results"][0]["ok"])

        # もう一度やる → バックエンドが承認の情報を失っていた場合（再起動など）
        deadline = time.time() + 5
        while time.time() < deadline and job_id in c._active:
            time.sleep(0.1)
        c.start_grant(job_id)
        self.stub.grant_state.clear()
        end = time.time() + 15
        while time.time() < end and c.get_job(job_id)["permissions"]["waiting"]:
            time.sleep(0.2)
        self.assertIn("失われました", c.get_job(job_id)["message"])

    def test_hints_only_has_nothing_to_approve(self):
        c = self.core
        job_id, job = self._to_needs_permission("hintsonly")
        self.assertEqual(job["status"], "needs_permission")
        self.assertEqual(job["permissions"]["requests"], [])
        self.assertIn("IAP", job["permissions"]["hints"][0])
        with self.assertRaises(c.JobError):
            c.start_grant(job_id)
        c.approve(job_id)           # 手動で設定したあと、もう一度デプロイできる
        self.wait_for(job_id, ("needs_permission", "deployed", "failed"))

    def test_start_grant_requires_login_and_state(self):
        c = self.core
        job_id = self.make_job("static-site", [{"path": "a", "content": "b"}])
        self.wait_for(job_id, ("awaiting_approval",))
        with self.assertRaises(c.JobError):
            c.start_grant(job_id)

    def test_session_storage(self):
        c = self.core
        info = c.session_info()
        self.assertTrue(info["logged_in"])
        self.assertEqual(info["email"], "owner@example.com")
        self.assertEqual(info["backend_url"], self.stub_url)
        self.assertEqual(stat_mode(c.SESSION_FILE), 0o600)
        for bad in ["nodot", "a.b.c", "x" * 3000]:
            with self.assertRaises(c.JobError):
                c.set_session(bad)
        with self.assertRaises(c.JobError):
            c.set_session(SESSION, "not a url")
        c.set_session(make_session(exp=int(time.time()) - 10))
        self.assertFalse(c.session_info()["logged_in"])
        c.set_session(SESSION)

    def test_schema_migration_adds_new_columns(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, project_name TEXT)")
        self.core._migrate(conn)
        self.assertTrue({"visibility", "permissions"} <= {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")})
        self.core._migrate(conn)    # 2回目も壊れない

    def test_reject(self):
        c = self.core
        job_id = self.make_job("static-site", [{"path": "a", "content": "b"}])
        self.wait_for(job_id, ("awaiting_approval",))
        c.reject(job_id)
        self.assertEqual(c.get_job(job_id)["status"], "rejected")
        with self.assertRaises(c.JobError):
            c.approve(job_id)

    def test_recover_interrupted(self):
        c = self.core
        job_id = c.create_job("interrupted", "x", [], "auto")
        c.update_job(job_id, status="deploying")
        c.recover_interrupted()
        job = c.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("結果を確認", job["message"])

    def test_validation(self):
        c = self.core
        for kwargs in [dict(project_name="", source_content="x", files=[]),
                       dict(project_name="ok", source_content="", files=[]),
                       dict(project_name="ok", source_content="x", files=[{"path": "../a", "content": "x"}]),
                       dict(project_name="ok", source_content="x",
                            files=[{"path": "a", "content": "x"}, {"path": "a", "content": "y"}]),
                       dict(project_name="ok", source_content="x",
                            files=[{"path": "big", "content": "x" * 300_000}])]:
            with self.assertRaises(c.JobError, msg=str(kwargs)[:60]):
                c.create_job(kwargs["project_name"], kwargs["source_content"], kwargs["files"])

    def test_parse_command(self):
        c = self.core
        self.assertEqual(c.parse_command("python3 -m http.server {port}", 9000),
                         ["python3", "-m", "http.server", "9000"])
        for bad in ["", "rm -rf /", "./python app.py", "/usr/bin/python app.py", "python a.py && ls",
                    "python a.py | tee x", "bash run.sh", "curl http://x"]:
            with self.assertRaises(ValueError, msg=bad):
                c.parse_command(bad, 8080)

    def test_token(self):
        c = self.core
        self.assertTrue(c.check_token("bridge-secret"))
        self.assertFalse(c.check_token("nope"))
        self.assertFalse(c.check_token(None))

    def test_trial_env_hides_secrets(self):
        c = self.core
        os.environ["SOME_SECRET"] = "should-not-leak"
        try:
            env = c._trial_env(c.WORKSPACE_DIR, 1234)
        finally:
            del os.environ["SOME_SECRET"]
        self.assertNotIn("SOME_SECRET", env)
        self.assertNotIn("BRIDGE_TOKEN", env)
        self.assertNotIn("bridge-secret", json.dumps(env))
        self.assertEqual(env["PORT"], "1234")

    def test_log_offsets(self):
        c = self.core
        job_id = c.create_job("logs", "x", [], "auto")
        text, off = c.read_log(job_id, -1)
        self.assertIn("作成", text)
        c.jlog(job_id, "t", "追記")
        text2, off2 = c.read_log(job_id, off)
        self.assertIn("追記", text2)
        self.assertNotIn("作成", text2)
        self.assertGreater(off2, off)


if __name__ == "__main__":
    unittest.main()
