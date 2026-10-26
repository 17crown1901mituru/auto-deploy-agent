"""GCPの権限付与: 提案 → 検証 → ユーザー承認後の実行。FastAPI/ADKに依存しない部分。

方針:
- エージェント（とバックエンド）は「何の権限が必要か」を“種類つきの提案”として出すだけ。自由なロール名は受け付けない
- 実行できるのは下の許可リストにある種類だけ。パラメータはサーバー側で作り直す（クライアントの値は信用しない）
- 実行は、ユーザーがGoogleの同意画面で承認したとき、そのユーザー本人のトークンで1回だけ行う
  （バックエンドのサービスアカウントに強い権限を持たせない）
"""
import hashlib
import json
import os
import re
import subprocess
import secrets
import tempfile
import threading
import time

GCLOUD_BIN = os.environ.get("GCLOUD_BIN", "gcloud")

ALLOWED_APIS = {
    "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "iap.googleapis.com", "storage.googleapis.com", "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com", "iam.googleapis.com",
}
# デプロイするアカウント(バックエンドのサービスアカウント)に必要なロール
DEPLOYER_ROLES = ["roles/run.admin", "roles/serviceusage.serviceUsageConsumer", "roles/iam.serviceAccountUser"]
# Cloud Buildが使うサービスアカウントに必要なロール
BUILDER_ROLES = ["roles/run.builder"]

IAP_AGENT = "service-{number}@gcp-sa-iap.iam.gserviceaccount.com"
COMPUTE_SA = "{number}-compute@developer.gserviceaccount.com"

TYPES = ("enable_api", "grant_deployer_roles", "grant_builder_role", "run_invoker_iap", "iap_access", "create_iap_agent")

# 提案の種類ごとに、実行に必要な情報
NEEDS = {
    "enable_api": ["project"],
    "grant_deployer_roles": ["project", "backend_sa"],
    "grant_builder_role": ["project", "project_number"],
    "run_invoker_iap": ["project", "project_number", "service", "region"],
    "iap_access": ["project", "service", "region", "owner_email"],
    "create_iap_agent": ["project"],
}

_API_RE = re.compile(r"\b([a-z0-9-]+\.googleapis\.com)\b")


def _rid(kind, *parts):
    return hashlib.sha256(("|".join([kind, *map(str, parts)])).encode()).hexdigest()[:10]


def make_request(kind, ctx, reason, api=None):
    """正規の形の提案を作る。ctx は {project, project_number, backend_sa, owner_email, service, region}。"""
    if kind not in TYPES:
        raise ValueError(f"未対応の権限の種類です: {kind}")
    req = {"type": kind, "reason": reason, "params": {}}
    if kind == "enable_api":
        if api not in ALLOWED_APIS:
            raise ValueError(f"有効化できないAPIです: {api}")
        req["params"] = {"api": api}
    elif kind == "grant_deployer_roles":
        req["params"] = {"member": ctx.get("backend_sa", ""), "roles": list(DEPLOYER_ROLES)}
    elif kind == "grant_builder_role":
        req["params"] = {"member": COMPUTE_SA.format(number=ctx.get("project_number", "")), "roles": list(BUILDER_ROLES)}
    elif kind == "run_invoker_iap":
        req["params"] = {"service": ctx.get("service", ""), "region": ctx.get("region", ""),
                         "member": IAP_AGENT.format(number=ctx.get("project_number", "")), "roles": ["roles/run.invoker"]}
    elif kind == "iap_access":
        req["params"] = {"service": ctx.get("service", ""), "region": ctx.get("region", ""),
                         "member": ctx.get("owner_email", ""), "roles": ["roles/iap.httpsResourceAccessor"]}
    req["id"] = _rid(kind, json.dumps(req["params"], sort_keys=True))
    return req


def validate_requests(requests, ctx):
    """クライアントから届いた提案を、許可リストに照らして作り直す。許可外は ValueError。"""
    if not isinstance(requests, list) or not requests:
        raise ValueError("権限の提案がありません")
    if len(requests) > 10:
        raise ValueError("提案が多すぎます")
    cleaned, seen = [], set()
    for r in requests:
        if not isinstance(r, dict):
            raise ValueError("提案の形式が正しくありません")
        kind = r.get("type")
        api = (r.get("params") or {}).get("api") if kind == "enable_api" else None
        req = make_request(kind, ctx, str(r.get("reason") or "")[:300], api=api)
        missing = [k for k in NEEDS[kind] if not ctx.get(k)]
        if missing:
            raise ValueError(f"{', '.join(missing)} を特定できないため、{kind} は実行できません")
        if req["id"] not in seen:
            seen.add(req["id"])
            cleaned.append(req)
    return cleaned


# ---------------------------------------------------------------- 失敗ログからの提案

def detect(text, stage, ctx):
    """gcloudの出力から、必要そうな権限の提案と、手動作業のヒントを作る。(requests, hints)"""
    low = text.lower()
    requests, hints = [], []

    # APIが無効
    apis = []
    for m in re.finditer(r"API \[([a-z0-9-]+\.googleapis\.com)\] not enabled|"
                         r"([a-z0-9-]+\.googleapis\.com) (?:has not been used|is not enabled|is disabled)", text):
        apis.append(m.group(1) or m.group(2))
    if "service_disabled" in low:
        apis += _API_RE.findall(text)
    for api in dict.fromkeys(apis):
        if api in ALLOWED_APIS:
            requests.append(make_request("enable_api", ctx, f"{api} が無効なため、デプロイに失敗しました。", api=api))

    denied = "permission_denied" in low or "does not have permission" in low or "permission denied" in low \
        or "iam.serviceaccounts.actas" in low or "forbidden" in low

    if re.search(r"gcp-sa-iap\.iam\.gserviceaccount\.com", text) and ("does not exist" in low or "invalid" in low):
        requests.append(make_request("create_iap_agent", ctx, "IAPのサービスエージェントがまだ作られていません。"))
        requests.append(make_request("run_invoker_iap", ctx, "IAPがCloud Runを呼び出せるようにします。"))
    elif stage == "iap_invoker" and denied:
        requests.append(make_request("run_invoker_iap", ctx, "IAPがCloud Runを呼び出せるようにするため。"))
    elif stage == "iap_access" and denied:
        requests.append(make_request(
            "iap_access", ctx, f"デプロイしたサービスを {ctx.get('owner_email', 'あなた')} だけが開けるようにするため。"))
    elif stage == "deploy" and denied:
        if ctx.get("backend_sa"):
            requests.append(make_request(
                "grant_deployer_roles", ctx, "バックエンドがCloud Runにデプロイするための基本のロールです。"))
        if ctx.get("project_number"):
            requests.append(make_request(
                "grant_builder_role", ctx, "Cloud Buildがソースからビルドしてデプロイするためのロールです。"))

    if any(s in low for s in ("oauth client", "oauth brand", "oauth consent", "use the cloud run ui", "brand")) \
            and "iap" in low:
        hints.append("IAPを初めて使うプロジェクトでは、Cloud Runのコンソールで一度だけIAPを有効にする必要があります"
                     "（OAuth同意画面の設定が必要です）。コンソールで設定したあと、もう一度デプロイしてください。")

    uniq = {}
    for r in requests:
        uniq.setdefault(r["id"], r)
    return list(uniq.values()), hints


# ---------------------------------------------------------------- 実行（ユーザー本人のトークンで）

def build_commands(req, ctx):
    """承認された提案を gcloud のコマンド列にする。"""
    project = ctx["project"]
    p = req["params"]
    kind = req["type"]
    cmds = []
    if kind == "enable_api":
        cmds.append(["services", "enable", p["api"], "--project", project])
    elif kind in ("grant_deployer_roles", "grant_builder_role"):
        for role in p["roles"]:
            cmds.append(["projects", "add-iam-policy-binding", project,
                         "--member", f"serviceAccount:{p['member']}", "--role", role, "--condition=None"])
    elif kind == "run_invoker_iap":
        cmds.append(["run", "services", "add-iam-policy-binding", p["service"], "--region", p["region"],
                     "--member", f"serviceAccount:{p['member']}", "--role", "roles/run.invoker", "--project", project])
    elif kind == "iap_access":
        cmds.append(["iap", "web", "add-iam-policy-binding", "--member", f"user:{p['member']}",
                     "--role", "roles/iap.httpsResourceAccessor", "--region", p["region"],
                     "--resource-type", "cloud-run", "--service", p["service"], "--project", project])
    elif kind == "create_iap_agent":
        cmds.append(["beta", "services", "identity", "create", "--service", "iap.googleapis.com", "--project", project])
    return cmds


def run_grants(requests, ctx, access_token):
    """ユーザーのアクセストークンで、承認済みの提案を順に実行する。トークンは一時ファイルに置き、必ず消す。"""
    results = []
    fd, token_file = tempfile.mkstemp(prefix="tok-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(access_token)
        os.chmod(token_file, 0o600)
        # --access-token-file: 「有効なアカウントの認証情報は無視され、このトークンだけで実行される」フラグ。
        # ファイルにはトークンだけを入れる（改行なども入れない）
        env = dict(os.environ)
        env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
        env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        for req in requests:
            ok, tail = True, []
            for args in build_commands(req, ctx):
                try:
                    r = subprocess.run([GCLOUD_BIN, *args, "--quiet", f"--access-token-file={token_file}"],
                                       capture_output=True, text=True, timeout=180, env=env)
                except (OSError, subprocess.TimeoutExpired) as e:
                    ok, tail = False, tail + [f"{type(e).__name__}: {e}"]
                    break
                tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
                if r.returncode != 0:
                    ok = False
                    break
            results.append({"id": req["id"], "type": req["type"], "ok": ok, "output": "\n".join(tail)[-600:]})
    finally:
        try:
            os.remove(token_file)
        except OSError:
            pass
    return results


# ---------------------------------------------------------------- 承認待ちの付与(メモリ上・短時間)

class GrantStore:
    TTL = 30 * 60

    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def _purge(self):
        now = time.time()
        for k in [k for k, v in self._d.items() if now - v["created"] > self.TTL]:
            del self._d[k]

    def create(self, email, requests, ctx):
        gid = secrets.token_urlsafe(18)
        with self._lock:
            self._purge()
            self._d[gid] = {"email": email, "requests": requests, "ctx": ctx, "status": "pending",
                            "results": [], "created": time.time()}
        return gid

    def get(self, gid):
        with self._lock:
            self._purge()
            entry = self._d.get(gid or "")
            return dict(entry) if entry else None

    def update(self, gid, **fields):
        with self._lock:
            if gid in self._d:
                self._d[gid].update(fields)
