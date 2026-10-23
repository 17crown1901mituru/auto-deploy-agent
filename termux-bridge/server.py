import os
import sqlite3
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Termux Local Bridge & Queue", version="1.0.0")

DB_PATH = "local_queue.db"
GCP_BACKEND_URL = os.environ.get("GCP_BACKEND_URL", "https://your-cloud-run-url.a.run.app")

# SQLiteの初期化（オフラインキュー用）
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deploy_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT,
            source_content TEXT,
            target_env TEXT,
            status TEXT DEFAULT 'pending' -- 'pending', 'synced', 'failed'
        )
    """)
    conn.commit()
    conn.close()

init_db()

class QueueRequest(BaseModel):
    project_name: str
    source_content: str
    target_env: str = "auto"

@app.get("/")
def status():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM deploy_queue WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]
    conn.close()
    return {"status": "running", "environment": "termux-local", "pending_tasks": pending_count}

# 1. スマホ (APK) からの指示をローカルに一時保存（オフラインキューイング）
@app.post("/queue/add")
def add_to_queue(req: QueueRequest):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO deploy_queue (project_name, source_content, target_env, status) VALUES (?, ?, ?, 'pending')",
            (req.project_name, req.source_content, req.target_env)
        )
        conn.commit()
        conn.close()
        return {"status": "success", "message": "Task queued locally in Termux."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 2. オンライン復帰時にGCPバックエンドへ一斉同期するエンドポイント
@app.post("/queue/sync")
def sync_with_gcp():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, project_name, source_content, target_env FROM deploy_queue WHERE status = 'pending'")
    tasks = cursor.fetchall()
    
    synced_count = 0
    errors = []

    for task in tasks:
        task_id, proj_name, content, env = task
        payload = {
            "project_name": proj_name,
            "source_content": content,
            "target_env": env
        }
        try:
            response = requests.post(f"{GCP_BACKEND_URL}/api/analyze-and-plan", json=payload, timeout=10)
            if response.status_code == 200:
                cursor.execute("UPDATE deploy_queue SET status = 'synced' WHERE id = ?", (task_id,))
                synced_count += 1
            else:
                errors.append(f"Task {task_id}: Server error {response.status_code}")
        except requests.RequestException as e:
            errors.append(f"Task {task_id}: Connection failed ({str(e)})")
            break  # ネットが繋がらなくなったら同期を中断

    conn.commit()
    conn.close()
    return {
        "status": "completed",
        "synced_count": synced_count,
        "errors": errors
    }
