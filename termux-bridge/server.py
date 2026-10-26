"""Termux Local Bridge: スマホ内で動くAPI。APK(WebView)から 127.0.0.1:8000 で呼ばれる。

ロジックは core.py にあり、ここはHTTPの入口だけ。
APIは X-Bridge-Token ヘッダー必須（トークンは ~/.auto-deploy/bridge_token。セットアップ時にAPKが決めた値）。
"""
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import core

app = FastAPI(title="Termux Local Bridge", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "https://localhost", "capacitor://localhost", "http://127.0.0.1"],
    allow_methods=["*"],
    allow_headers=["*"],
)

core.init()
TOKEN = core.load_token()
print(f"[bridge] token file: {core.APP_DIR / 'bridge_token'}", flush=True)


class FileItem(BaseModel):
    path: str
    content: str


class JobCreate(BaseModel):
    project_name: str
    source_content: str = ""
    files: list[FileItem] = Field(default_factory=list)
    target_env: str = "auto"


class Answers(BaseModel):
    answers: dict[str, str]


class Feedback(BaseModel):
    feedback: str = ""


class SessionBody(BaseModel):
    session: str
    backend_url: str | None = None


class ApproveBody(BaseModel):
    visibility: str = "private"   # private: ログインしたアカウントだけ / public: 誰でも


def auth(x_bridge_token: str | None = Header(default=None)):
    if not core.check_token(x_bridge_token):
        raise HTTPException(status_code=401, detail="トークンが正しくありません")


def guarded(fn, *args):
    try:
        return fn(*args)
    except core.JobError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@app.get("/")
def status():
    """認証なしのヘルスチェック（APKが起動確認に使う）。"""
    return {"status": "running", "environment": "termux-local", "version": "2.0.0"}


@app.get("/session", dependencies=[Depends(auth)])
def get_session():
    return core.session_info()


@app.post("/session", dependencies=[Depends(auth)])
def put_session(body: SessionBody):
    """APKがGoogleログインで受け取ったセッションを渡す。以後、ブリッジがバックエンドを呼ぶときに使う。"""
    guarded(core.set_session, body.session, body.backend_url)
    return core.session_info()


@app.delete("/session", dependencies=[Depends(auth)])
def delete_session():
    core.clear_session()
    return core.session_info()


@app.get("/jobs", dependencies=[Depends(auth)])
def jobs():
    return {"jobs": core.list_jobs()}


@app.post("/jobs", dependencies=[Depends(auth)])
def create_job(req: JobCreate):
    job_id = guarded(core.create_job, req.project_name, req.source_content,
                     [f.model_dump() for f in req.files], req.target_env)
    core.start_task(job_id, core.pipeline)
    return {"id": job_id}


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
def job_detail(job_id: str):
    return core.job_view(guarded(core.get_job, job_id))


@app.get("/jobs/{job_id}/file", dependencies=[Depends(auth)])
def job_file(job_id: str, path: str = Query(...)):
    return {"path": path, "content": guarded(core.read_job_file, job_id, path)}


@app.get("/jobs/{job_id}/log", dependencies=[Depends(auth)])
def job_log(job_id: str, offset: int = -1):
    guarded(core.get_job, job_id)
    text, new_offset = core.read_log(job_id, offset)
    return {"text": text, "offset": new_offset}


@app.post("/jobs/{job_id}/answers", dependencies=[Depends(auth)])
def job_answers(job_id: str, body: Answers):
    guarded(core.submit_answers, job_id, body.answers)
    return {"ok": True}


@app.post("/jobs/{job_id}/approve", dependencies=[Depends(auth)])
def job_approve(job_id: str, body: ApproveBody | None = None):
    guarded(core.approve, job_id, body.visibility if body else "private")
    return {"ok": True}


@app.post("/jobs/{job_id}/permissions/start", dependencies=[Depends(auth)])
def job_permissions_start(job_id: str):
    """権限付与の承認画面のURLを返す。ユーザーがGoogleの画面で承認するまで、何も実行されない。"""
    return {"url": guarded(core.start_grant, job_id)}


@app.post("/jobs/{job_id}/reject", dependencies=[Depends(auth)])
def job_reject(job_id: str):
    guarded(core.reject, job_id)
    return {"ok": True}


@app.post("/jobs/{job_id}/revise", dependencies=[Depends(auth)])
def job_revise(job_id: str, body: Feedback):
    guarded(core.revise, job_id, body.feedback)
    return {"ok": True}


@app.post("/jobs/{job_id}/check-deploy", dependencies=[Depends(auth)])
def job_check_deploy(job_id: str):
    return {"url": guarded(core.check_deploy, job_id)}


@app.post("/queue/sync", dependencies=[Depends(auth)])
def queue_sync():
    """オフライン待機中のジョブを再試行する。"""
    return {"started": core.sync_queued()}
