"""Termuxブリッジの中核ロジック（FastAPIに依存しない）。

ジョブの流れ:
  queued -> analyzing -> (needs_info) -> drafting -> trial_running -> awaiting_approval
         -> deploying -> deployed
  途中で failed / rejected になることがある。

ログは ~/deploy-logs/<プロジェクト>/<日時>_<ジョブID>.log に残る（GCP側のログもここに追記）。
"""
import base64
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

import requests

HOME = Path.home()
APP_DIR = Path(os.environ.get("AUTO_DEPLOY_HOME", str(HOME / ".auto-deploy")))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", str(HOME / "auto-deploy-workspace")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(HOME / "deploy-logs")))
DB_PATH = Path(os.environ.get("DB_PATH", str(APP_DIR / "jobs.db")))

BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8000"))
GCP_BACKEND_URL = os.environ.get("GCP_BACKEND_URL", "").rstrip("/")
GRANT_WAIT_SECONDS = int(os.environ.get("GRANT_WAIT_SECONDS", "600"))
TRIAL_RUN = os.environ.get("TRIAL_RUN", "1") != "0"
INSTALL_TIMEOUT = int(os.environ.get("INSTALL_TIMEOUT", "600"))
START_TIMEOUT = int(os.environ.get("START_TIMEOUT", "45"))

MAX_INPUT_FILE_BYTES = 200_000
MAX_INPUT_TOTAL_BYTES = 1_000_000
MAX_DEPLOY_FILE_BYTES = 1_000_000
MAX_DEPLOY_TOTAL_BYTES = 10_000_000
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}

ALLOWED_EXECUTABLES = {"python", "python3", "pip", "pip3", "node", "npm", "npx", "uvicorn", "gunicorn"}
SHELL_TOKENS = {"&&", "||", ";", "|", "&", ">", ">>", "<"}
TRIAL_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PREFIX", "TERM", "TZ",
                  "LD_LIBRARY_PATH", "LD_PRELOAD", "ANDROID_ROOT", "ANDROID_DATA")

ACTIVE_STATUSES = ("queued", "analyzing", "drafting", "trial_running", "deploying")
LOGIN_REQUIRED_MESSAGE = "Googleのログインが必要です。アプリで（再）ログインしてください"


class JobError(Exception):
    """利用者に見せてよいエラー。statusはHTTPステータスの目安。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class BackendOffline(Exception):
    pass


class AuthRequired(Exception):
    """ログインが無い／期限切れ。"""


# ---------------------------------------------------------------- 初期化・認証

_db_lock = threading.RLock()
_token = None


def init():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_name TEXT NOT NULL,
                source_content TEXT NOT NULL DEFAULT '',
                input_files TEXT NOT NULL DEFAULT '[]',
                target_env TEXT NOT NULL DEFAULT 'auto',
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT '',
                questions TEXT NOT NULL DEFAULT '[]',
                answers TEXT NOT NULL DEFAULT '{}',
                analysis TEXT NOT NULL DEFAULT 'null',
                artifacts TEXT NOT NULL DEFAULT '[]',
                trial TEXT NOT NULL DEFAULT 'null',
                deploy_url TEXT,
                visibility TEXT NOT NULL DEFAULT 'private',
                permissions TEXT NOT NULL DEFAULT 'null',
                error TEXT,
                log_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        _migrate(conn)
    load_backend_url()
    recover_interrupted()


def _migrate(conn):
    """古いバージョンのDBに、新しい列を足す。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in (("visibility", "TEXT NOT NULL DEFAULT 'private'"), ("permissions", "TEXT NOT NULL DEFAULT 'null'")):
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")


def load_token():
    """APK(WebView)とブリッジの間の合言葉。環境変数が無ければファイルに保存して使い回す。"""
    global _token
    env = os.environ.get("BRIDGE_TOKEN")
    if env:
        _token = env
        return _token
    f = APP_DIR / "bridge_token"
    if f.exists() and f.read_text().strip():
        _token = f.read_text().strip()
    else:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        _token = secrets.token_urlsafe(24)
        f.write_text(_token)
        try:
            f.chmod(0o600)
        except OSError:
            pass
    return _token


def check_token(provided):
    return bool(_token) and bool(provided) and hmac.compare_digest(provided, _token)


# ---------------------------------------------------------------- ログインのセッション

SESSION_FILE = APP_DIR / "session"
BACKEND_FILE = APP_DIR / "backend_url"


def get_session():
    try:
        return SESSION_FILE.read_text().strip()
    except OSError:
        return ""


def set_session(token, backend_url=None):
    """APKがログインして受け取ったセッションを保存する（バックエンドを呼ぶときに使う）。"""
    global GCP_BACKEND_URL
    token = (token or "").strip()
    if token.count(".") != 1 or len(token) > 2000:
        raise JobError("セッションの形式が正しくありません")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(token)
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass
    if backend_url:
        if not re.match(r"^https?://[^\s/]+(/\S*)?$", backend_url.strip()):
            raise JobError("バックエンドのURLが正しくありません")
        GCP_BACKEND_URL = backend_url.strip().rstrip("/")
        BACKEND_FILE.write_text(GCP_BACKEND_URL)


def clear_session():
    try:
        SESSION_FILE.unlink()
    except OSError:
        pass


def load_backend_url():
    global GCP_BACKEND_URL
    if not GCP_BACKEND_URL:
        try:
            GCP_BACKEND_URL = BACKEND_FILE.read_text().strip().rstrip("/")
        except OSError:
            pass


def session_info():
    """表示用。署名の検証はバックエンドがするので、ここでは中身を読むだけ。"""
    token = get_session()
    info = {"logged_in": False, "email": None, "expires": None, "backend_url": GCP_BACKEND_URL or None}
    if not token:
        return info
    try:
        body = token.split(".")[0]
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        exp = int(payload.get("exp", 0))
        info.update(email=payload.get("email"), expires=exp, logged_in=exp > time.time())
    except (ValueError, TypeError):
        pass
    return info


# ---------------------------------------------------------------- DB

@contextmanager
def db():
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


_JSON_FIELDS = ("input_files", "questions", "answers", "analysis", "artifacts", "trial", "permissions")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _row_to_job(row):
    job = dict(row)
    for k in _JSON_FIELDS:
        job[k] = json.loads(job[k]) if job.get(k) else None
    return job


def get_job(job_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise JobError("ジョブが見つかりません", 404)
    return _row_to_job(row)


def update_job(job_id, **fields):
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(json.dumps(v, ensure_ascii=False) if k in _JSON_FIELDS else v)
    cols.append("updated_at = ?")
    vals.append(_now())
    vals.append(job_id)
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id = ?", vals)


def set_status(job_id, status, message=""):
    update_job(job_id, status=status, message=message)
    jlog(job_id, "status", f"{status}: {message}" if message else status)


def list_jobs(limit=50):
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [job_view(_row_to_job(r)) for r in rows]


def recover_interrupted():
    """ブリッジが再起動したとき、途中だったジョブを失敗扱いにして再開できるようにする。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, status FROM jobs WHERE status IN ('analyzing','drafting','trial_running','deploying')"
        ).fetchall()
    for r in rows:
        if r["status"] == "deploying":
            msg = "デプロイ中にブリッジが停止しました。「結果を確認」でGCP側の状態を見られます"
        else:
            msg = "処理中にブリッジが停止しました。「やり直す」で再実行できます"
        update_job(r["id"], status="failed", message=msg, error="interrupted")
        jlog(r["id"], "status", msg)


def job_view(job):
    """APKに返す形。ファイルの中身など大きいものは含めない。"""
    a = job.get("analysis") or {}
    analysis = None
    if a:
        analysis = {
            "summary": a.get("summary", ""), "stack": a.get("stack", ""), "notes": a.get("notes", ""),
            "run": a.get("run"), "deploy": a.get("deploy"),
        }
    return {
        "id": job["id"], "project_name": job["project_name"], "target_env": job["target_env"],
        "status": job["status"], "message": job["message"], "questions": job["questions"] or [],
        "answers": job["answers"] or {}, "analysis": analysis, "artifacts": job["artifacts"] or [],
        "trial": job["trial"], "deploy_url": job["deploy_url"], "visibility": job["visibility"],
        "permissions": job["permissions"], "error": job["error"],
        "log_path": job["log_path"], "created_at": job["created_at"], "updated_at": job["updated_at"],
        "input_file_count": len(job["input_files"] or []),
    }


# ---------------------------------------------------------------- ログ

_log_paths = {}


def _log_path(job_id):
    if job_id not in _log_paths:
        _log_paths[job_id] = get_job(job_id)["log_path"]
    return _log_paths[job_id]


def jlog(job_id, stage, message):
    try:
        path = _log_path(job_id)
        with open(path, "a", encoding="utf-8") as f:
            for line in str(message).splitlines() or [""]:
                f.write(f"[{datetime.now():%H:%M:%S}] [{stage}] {line}\n")
    except (OSError, JobError):
        pass


def read_log(job_id, offset=0, max_bytes=64_000):
    """offset=-1 なら末尾16KBから。戻り値は (テキスト, 次のoffset)。"""
    path = _log_path(job_id)
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0
    if offset < 0:
        offset = max(0, size - 16_000)
    if offset > size:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    return data.decode("utf-8", errors="replace"), offset + len(data)


# ---------------------------------------------------------------- パス・コマンド検証

def safe_rel_path(p):
    if not isinstance(p, str) or not p.strip():
        raise JobError("ファイル名が空です")
    p = p.replace("\\", "/").strip()
    if "\x00" in p or len(p) > 200 or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise JobError(f"使えないファイル名です: {p[:60]}")
    parts = PurePosixPath(p).parts
    if not parts or ".." in parts:
        raise JobError(f"使えないファイル名です: {p[:60]}")
    return "/".join(parts)


def slug(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name or "").strip("-").lower()[:40] or "project"


def parse_command(cmd, port):
    """仮実行で動かすコマンドを検証して argv にする。シェルは使わず、実行ファイルは許可リスト制。"""
    cmd = (cmd or "").replace("{port}", str(port))
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise ValueError(f"コマンドを解析できません: {e}")
    if not argv:
        raise ValueError("コマンドが空です")
    if any(t in SHELL_TOKENS for t in argv):
        raise ValueError("シェルの演算子（&& ; | > など）は使えません")
    if "/" in argv[0] or argv[0] not in ALLOWED_EXECUTABLES:
        raise ValueError(f"許可されていない実行ファイルです: {argv[0]}")
    return argv


# ---------------------------------------------------------------- ジョブ作成

def create_job(project_name, source_content, files, target_env="auto"):
    project_name = (project_name or "").strip()
    if not project_name or len(project_name) > 80:
        raise JobError("プロジェクト名は1〜80文字で入力してください")
    files = files or []
    clean, total = [], 0
    for f in files:
        path = safe_rel_path(f.get("path"))
        content = f.get("content")
        if not isinstance(content, str):
            raise JobError(f"ファイルの内容が文字列ではありません: {path}")
        size = len(content.encode("utf-8"))
        if size > MAX_INPUT_FILE_BYTES:
            raise JobError(f"ファイルが大きすぎます（{MAX_INPUT_FILE_BYTES // 1000}KBまで）: {path}")
        total += size
        clean.append({"path": path, "content": content})
    if total > MAX_INPUT_TOTAL_BYTES:
        raise JobError("ファイルの合計サイズが大きすぎます（1MBまで）")
    if len({f["path"] for f in clean}) != len(clean):
        raise JobError("同じ名前のファイルが複数あります")
    if not clean and not (source_content or "").strip():
        raise JobError("説明かファイルのどちらかは必要です")

    job_id = uuid.uuid4().hex[:8]
    log_path = LOG_DIR / slug(project_name) / f"{datetime.now():%Y%m%d-%H%M%S}_{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = _now()
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, project_name, source_content, input_files, target_env, status, message, "
            "log_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', '受け付けました', ?, ?, ?)",
            (job_id, project_name, source_content or "", json.dumps(clean, ensure_ascii=False),
             target_env or "auto", str(log_path), now, now),
        )
    jlog(job_id, "job", f"作成: {project_name} / 入力ファイル {len(clean)} 件")
    return job_id


# ---------------------------------------------------------------- バックグラウンド実行

_active = set()
_active_lock = threading.Lock()
_trial_lock = threading.Lock()


def start_task(job_id, fn, pre=None):
    """ジョブの処理をスレッドで始める。pre は枠を確保した直後・スレッド開始前に同期実行される
    （状態を先に切り替えて、画面に古い状態が残る時間をなくすため）。"""
    with _active_lock:
        if job_id in _active:
            return False
        _active.add(job_id)
    if pre:
        try:
            pre()
        except Exception:
            with _active_lock:
                _active.discard(job_id)
            raise

    def runner():
        try:
            fn(job_id)
        except AuthRequired as e:
            jlog(job_id, "auth", str(e))
            update_job(job_id, status="failed", error="auth_required", message=str(e))
        except Exception as e:  # noqa: BLE001
            jlog(job_id, "error", traceback.format_exc())
            update_job(job_id, status="failed", error=f"{type(e).__name__}: {e}", message="内部エラーが発生しました")
        finally:
            with _active_lock:
                _active.discard(job_id)

    threading.Thread(target=runner, daemon=True).start()
    return True


# ---------------------------------------------------------------- GCPバックエンド呼び出し

def _headers():
    return {"Authorization": "Bearer " + get_session()}


def _require_backend():
    if not GCP_BACKEND_URL:
        raise RuntimeError("バックエンドのURLが未設定です（アプリの設定で入力してください）")
    if not get_session():
        raise AuthRequired(LOGIN_REQUIRED_MESSAGE)


def _check_status(r):
    """バックエンドの応答を確認する。401はログインのやり直しとして扱う。"""
    if r.status_code == 401:
        raise AuthRequired(LOGIN_REQUIRED_MESSAGE)
    if r.status_code != 200:
        raise RuntimeError(f"バックエンドがエラーを返しました ({r.status_code}): {r.text[:300]}")


def call_analyze(job):
    _require_backend()
    payload = {
        "project_name": job["project_name"], "source_content": job["source_content"],
        "files": job["input_files"], "target_env": job["target_env"], "answers": job["answers"] or {},
    }
    try:
        r = requests.post(f"{GCP_BACKEND_URL}/api/analyze", json=payload, headers=_headers(), timeout=(10, 180))
    except requests.ConnectionError as e:
        raise BackendOffline(str(e))
    _check_status(r)
    return r.json()


# ---------------------------------------------------------------- 解析 → 成果物 → 仮実行

def pipeline(job_id):
    job = get_job(job_id)
    set_status(job_id, "analyzing", "資料とコードを解析しています")
    try:
        analysis = call_analyze(job)
    except BackendOffline as e:
        jlog(job_id, "analyze", f"バックエンドに接続できません: {e}")
        set_status(job_id, "queued", "オフラインのため待機中。つながったら「再試行」を押してください")
        return

    if analysis.get("status") == "needs_info":
        update_job(job_id, questions=analysis.get("questions", []))
        jlog(job_id, "analyze", f"質問 {len(analysis.get('questions', []))} 件")
        set_status(job_id, "needs_info", "確認したいことがあります")
        return

    update_job(job_id, analysis=analysis, questions=[], error=None, trial=None, permissions=None)
    jlog(job_id, "analyze", f"{analysis.get('stack', '')} / {analysis.get('summary', '')}")

    set_status(job_id, "drafting", "成果物を作成しています")
    artifacts = write_workspace(job_id, job["input_files"], analysis.get("files", []))
    update_job(job_id, artifacts=artifacts)

    run = analysis.get("run") or {}
    if TRIAL_RUN and (run.get("command") or "").strip():
        set_status(job_id, "trial_running", "仮実行しています")
        trial = trial_run(job_id, run)
    else:
        reason = "仮実行は無効です" if not TRIAL_RUN else "エージェントが仮実行不要と判断しました"
        trial = {"skipped": True, "ok": None, "detail": reason}
        jlog(job_id, "trial", reason)
    update_job(job_id, trial=trial)
    set_status(job_id, "awaiting_approval", "内容を確認して承認してください")


def src_dir(job_id):
    return WORKSPACE_DIR / job_id / "src"


def write_workspace(job_id, input_files, generated):
    base = src_dir(job_id)
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    artifacts, origin = {}, {}
    for f in input_files:
        origin[f["path"]] = "user"
        _write(base, f["path"], f["content"])
        artifacts[f["path"]] = len(f["content"].encode("utf-8"))
    for f in generated:
        path = safe_rel_path(f["path"])
        overwrote = path in origin
        origin[path] = "generated"
        _write(base, path, f["content"])
        artifacts[path] = len(f["content"].encode("utf-8"))
        jlog(job_id, "draft", f"生成: {path}" + (" （同名の入力ファイルを上書き）" if overwrote else ""))
    return [{"path": p, "size": artifacts[p], "origin": origin[p]} for p in sorted(artifacts)]


def _write(base, rel, content):
    dest = base / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")


def read_job_file(job_id, rel):
    get_job(job_id)
    rel = safe_rel_path(rel)
    p = src_dir(job_id) / rel
    if not p.is_file() or src_dir(job_id).resolve() not in p.resolve().parents:
        raise JobError("ファイルが見つかりません", 404)
    if p.stat().st_size > 200_000:
        raise JobError("大きいファイルは表示できません")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise JobError("テキストではないファイルです")


def _port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _trial_env(src, port):
    env = {k: os.environ[k] for k in TRIAL_ENV_KEYS if k in os.environ}
    env["PORT"] = str(port)
    venv = src / ".venv"
    if venv.exists():
        env["PATH"] = f"{venv}/bin:" + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(venv)
    return env


def trial_run(job_id, run):
    """Dockerなしで、インストール→起動→ヘルスチェック→停止 を行う。APIキーなどの秘密は渡さない。"""
    result = {"ok": False, "skipped": False, "port": None, "status_code": None, "detail": "", "steps": []}
    base = src_dir(job_id)
    port = int(run.get("port") or 8080)
    if _port_in_use(port):
        # 別のサーバーが使っているポートでヘルスチェックすると、そちらの応答で誤って成功してしまう
        new_port = _free_port()
        jlog(job_id, "trial", f"ポート {port} は使用中のため {new_port} で仮実行します")
        port = new_port
    result["port"] = port
    health = run.get("health_path") or "/"

    try:
        command = parse_command(run.get("command", ""), port)
        install_text = (run.get("install") or "").strip()
        if not install_text and (base / "requirements.txt").exists():
            install_text = "pip install -r requirements.txt"
        install = parse_command(install_text, port) if install_text else None
    except ValueError as e:
        result["detail"] = f"仮実行をスキップしました: {e}"
        result["skipped"] = True
        result["ok"] = None
        jlog(job_id, "trial", result["detail"])
        return result

    with _trial_lock, open(_log_path(job_id), "ab") as logf:
        if (base / "requirements.txt").exists() and not (base / ".venv").exists():
            jlog(job_id, "trial", "仮想環境(.venv)を作成します")
            subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", ".venv"],
                           cwd=base, stdout=logf, stderr=subprocess.STDOUT)
        env = _trial_env(base, port)

        if install:
            jlog(job_id, "trial", "$ " + " ".join(install))
            logf.flush()
            t0 = time.time()
            try:
                rc = subprocess.run(install, cwd=base, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                    timeout=INSTALL_TIMEOUT).returncode
                step = {"name": "install", "ok": rc == 0, "seconds": round(time.time() - t0, 1)}
            except subprocess.TimeoutExpired:
                step = {"name": "install", "ok": False, "seconds": INSTALL_TIMEOUT, "detail": "タイムアウト"}
            except OSError as e:
                step = {"name": "install", "ok": False, "seconds": 0, "detail": str(e)}
            result["steps"].append(step)
            if not step["ok"]:
                result["detail"] = "依存パッケージのインストールに失敗しました（詳細はログ）"
                jlog(job_id, "trial", result["detail"])
                return result

        jlog(job_id, "trial", "$ " + " ".join(command) + f"  (PORT={port})")
        logf.flush()
        t0 = time.time()
        try:
            proc = subprocess.Popen(command, cwd=base, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as e:
            result["detail"] = f"起動できませんでした: {e}"
            result["steps"].append({"name": "start", "ok": False, "seconds": 0})
            jlog(job_id, "trial", result["detail"])
            return result

        try:
            url = f"http://127.0.0.1:{port}{health}"
            deadline = time.time() + START_TIMEOUT
            while time.time() < deadline:
                if proc.poll() is not None:
                    result["detail"] = f"プロセスが途中で終了しました (code {proc.returncode})"
                    break
                try:
                    r = requests.get(url, timeout=2)
                    result["status_code"] = r.status_code
                    if r.status_code < 500:
                        result["ok"] = True
                        result["detail"] = f"{url} が応答しました (HTTP {r.status_code})"
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                result["detail"] = f"{START_TIMEOUT}秒以内に正常な応答がありませんでした"
            result["steps"].append({"name": "start", "ok": bool(result["ok"]), "seconds": round(time.time() - t0, 1)})
        finally:
            _stop_process(proc)
    jlog(job_id, "trial", result["detail"])
    return result


def _stop_process(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)


# ---------------------------------------------------------------- ユーザー操作

def submit_answers(job_id, answers):
    job = get_job(job_id)
    if job["status"] != "needs_info":
        raise JobError("いまは質問に回答できる状態ではありません", 409)
    merged = dict(job["answers"] or {})
    for k, v in (answers or {}).items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            merged[k] = v.strip()[:1000]
    if not merged:
        raise JobError("回答を入力してください")
    update_job(job_id, answers=merged)
    jlog(job_id, "user", "質問に回答しました")
    _start_or_409(job_id, pipeline, "analyzing", "回答をもとに再解析しています")


def revise(job_id, feedback):
    job = get_job(job_id)
    if job["status"] not in ("awaiting_approval", "rejected", "failed", "needs_permission"):
        raise JobError("いまは修正を依頼できる状態ではありません", 409)
    merged = dict(job["answers"] or {})
    feedback = (feedback or "").strip()
    if feedback:
        merged["revision_feedback"] = feedback[:1000]
    update_job(job_id, answers=merged, error=None)
    jlog(job_id, "user", "修正を依頼: " + (feedback or "（コメントなし・やり直し）"))
    _start_or_409(job_id, pipeline, "analyzing", "やり直しています")


def approve(job_id, visibility="private"):
    job = get_job(job_id)
    if job["status"] not in ("awaiting_approval", "needs_permission"):   # 権限を整えた後の再実行もここから
        raise JobError("承認待ちのジョブではありません", 409)
    if visibility not in ("private", "public"):
        raise JobError("公開範囲は private か public にしてください")
    update_job(job_id, visibility=visibility, permissions=None)
    who = "ログインしたアカウントだけ" if visibility == "private" else "誰でも（公開）"
    jlog(job_id, "user", f"承認しました（公開範囲: {who}）")
    _start_or_409(job_id, deploy_task, "deploying", "デプロイを開始します")


def reject(job_id):
    job = get_job(job_id)
    if job["status"] not in ("awaiting_approval", "needs_info", "failed", "queued", "needs_permission"):
        raise JobError("いまは却下できる状態ではありません", 409)
    set_status(job_id, "rejected", "却下しました（デプロイしていません）")
    jlog(job_id, "user", "却下しました")


def sync_queued():
    """オフラインで待機中のジョブを再試行する。"""
    started = 0
    for j in list_jobs(200):
        if j["status"] == "queued" and start_task(j["id"], pipeline):
            started += 1
    return started


def _start_or_409(job_id, fn, status, message):
    for _ in range(20):  # 直前のスレッドが片付くのを最大2秒待つ
        if start_task(job_id, fn, pre=lambda: set_status(job_id, status, message)):
            return
        time.sleep(0.1)
    raise JobError("このジョブは処理中です", 409)


# ---------------------------------------------------------------- デプロイ

def collect_deploy_files(base):
    files, skipped, total = [], [], 0
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            p = Path(root) / n
            rel = p.relative_to(base).as_posix()
            size = p.stat().st_size
            if size > MAX_DEPLOY_FILE_BYTES:
                skipped.append(f"{rel} (大きすぎる)")
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{rel} (テキストではない)")
                continue
            total += size
            if total > MAX_DEPLOY_TOTAL_BYTES:
                raise RuntimeError("デプロイするファイルの合計サイズが大きすぎます")
            files.append({"path": rel, "content": content})
    return files, skipped


def deploy_task(job_id):
    job = get_job(job_id)
    analysis = job["analysis"] or {}
    deploy = analysis.get("deploy") or {}
    _require_backend()
    files, skipped = collect_deploy_files(src_dir(job_id))
    jlog(job_id, "deploy", f"送信するファイル: {len(files)} 件" + (f" / 除外: {', '.join(skipped)}" if skipped else ""))
    payload = {
        "project_name": job["project_name"], "files": files,
        "service_name": deploy.get("service_name"), "region": deploy.get("region"),
        "port": (analysis.get("run") or {}).get("port"), "visibility": job["visibility"],
    }
    ok, url, error, perm = None, None, None, None
    try:
        with requests.post(f"{GCP_BACKEND_URL}/api/deploy", json=payload, headers=_headers(),
                           stream=True, timeout=(10, 120)) as r:
            _check_status(r)
            r.encoding = "utf-8"
            for raw in r.iter_lines(decode_unicode=True):
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    jlog(job_id, "gcp", raw)
                    continue
                kind = ev.get("event")
                if kind == "log":
                    jlog(job_id, "gcp", ev.get("line", ""))
                elif kind == "permission_needed":
                    perm = {"requests": ev.get("requests", []), "hints": ev.get("hints", []),
                            "service": ev.get("service"), "region": ev.get("region"), "waiting": False}
                elif kind == "result":
                    ok, url, error = ev.get("ok"), ev.get("url"), ev.get("error")
    except requests.RequestException as e:
        jlog(job_id, "deploy", f"接続が切れました: {e}")
        update_job(job_id, status="failed", error="connection_lost",
                   message="通信が切れました。GCP側ではデプロイが続いている可能性があります。「結果を確認」を押してください")
        return

    if ok and url:
        update_job(job_id, deploy_url=url, error=None, permissions=None)
        who = "ログインしたアカウント限定" if job["visibility"] == "private" else "公開"
        set_status(job_id, "deployed", f"デプロイが完了しました（{who}）")
    elif ok:
        set_status(job_id, "deployed", "デプロイは成功しましたが、URLを取得できませんでした")
    elif perm and (perm["requests"] or perm["hints"]):
        update_job(job_id, permissions=perm, error=error)
        for r_ in perm["requests"]:
            jlog(job_id, "permission", f"承認が必要: {r_['type']} - {r_.get('reason', '')}")
        for h_ in perm["hints"]:
            jlog(job_id, "permission", f"手動で必要: {h_}")
        set_status(job_id, "needs_permission",
                   "GCPの権限の承認が必要です" if perm["requests"] else "GCPで手動の設定が必要です")
    else:
        update_job(job_id, error=error or "unknown")
        set_status(job_id, "failed", f"デプロイに失敗しました: {error or '詳細はログを確認してください'}")


GRANT_POLL_SECONDS = float(os.environ.get("GRANT_POLL_SECONDS", "2"))


def start_grant(job_id):
    """必要な権限の付与を、ユーザーの承認つきで始める。承認画面のURLを返す（承認するまで何も実行されない）。"""
    job = get_job(job_id)
    perm = job["permissions"] or {}
    if job["status"] != "needs_permission" or not perm.get("requests"):
        raise JobError("承認が必要な権限はありません", 409)
    if perm.get("waiting") and perm.get("url"):
        return perm["url"]
    _require_backend()
    deploy = (job["analysis"] or {}).get("deploy") or {}
    try:
        r = requests.post(f"{GCP_BACKEND_URL}/api/grants/start", headers=_headers(), timeout=30, json={
            "requests": perm["requests"], "service": perm.get("service") or deploy.get("service_name"),
            "region": perm.get("region") or deploy.get("region")})
    except requests.RequestException as e:
        raise JobError(f"バックエンドに接続できません: {e}", 502)
    if r.status_code == 401:
        raise JobError(LOGIN_REQUIRED_MESSAGE, 401)
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise JobError(f"バックエンドが受け付けませんでした: {detail}", 400)
    data = r.json()
    perm.update(waiting=True, url=data["url"], grant_id=data["grant_id"], results=[],
                requests=data["requests"])   # サーバーが検証して作り直した正規の形に置き換える
    update_job(job_id, permissions=perm, message="Googleの画面で承認してください")
    jlog(job_id, "grant", "承認画面を用意しました（承認するまで何も実行されません）")
    start_task(job_id, grant_watch)
    return data["url"]


def _grant_done(job_id, results, message):
    perm = get_job(job_id)["permissions"] or {}
    perm.update(waiting=False, results=results)
    update_job(job_id, permissions=perm)
    set_status(job_id, "needs_permission", message)


def grant_watch(job_id):
    """承認の結果を待ち、付与できたらデプロイを自動で再開する。"""
    perm = get_job(job_id)["permissions"] or {}
    gid = perm.get("grant_id")
    deadline = time.time() + GRANT_WAIT_SECONDS
    missing = 0
    while time.time() < deadline:
        try:
            r = requests.get(f"{GCP_BACKEND_URL}/api/grants/{gid}", headers=_headers(), timeout=20)
        except requests.RequestException:
            time.sleep(GRANT_POLL_SECONDS)
            continue
        if r.status_code == 401:
            raise AuthRequired(LOGIN_REQUIRED_MESSAGE)
        if r.status_code == 404:
            missing += 1
            if missing >= 3:
                jlog(job_id, "grant", "承認の情報がバックエンドから失われました")
                _grant_done(job_id, [], "承認の情報が失われました（バックエンドが再起動した可能性があります）。もう一度承認してください")
                return
        elif r.status_code == 200:
            data = r.json()
            if data["status"] == "done":
                kinds = ", ".join(x["type"] for x in data["results"])
                jlog(job_id, "grant", f"権限を付与しました: {kinds}")
                _grant_done(job_id, data["results"], "権限を付与しました")
                set_status(job_id, "deploying", "権限を付与しました。デプロイを再開します")
                deploy_task(job_id)
                return
            if data["status"] == "failed":
                for x in data["results"]:
                    jlog(job_id, "grant", f"{x['type']}: {'OK' if x['ok'] else '失敗'} {x.get('output', '')}")
                _grant_done(job_id, data["results"], "一部の権限を付与できませんでした（詳細はログ）")
                return
        time.sleep(GRANT_POLL_SECONDS)
    jlog(job_id, "grant", "承認を確認できないまま時間切れになりました")
    _grant_done(job_id, [], "承認を確認できませんでした（時間切れ）。もう一度「承認する」を押してください")


def check_deploy(job_id):
    """通信が切れた後などに、GCP側のサービスURLを問い合わせる。"""
    job = get_job(job_id)
    if job["status"] not in ("failed", "deploying", "deployed"):
        raise JobError("デプロイを行ったジョブではありません", 409)
    deploy = (job["analysis"] or {}).get("deploy") or {}
    service = deploy.get("service_name")
    if not service:
        raise JobError("サービス名が分かりません")
    _require_backend()
    try:
        r = requests.get(f"{GCP_BACKEND_URL}/api/service/{service}", headers=_headers(),
                         params={"region": deploy.get("region")} if deploy.get("region") else None, timeout=60)
    except requests.RequestException as e:
        raise JobError(f"バックエンドに接続できません: {e}", 502)
    if r.status_code == 401:
        raise JobError(LOGIN_REQUIRED_MESSAGE, 401)
    if r.status_code == 404:
        raise JobError("GCP側にまだサービスがありません（デプロイは完了していません）", 404)
    if r.status_code != 200:
        raise JobError(f"バックエンドがエラーを返しました ({r.status_code})", 502)
    url = r.json().get("url")
    update_job(job_id, deploy_url=url, error=None)
    jlog(job_id, "deploy", f"サービスのURLを確認しました: {url}（最新リビジョンかどうかは未確認）")
    set_status(job_id, "deployed", "サービスが稼働しています（最新の内容かはログで確認してください）")
    return url
