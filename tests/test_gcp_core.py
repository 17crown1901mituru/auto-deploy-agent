"""gcp-backend のテスト（標準ライブラリのみ・gcloud は偽物のスクリプトで代用・Google への通信は差し替え）。
実行: python3 -m unittest discover -s tests -p "test_gcp_core.py"   （リポジトリのルートで）
"""
import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "gcp-backend")

FAKE_GCLOUD = r"""#!/bin/bash
# 偽のgcloud: 呼び出しを記録し、FAKE_FAIL_STAGE で失敗のしかたを変える
tokfile=""; for a in "$@"; do case "$a" in --access-token-file=*) tokfile="${a#--access-token-file=}";; esac; done
tok=""; bytes=0
if [ -n "$tokfile" ]; then tok=$(cat "$tokfile"); bytes=$(wc -c < "$tokfile" | tr -d ' '); fi
echo "$* ||TOKEN=$tok ||BYTES=$bytes" >> "$FAKE_GCLOUD_LOG"
case "$1 $2" in
  "run services")
    if [ "$3" = "describe" ]; then [ "$FAKE_DESCRIBE_FAIL" = "1" ] && exit 1; echo "https://demo-abc123-an.a.run.app"; exit 0; fi
    [ "$FAKE_FAIL_STAGE" = "iap_invoker" ] && { echo "ERROR: PERMISSION_DENIED: Permission 'run.services.setIamPolicy' denied" >&2; exit 1; }
    echo "Updated IAM policy"; exit 0;;
  "run deploy")
    src=""; while [ $# -gt 0 ]; do [ "$1" = "--source" ] && src="$2"; shift; done
    echo "Building using source in $src"
    (cd "$src" && find . -type f | sort)
    [ "$FAKE_FAIL_STAGE" = "api" ] && { echo "ERROR: API [run.googleapis.com] not enabled on project 123456" >&2; exit 1; }
    [ "$FAKE_FAIL_STAGE" = "deploy" ] && { echo "ERROR: PERMISSION_DENIED: iam.serviceaccounts.actAs denied" >&2; exit 3; }
    echo "Service URL: https://demo-abc123-an.a.run.app"; exit 0;;
  "iap web")
    [ "$FAKE_FAIL_STAGE" = "iap_access" ] && { echo "ERROR: PERMISSION_DENIED: iap.web.setIamPolicy" >&2; exit 1; }
    echo "Updated IAM policy"; exit 0;;
  "projects add-iam-policy-binding") [ "$FAKE_FAIL_GRANT" = "1" ] && { echo "ERROR: forbidden" >&2; exit 1; }; echo "ok"; exit 0;;
esac
echo "ok"; exit 0
"""

OWNER = "owner@example.com"


def setUpModule():
    global core, auth, perms, tmpdir
    tmpdir = tempfile.mkdtemp()
    fake = os.path.join(tmpdir, "gcloud")
    with open(fake, "w") as f:
        f.write(FAKE_GCLOUD)
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    os.environ.update({
        "GCLOUD_BIN": fake, "FAKE_GCLOUD_LOG": os.path.join(tmpdir, "calls.log"),
        "GCP_PROJECT": "my-proj", "GCP_PROJECT_NUMBER": "123456",
        "BACKEND_SERVICE_ACCOUNT": "backend@my-proj.iam.gserviceaccount.com",
        "SESSION_SECRET": "unit-test-secret-0123456789", "ALLOWED_EMAILS": f"{OWNER}, second@example.com",
        "GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "csecret",
        "PUBLIC_BASE_URL": "https://backend.example.run.app",
    })
    sys.path.insert(0, BACKEND)
    import auth as _auth, core as _core, perms as _perms  # noqa: E402
    core, auth, perms = _core, _auth, _perms


def calls():
    path = os.environ["FAKE_GCLOUD_LOG"]
    if not os.path.exists(path):
        return ""
    with open(path) as fh:
        return fh.read()


def reset_calls():
    try:
        os.remove(os.environ["FAKE_GCLOUD_LOG"])
    except OSError:
        pass


def fake_id_token(email, aud="cid.apps.googleusercontent.com", verified=True, exp=None):
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    claims = {"iss": "https://accounts.google.com", "aud": aud, "email": email, "email_verified": verified,
              "exp": exp or int(time.time()) + 600}
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


# ================================================================ コアの純粋なロジック

class CoreTests(unittest.TestCase):
    def test_safe_rel_path(self):
        self.assertEqual(core.safe_rel_path("a/b.txt"), "a/b.txt")
        self.assertEqual(core.safe_rel_path("a\\b.txt"), "a/b.txt")
        for bad in ["", "/etc/passwd", "../x", "a/../../x", "C:\\x", "a\x00b"]:
            with self.assertRaises(ValueError, msg=bad):
                core.safe_rel_path(bad)

    def test_slugify_service(self):
        self.assertEqual(core.slugify_service("My Web_App!"), "my-web-app")
        self.assertEqual(core.slugify_service("123abc"), "app-123abc")
        self.assertEqual(core.slugify_service("日本語"), "app")
        self.assertLessEqual(len(core.slugify_service("x" * 100)), 40)

    def test_extract_json(self):
        self.assertEqual(core.extract_json('{"a":1}'), {"a": 1})
        self.assertEqual(core.extract_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(core.extract_json('結果です\n{"a": {"b": 2}}\n以上'), {"a": {"b": 2}})
        for bad in ["", "no json", "[1,2]", "{broken"]:
            with self.assertRaises(ValueError, msg=bad):
                core.extract_json(bad)

    def test_normalize_needs_info(self):
        out = core.normalize_analysis({"status": "needs_info", "questions": [
            {"id": "Deploy Target!", "question": "デプロイ先は？", "options": ["Cloud Run", "", "AWS"]},
            {"question": ""}]})
        self.assertEqual(out["questions"][0]["id"], "deploy_target")
        self.assertEqual(out["questions"][0]["options"], ["Cloud Run", "AWS"])
        self.assertEqual(len(out["questions"]), 1)
        with self.assertRaises(ValueError):
            core.normalize_analysis({"status": "needs_info", "questions": []})

    def test_normalize_ready(self):
        out = core.normalize_analysis({
            "status": "ready", "summary": "s", "stack": "python",
            "files": [{"path": "Dockerfile", "content": "FROM python"}],
            "run": {"command": "python app.py", "port": "9000", "health_path": "health"},
            "deploy": {"service_name": "My App"}})
        self.assertEqual(out["run"]["port"], 9000)
        self.assertEqual(out["run"]["health_path"], "/health")
        self.assertEqual(out["deploy"]["service_name"], "my-app")
        with self.assertRaises(ValueError):
            core.normalize_analysis({"status": "ready", "files": [{"path": "../x", "content": "a"}]})
        with self.assertRaises(ValueError):
            core.normalize_analysis({"status": "maybe"})
        self.assertEqual(core.normalize_analysis({"status": "ready", "run": {"port": 99999}})["run"]["port"], 8080)

    def test_build_prompt(self):
        files = [{"path": "main.py", "content": "print(1)"}, {"path": "README.md", "content": "# Hi"},
                 {"path": "big.txt", "content": "x" * 50000}]
        p = core.build_prompt("demo", "auto", "説明です", files,
                              {"deploy_target": "Cloud Run", "revision_feedback": "軽くして"})
        materials = p.split("Materials follow")[1]
        self.assertLess(materials.index("README.md"), materials.index("main.py"))
        self.assertIn("[truncated]", p)
        self.assertIn("deploy_target: Cloud Run", p)
        self.assertIn("軽くして", p)

    def test_run_analysis_retries_once(self):
        replies = iter(["これはJSONではありません", json.dumps({"status": "ready", "summary": "ok"})])
        calls_ = []

        async def ask(text):
            calls_.append(text)
            return next(replies)

        self.assertEqual(asyncio.run(core.run_analysis(ask, "prompt"))["summary"], "ok")
        self.assertEqual(len(calls_), 2)

        async def always_bad(text):
            return "nope"

        with self.assertRaises(ValueError):
            asyncio.run(core.run_analysis(always_bad, "prompt"))

    def test_prepare_deploy_validation(self):
        with self.assertRaises(ValueError):
            core.prepare_deploy("demo", [], owner_email=OWNER)
        with self.assertRaises(ValueError):
            core.prepare_deploy("demo", [{"path": "../evil", "content": "x"}], owner_email=OWNER)
        with self.assertRaises(ValueError):
            core.prepare_deploy("demo", [{"path": "a", "content": "x"}, {"path": "a", "content": "y"}], owner_email=OWNER)
        with self.assertRaises(ValueError):  # 限定公開なのにアカウントが無い
            core.prepare_deploy("demo", [{"path": "a", "content": "x"}])
        with self.assertRaises(ValueError):
            core.prepare_deploy("demo", [{"path": "a", "content": "x"}], visibility="everyone", owner_email=OWNER)
        self.assertEqual(core.prepare_deploy("demo", [{"path": "a", "content": "x"}], owner_email=OWNER)["visibility"], "private")


# ================================================================ デプロイ

class DeployTests(unittest.TestCase):
    def setUp(self):
        reset_calls()
        os.environ.pop("FAKE_FAIL_STAGE", None)

    def run_deploy(self, **kw):
        params = core.prepare_deploy("Demo App", [{"path": "Dockerfile", "content": "FROM x"},
                                                  {"path": "src/app.py", "content": "print(1)"}], port=8080, **kw)
        q = core.start_deploy(params)
        return [json.loads(line) for line in core.iter_events(q, ping_seconds=5)]

    def test_private_is_default_and_sets_up_iap(self):
        events = self.run_deploy(owner_email=OWNER)
        result = events[-1]
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["visibility"], result["owner"]), ("private", OWNER))
        self.assertEqual(result["url"], "https://demo-abc123-an.a.run.app")
        log = calls()
        self.assertIn("run deploy demo-app", log)
        self.assertIn("--no-allow-unauthenticated --iap", log)
        self.assertNotIn("--allow-unauthenticated --", log.replace("--no-allow-unauthenticated", ""))
        self.assertIn("serviceAccount:service-123456@gcp-sa-iap.iam.gserviceaccount.com", log)
        self.assertIn(f"iap web add-iam-policy-binding --member user:{OWNER} --role roles/iap.httpsResourceAccessor", log)
        self.assertIn("--resource-type cloud-run --service demo-app", log)
        self.assertIn("--project my-proj", log)
        files = "\n".join(e["line"] for e in events if e["event"] == "log")
        self.assertIn("./src/app.py", files)
        # 後始末
        self.assertTrue(core._deploy_lock.acquire(blocking=False))
        core._deploy_lock.release()
        src = [e["line"] for e in events if "Building using source" in e.get("line", "")][0].split(" in ")[1]
        self.assertFalse(os.path.exists(src))

    def test_public_when_requested(self):
        events = self.run_deploy(visibility="public")
        self.assertTrue(events[-1]["ok"])
        log = calls()
        self.assertIn("--allow-unauthenticated", log)
        self.assertNotIn("--iap", log)
        self.assertNotIn("iap web", log)
        self.assertIsNone(events[-1]["owner"])

    def _failure(self, stage):
        os.environ["FAKE_FAIL_STAGE"] = stage
        try:
            return self.run_deploy(owner_email=OWNER)
        finally:
            del os.environ["FAKE_FAIL_STAGE"]

    def test_permission_denied_on_deploy_proposes_roles(self):
        events = self._failure("deploy")
        need = [e for e in events if e["event"] == "permission_needed"][0]
        kinds = {r["type"] for r in need["requests"]}
        self.assertEqual(kinds, {"grant_deployer_roles", "grant_builder_role"})
        dep = [r for r in need["requests"] if r["type"] == "grant_deployer_roles"][0]
        self.assertEqual(dep["params"]["member"], "backend@my-proj.iam.gserviceaccount.com")
        self.assertIn("roles/run.admin", dep["params"]["roles"])
        self.assertFalse(events[-1]["ok"])
        self.assertTrue(events[-1]["needs_permission"])

    def test_api_not_enabled_proposes_enable_api(self):
        events = self._failure("api")
        need = [e for e in events if e["event"] == "permission_needed"][0]
        self.assertEqual([(r["type"], r["params"]["api"]) for r in need["requests"] if r["type"] == "enable_api"],
                         [("enable_api", "run.googleapis.com")])

    def test_iap_stage_failures(self):
        need = [e for e in self._failure("iap_access") if e["event"] == "permission_needed"][0]
        req = need["requests"][0]
        self.assertEqual((req["type"], req["params"]["member"]), ("iap_access", OWNER))
        need = [e for e in self._failure("iap_invoker") if e["event"] == "permission_needed"][0]
        self.assertEqual(need["requests"][0]["type"], "run_invoker_iap")

    def test_busy(self):
        self.assertTrue(core._deploy_lock.acquire(blocking=False))
        try:
            with self.assertRaises(core.DeployBusy):
                core.start_deploy(core.prepare_deploy("demo", [{"path": "a", "content": "x"}], owner_email=OWNER))
        finally:
            core._deploy_lock.release()

    def test_service_url(self):
        self.assertEqual(core.get_service_url("demo", "asia-northeast1"), "https://demo-abc123-an.a.run.app")
        os.environ["FAKE_DESCRIBE_FAIL"] = "1"
        try:
            self.assertIsNone(core.get_service_url("demo", "asia-northeast1"))
        finally:
            del os.environ["FAKE_DESCRIBE_FAIL"]


# ================================================================ ログイン・セッション

class AuthTests(unittest.TestCase):
    def test_session_roundtrip_and_tamper(self):
        token = auth.issue_session("Owner@Example.com")
        self.assertEqual(auth.verify_session(token), OWNER)
        self.assertEqual(auth.bearer_email("Bearer " + token), OWNER)
        self.assertIsNone(auth.bearer_email("Basic " + token))
        self.assertIsNone(auth.bearer_email(None))
        body, sig = token.split(".")
        forged = auth._b64e(json.dumps({"k": "s", "email": "evil@example.com", "exp": int(time.time()) + 999}).encode())
        self.assertIsNone(auth.verify_session(f"{forged}.{sig}"))
        self.assertIsNone(auth.verify_session(token + "x"))
        self.assertIsNone(auth.verify_session("garbage"))

    def test_session_expiry_and_allowlist(self):
        expired = auth.sign({"k": "s", "email": OWNER, "exp": int(time.time()) - 1})
        self.assertIsNone(auth.verify_session(expired))
        outsider = auth.issue_session("outsider@example.com")
        self.assertIsNone(auth.verify_session(outsider))   # 署名は正しくても許可リストに無い
        # 状態トークンはセッションとして使えない
        self.assertIsNone(auth.verify_session(auth.make_state("login", "x" * 20)))

    def test_missing_secret_is_loud(self):
        saved = os.environ.pop("SESSION_SECRET")
        try:
            with self.assertRaises(auth.AuthError):
                auth.verify_session("a.b")
        finally:
            os.environ["SESSION_SECRET"] = saved

    def test_empty_allowlist_rejects_everyone(self):
        saved = os.environ["ALLOWED_EMAILS"]
        os.environ["ALLOWED_EMAILS"] = ""
        try:
            self.assertFalse(auth.is_allowed(OWNER))
            self.assertIsNone(auth.verify_session(auth.issue_session(OWNER)))
        finally:
            os.environ["ALLOWED_EMAILS"] = saved

    def test_login_store_flow(self):
        store, login_id, verifier = auth.LoginStore(), auth.new_id(), auth.new_id() + auth.new_id()
        store.create(login_id, auth.challenge_of(verifier))
        self.assertEqual(store.poll(login_id, verifier), ("pending", None))
        self.assertEqual(store.poll(login_id, "wrong-verifier")[0], "invalid")
        store.complete(login_id, OWNER)
        self.assertEqual(store.poll(login_id, "wrong-verifier")[0], "invalid")
        self.assertEqual(store.poll(login_id, verifier), ("ready", OWNER))
        self.assertEqual(store.poll(login_id, verifier)[0], "invalid")   # 一度きり
        with self.assertRaises(auth.AuthError):
            store.create("short", "bad")
        with self.assertRaises(auth.AuthError):
            store.complete(auth.new_id(), OWNER)

    def test_challenge_matches_known_vector(self):
        # RFC 7636 の例: verifier → challenge（JavaScript側も同じ計算をする）
        self.assertEqual(auth.challenge_of("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_id_token_checks(self):
        self.assertEqual(auth.email_from_id_token(fake_id_token("A@B.com")), "a@b.com")
        for bad in [fake_id_token(OWNER, aud="other"), fake_id_token(OWNER, verified=False),
                    fake_id_token(OWNER, exp=int(time.time()) - 5), "not.a-token"]:
            with self.assertRaises(auth.AuthError):
                auth.email_from_id_token(bad)

    def test_auth_url(self):
        url = auth.build_auth_url(auth.make_state("grant", "g" * 20), auth.GRANT_SCOPES, login_hint=OWNER, prompt="consent")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["redirect_uri"], ["https://backend.example.run.app/auth/callback"])
        self.assertIn("https://www.googleapis.com/auth/cloud-platform", q["scope"][0])
        self.assertEqual((q["login_hint"], q["prompt"]), ([OWNER], ["consent"]))
        self.assertEqual(auth.read_state(q["state"][0])["kind"], "grant")
        self.assertIsNone(auth.read_state("tampered.state"))


# ================================================================ 権限の提案・検証・実行

CTX = {"project": "my-proj", "project_number": "123456", "backend_sa": "backend@my-proj.iam.gserviceaccount.com",
       "owner_email": OWNER, "service": "demo-app", "region": "asia-northeast1"}


class PermsTests(unittest.TestCase):
    def setUp(self):
        reset_calls()
        os.environ.pop("FAKE_FAIL_GRANT", None)

    def test_validate_rebuilds_params_from_server_context(self):
        evil = [{"type": "iap_access", "reason": "x", "params": {"member": "attacker@evil.com", "roles": ["roles/owner"]}},
                {"type": "grant_deployer_roles", "params": {"member": "attacker@evil.com", "roles": ["roles/owner"]}}]
        out = perms.validate_requests(evil, CTX)
        self.assertEqual(out[0]["params"]["member"], OWNER)
        self.assertEqual(out[0]["params"]["roles"], ["roles/iap.httpsResourceAccessor"])
        self.assertEqual(out[1]["params"]["member"], CTX["backend_sa"])
        self.assertNotIn("roles/owner", json.dumps(out))

    def test_validate_rejects_unknown_things(self):
        for bad in [[{"type": "grant_owner"}], [{"type": "enable_api", "params": {"api": "compute.googleapis.com"}}],
                    [], "x", [{"type": "iap_access"}] * 11, ["str"]]:
            with self.assertRaises(ValueError, msg=str(bad)[:40]):
                perms.validate_requests(bad, CTX)
        with self.assertRaises(ValueError):  # 必要な情報が足りない
            perms.validate_requests([{"type": "iap_access"}], dict(CTX, owner_email=""))

    def test_validate_dedupes(self):
        out = perms.validate_requests([{"type": "create_iap_agent"}, {"type": "create_iap_agent"}], CTX)
        self.assertEqual(len(out), 1)

    def test_run_grants_uses_the_users_token_and_cleans_up(self):
        reqs = perms.validate_requests([
            {"type": "enable_api", "params": {"api": "iap.googleapis.com"}}, {"type": "grant_deployer_roles"},
            {"type": "iap_access"}, {"type": "run_invoker_iap"}, {"type": "create_iap_agent"}], CTX)
        before = set(os.listdir(tempfile.gettempdir()))
        results = perms.run_grants(reqs, CTX, "user-access-token-xyz")
        self.assertTrue(all(r["ok"] for r in results), results)
        lines = [l for l in calls().splitlines() if l]
        token = "user-access-token-xyz"
        # すべてのコマンドが、文書化されたフラグでユーザーのトークンを使い、ファイルにはトークンだけが入っている
        self.assertTrue(all(f"||TOKEN={token} ||BYTES={len(token)}" in l for l in lines), lines)
        self.assertTrue(all("--access-token-file=" in l for l in lines), lines)
        text = calls()
        self.assertIn("services enable iap.googleapis.com --project my-proj", text)
        self.assertEqual(text.count("projects add-iam-policy-binding my-proj"), 3)   # デプロイ用ロール3つ
        self.assertIn("--member serviceAccount:backend@my-proj.iam.gserviceaccount.com --role roles/run.admin", text)
        self.assertIn("beta services identity create --service iap.googleapis.com", text)
        self.assertEqual([f for f in os.listdir(tempfile.gettempdir()) if f.startswith("tok-")],
                         [f for f in before if f.startswith("tok-")])   # トークンの一時ファイルが残らない

    def test_run_grants_reports_failures_and_continues(self):
        os.environ["FAKE_FAIL_GRANT"] = "1"
        try:
            reqs = perms.validate_requests([{"type": "grant_deployer_roles"}, {"type": "create_iap_agent"}], CTX)
            results = perms.run_grants(reqs, CTX, "t")
        finally:
            del os.environ["FAKE_FAIL_GRANT"]
        self.assertEqual([r["ok"] for r in results], [False, True])

    def test_detect_hints_for_first_time_iap(self):
        reqs, hints = perms.detect("ERROR: Please use the Cloud Run UI to enable IAP: OAuth brand not configured", "deploy", CTX)
        self.assertTrue(hints)
        self.assertEqual(reqs, [])

    def test_detect_missing_iap_agent(self):
        reqs, _ = perms.detect("Service account service-123456@gcp-sa-iap.iam.gserviceaccount.com does not exist", "iap_invoker", CTX)
        self.assertEqual({r["type"] for r in reqs}, {"create_iap_agent", "run_invoker_iap"})


# ================================================================ ルート関数のロジック（FastAPIの代用品で直接呼ぶ）

class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, HERE)
        import fake_fastapi
        fake_fastapi.install()
        import main
        cls.main = main
        cls.fake = fake_fastapi

    def setUp(self):
        reset_calls()
        self.main.login_store = auth.LoginStore()
        self.main.grant_store = perms.GrantStore()
        self.saved_exchange = auth.exchange_code

    def tearDown(self):
        auth.exchange_code = self.saved_exchange

    def state_from(self, redirect):
        return urllib.parse.parse_qs(urllib.parse.urlparse(redirect.url).query)["state"][0]

    def login(self, email=OWNER):
        verifier = auth.new_id() + auth.new_id()
        login_id = auth.new_id()
        redirect = self.main.auth_login(id=login_id, c=auth.challenge_of(verifier))
        self.assertIn("accounts.google.com", redirect.url)
        auth.exchange_code = lambda code: {"id_token": fake_id_token(email), "access_token": "a", "scope": "openid email"}
        page = self.main.auth_callback(code="abc", state=self.state_from(redirect), error=None)
        return page, login_id, verifier

    def test_login_success_flow(self):
        page, login_id, verifier = self.login()
        self.assertEqual(page.status_code, 200)
        self.assertIn("ログインできました", page.text)
        got = self.main.auth_poll(self.main.PollRequest(id=login_id, verifier=verifier))
        self.assertEqual((got["status"], got["email"]), ("ready", OWNER))
        self.assertEqual(self.main.current_user(authorization="Bearer " + got["session"]), OWNER)
        self.assertEqual(self.main.auth_poll(self.main.PollRequest(id=login_id, verifier=verifier))["status"], "invalid")

    def test_login_rejects_accounts_not_on_allowlist(self):
        page, login_id, verifier = self.login("outsider@example.com")
        self.assertEqual(page.status_code, 403)
        self.assertEqual(self.main.auth_poll(self.main.PollRequest(id=login_id, verifier=verifier))["status"], "pending")

    def test_callback_rejects_bad_state_and_cancel(self):
        self.assertEqual(self.main.auth_callback(code="x", state="forged.state", error=None).status_code, 400)
        self.assertEqual(self.main.auth_callback(code=None, state=None, error="access_denied").status_code, 400)

    def test_api_requires_a_valid_session(self):
        for header in [None, "Bearer nope", "Bearer " + auth.issue_session("outsider@example.com")]:
            with self.assertRaises(self.fake.HTTPException) as cm:
                self.main.current_user(authorization=header)
            self.assertEqual(cm.exception.status_code, 401)

    def start_grant(self, requests=None):
        reqs = requests or [{"type": "iap_access", "reason": "限定公開のため"}, {"type": "grant_builder_role"}]
        body = self.main.GrantStartRequest(requests=reqs, service="Demo App", region="asia-northeast1")
        return self.main.grants_start(body, email=OWNER)

    def consent(self, gid, email=OWNER, scope="openid email https://www.googleapis.com/auth/cloud-platform", token="user-token"):
        redirect = self.main.auth_grant(id=gid)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(redirect.url).query)
        self.assertEqual(q["login_hint"], [OWNER])
        auth.exchange_code = lambda code: {"id_token": fake_id_token(email), "access_token": token, "scope": scope}
        return self.main.auth_callback(code="abc", state=q["state"][0], error=None)

    def test_grant_flow_executes_with_users_own_token(self):
        started = self.start_grant()
        self.assertTrue(started["url"].startswith("https://backend.example.run.app/auth/grant?id="))
        self.assertEqual([r["type"] for r in started["requests"]], ["iap_access", "grant_builder_role"])
        self.assertEqual(self.main.grant_status(started["grant_id"], email=OWNER)["status"], "pending")
        self.assertEqual(calls(), "")                       # 承認前は何も実行されない
        page = self.consent(started["grant_id"])
        self.assertEqual(page.status_code, 200, page.text)
        status = self.main.grant_status(started["grant_id"], email=OWNER)
        self.assertEqual(status["status"], "done")
        self.assertTrue(all(r["ok"] for r in status["results"]))
        self.assertIn("||TOKEN=user-token", calls())
        self.assertIn("iap web add-iam-policy-binding --member user:" + OWNER, calls())
        # 同じ承認は二度使えない
        self.assertEqual(self.consent(started["grant_id"]).status_code, 400)

    def test_grant_needs_cloud_platform_scope(self):
        started = self.start_grant()
        self.assertEqual(self.consent(started["grant_id"], scope="openid email").status_code, 400)
        self.assertEqual(calls(), "")

    def test_grant_must_be_approved_by_the_same_account(self):
        started = self.start_grant()
        page = self.consent(started["grant_id"], email="second@example.com")
        self.assertEqual(page.status_code, 403)
        self.assertEqual(calls(), "")
        with self.assertRaises(self.fake.HTTPException):
            self.main.grant_status(started["grant_id"], email="second@example.com")

    def test_grant_start_rejects_disallowed_requests(self):
        with self.assertRaises(self.fake.HTTPException) as cm:
            self.start_grant([{"type": "enable_api", "params": {"api": "compute.googleapis.com"}}])
        self.assertEqual(cm.exception.status_code, 400)

    def test_deploy_route_streams_ndjson_and_is_private_by_default(self):
        body = self.main.DeployRequest(project_name="Demo App", files=[self.main.FileItem(path="a.txt", content="x")])
        resp = self.main.deploy(body, email=OWNER)
        events = [json.loads(line) for line in resp.content]
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(events[-1]["visibility"], "private")
        self.assertIn("--iap", calls())


if __name__ == "__main__":
    unittest.main()
