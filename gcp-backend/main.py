"""Auto-Deploy Agent: GCP側バックエンド（Cloud Run上で動く）

ログイン（ブラウザ経由・認証不要のURL）
  GET  /auth/login      外部ブラウザで開く。Googleのログイン画面へ
  GET  /auth/callback   Googleから戻ってくる場所
  POST /auth/poll       アプリがログイン結果（セッション）を受け取る
  GET  /auth/grant      権限付与の同意画面へ（アプリが /api/grants/start で作ったURL）

API（Authorization: Bearer <セッション> 必須。メールが ALLOWED_EMAILS に入っていること）
  GET  /auth/me
  POST /api/analyze        資料とコードを解析して、不足情報の質問か、成果物の案を返す
  POST /api/deploy         承認済みのファイルをCloud Runにデプロイ（既定は、ログインしたアカウント限定のIAP）
  GET  /api/service/{name} デプロイ済みサービスのURL
  POST /api/grants/start   必要な権限の付与を、ユーザーの承認つきで始める
  GET  /api/grants/{id}    付与の進み具合
"""
import html
import os
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

import auth
import core
import perms

app = FastAPI(title="Auto-Deploy Agent Backend", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "https://localhost", "capacitor://localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_NAME = "auto_deploy_analyzer"
USER_ID = "termux-bridge"
MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-2.5-flash")

_agent_state = {}
login_store = auth.LoginStore()
grant_store = perms.GrantStore()


# ---------------------------------------------------------------- モデル

class FileItem(BaseModel):
    path: str
    content: str


class AnalyzeRequest(BaseModel):
    project_name: str
    source_content: str = ""
    files: list[FileItem] = Field(default_factory=list)
    target_env: str = "auto"
    answers: dict[str, str] = Field(default_factory=dict)


class DeployRequest(BaseModel):
    project_name: str
    files: list[FileItem]
    service_name: str | None = None
    region: str | None = None
    port: int | None = None
    visibility: str = "private"   # private: ログインしたアカウントだけ / public: 誰でも


class PollRequest(BaseModel):
    id: str
    verifier: str


class GrantStartRequest(BaseModel):
    requests: list[dict]
    service: str
    region: str | None = None


def current_user(authorization: str | None = Header(default=None)):
    try:
        email = auth.bearer_email(authorization)
    except auth.AuthError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not email:
        raise HTTPException(status_code=401, detail="ログインが必要です（期限切れの場合はアプリで再ログインしてください）")
    return email


def page(title, message, status=200):
    body = (f"<!doctype html><html lang=ja><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            "<body style=\"font:16px/1.7 -apple-system,'Noto Sans JP',sans-serif;background:#0e131a;color:#e7edf5;"
            "max-width:30em;margin:12vh auto;padding:0 20px\">"
            f"<h1 style='font-size:20px'>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>")
    return HTMLResponse(body, status_code=status)


# ---------------------------------------------------------------- ADK

def _get_runner():
    """ADKは最初に使うときだけ読み込む（起動を軽くし、/ のヘルスチェックを安定させるため）。"""
    if "runner" not in _agent_state:
        from google.adk.agents import Agent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        sessions = InMemorySessionService()
        # 注意: instruction に { } を含めないこと（ADKがセッション変数として展開しようとする）
        agent = Agent(name="AutoDeployAnalyzer", model=MODEL_NAME, instruction=core.AGENT_INSTRUCTION)
        _agent_state["sessions"] = sessions
        _agent_state["runner"] = Runner(agent=agent, app_name=APP_NAME, session_service=sessions)
    return _agent_state["runner"], _agent_state["sessions"]


async def ask_agent_factory():
    """1リクエスト用のセッションを作り、ask(text) -> 返答テキスト と後始末の関数を返す。"""
    from google.genai import types

    runner, sessions = _get_runner()
    session_id = uuid.uuid4().hex
    await sessions.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)

    async def ask(text):
        message = types.Content(role="user", parts=[types.Part(text=text)])
        final = ""
        async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        return final

    async def cleanup():
        try:
            await sessions.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        except Exception:  # noqa: BLE001 - 後始末の失敗は無視してよい
            pass

    return ask, cleanup


# ---------------------------------------------------------------- ヘルスチェック・ログイン

@app.get("/")
def health_check():
    return {"status": "ok", "message": "GCP Auto-Deploy Agent Backend is running."}


@app.get("/auth/login")
def auth_login(id: str, c: str):
    try:
        login_store.create(id, c)
        url = auth.build_auth_url(auth.make_state("login", id), auth.LOGIN_SCOPES)
    except auth.AuthError as e:
        return page("ログインを始められません", str(e), 400)
    return RedirectResponse(url)


@app.get("/auth/grant")
def auth_grant(id: str):
    entry = grant_store.get(id)
    if not entry:
        return page("この承認は無効です", "有効期限が切れたか、すでに使われています。アプリからやり直してください。", 400)
    try:
        url = auth.build_auth_url(auth.make_state("grant", id), auth.GRANT_SCOPES,
                                  login_hint=entry["email"], prompt="consent")
    except auth.AuthError as e:
        return page("承認を始められません", str(e), 400)
    return RedirectResponse(url)


@app.get("/auth/callback")
def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    try:
        st = auth.read_state(state)
    except auth.AuthError as e:
        return page("設定が不足しています", str(e), 503)
    if error or not code or not st:
        return page("完了できませんでした", "Googleでの操作が取り消されたか、有効期限が切れました。アプリからやり直してください。", 400)
    try:
        tokens = auth.exchange_code(code)
        email = auth.email_from_id_token(tokens.get("id_token", ""))
        if st["kind"] == "login":
            if not auth.is_allowed(email):
                return page("このアカウントは許可されていません",
                            f"{email} は、このバックエンドの許可リスト（ALLOWED_EMAILS）に入っていません。", 403)
            login_store.complete(st["id"], email)
            return page("ログインできました", f"{email} でログインしました。アプリに戻ってください。")
        if st["kind"] == "grant":
            return finish_grant(st["id"], email, tokens)
    except auth.AuthError as e:
        return page("完了できませんでした", str(e), 400)
    return page("完了できませんでした", "不明な要求です。", 400)


@app.post("/auth/poll")
def auth_poll(req: PollRequest):
    status, email = login_store.poll(req.id, req.verifier)
    if status == "ready":
        return {"status": "ready", "session": auth.issue_session(email), "email": email}
    return {"status": status}


@app.get("/auth/me")
def auth_me(email: str = Depends(current_user)):
    return {"email": email}


# ---------------------------------------------------------------- 解析・デプロイ

@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest, email: str = Depends(current_user)):
    try:
        files = core.clean_files([f.model_dump() for f in req.files])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    prompt = core.build_prompt(req.project_name, req.target_env, req.source_content, files, req.answers)
    try:
        ask, cleanup = await ask_agent_factory()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"エージェントを初期化できません: {type(e).__name__}: {e}")
    try:
        return await core.run_analysis(ask, prompt)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"エージェントの返答を解釈できませんでした: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    finally:
        await cleanup()


@app.post("/api/deploy")
def deploy(req: DeployRequest, email: str = Depends(current_user)):
    try:
        params = core.prepare_deploy(
            req.project_name, [f.model_dump() for f in req.files],
            service_name=req.service_name, region=req.region, port=req.port,
            visibility=req.visibility, owner_email=email,
        )
        q = core.start_deploy(params)
    except core.DeployBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return StreamingResponse(core.iter_events(q), media_type="application/x-ndjson")


@app.get("/api/service/{name}")
def service_url(name: str, region: str | None = None, email: str = Depends(current_user)):
    url = core.get_service_url(core.slugify_service(name), region or core.DEFAULT_REGION)
    if not url:
        raise HTTPException(status_code=404, detail="サービスが見つかりません")
    return {"service": core.slugify_service(name), "url": url}


# ---------------------------------------------------------------- 権限付与（ユーザーの承認つき）

@app.post("/api/grants/start")
def grants_start(req: GrantStartRequest, email: str = Depends(current_user)):
    ctx = core.runtime_context(core.slugify_service(req.service), req.region, email)
    try:
        cleaned = perms.validate_requests(req.requests, ctx)
        base = auth.public_base()
    except (ValueError, auth.AuthError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    gid = grant_store.create(email, cleaned, ctx)
    return {"grant_id": gid, "url": f"{base}/auth/grant?id={gid}", "requests": cleaned}


@app.get("/api/grants/{gid}")
def grant_status(gid: str, email: str = Depends(current_user)):
    entry = grant_store.get(gid)
    if not entry or entry["email"] != email:
        raise HTTPException(status_code=404, detail="この承認は見つかりません")
    return {"status": entry["status"], "results": entry["results"], "requests": entry["requests"]}


def finish_grant(gid, email, tokens):
    """同意画面から戻ったとき、承認された提案をユーザー本人のトークンで1回だけ実行する。"""
    entry = grant_store.get(gid)
    if not entry or entry["status"] != "pending":
        return page("この承認は使えません", "有効期限が切れたか、すでに実行されています。", 400)
    if email != entry["email"]:
        return page("別のアカウントです", f"{entry['email']} で承認してください（{email} でログインされています）。", 403)
    token = tokens.get("access_token")
    if not token or "cloud-platform" not in (tokens.get("scope") or ""):
        return page("権限が許可されていません", "Googleの同意画面で、Google Cloudの権限を許可してください。", 400)
    grant_store.update(gid, status="running")
    results = perms.run_grants(entry["requests"], entry["ctx"], token)
    ok = all(r["ok"] for r in results)
    grant_store.update(gid, status="done" if ok else "failed", results=results)
    if ok:
        return page("権限を付与しました", "アプリに戻ってください。デプロイを続けます。")
    failed = ", ".join(r["type"] for r in results if not r["ok"])
    return page("一部の付与に失敗しました", f"失敗: {failed}。詳しくはアプリのログを確認してください。", 500)
