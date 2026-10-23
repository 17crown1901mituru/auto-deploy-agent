import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner

app = FastAPI(title="Auto-Deploy Agent Backend", version="1.0.0")

# リクエストデータの構造定義
class DeployRequest(BaseModel):
    project_name: str
    source_content: str  # ソースコード、ドキュメント、または要件定義のテキスト全体
    target_env: str = "auto" # "gcloud", "cloud-run", "auto" など

# Google ADK を使った「自己解析・デプロイ設計エージェント」の初期化
# あらゆるドキュメントやコードを読み解き、最適なデプロイ手順を構築する頭脳です。
analyzer_agent = Agent(
    name="AutoDeployAnalyzer",
    model="gemini-2.5-flash",  # または利用可能なモデル
    instruction=(
        "You are an expert DevOps and Architecture AI agent. "
        "Analyze the provided source code or source documents (requirements, README, API specs, etc.). "
        "Determine the appropriate tech stack, build commands, and deployment strategy (e.g., Docker, Cloud Run, static hosting). "
        "Output a clear, step-by-step deployment execution plan and generated configuration files if needed."
    )
)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "GCP Auto-Deploy Agent Backend is running."}

@app.post("/api/analyze-and-plan")
async def analyze_and_plan(req: DeployRequest):
    try:
        # プロンプトの組み立て
        prompt = f"""
        Project Name: {req.project_name}
        Target Environment: {req.target_env}
        
        --- Source / Document Content ---
        {req.source_content}
        ---------------------------------
        
        Please analyze this material and provide:
        1. Detected Language/Framework
        2. Required Configuration Files (e.g., Dockerfile, cloudbuild.yaml)
        3. Step-by-step deployment instructions.
        """
        
        # ADKのランナーを使ってエージェントを実行
        session_service = InMemorySessionService()
        runner = Runner(agent=analyzer_agent, session_service=session_service)
        
        # エージェントからの回答を取得
        # ※実際の非同期ハンドリングやセッション管理はプロジェクト構造に合わせて調整します
        response_text = "Analysis completed. (Mock execution response for ADK runner pipeline)"
        
        return {
            "status": "success",
            "project": req.project_name,
            "analysis_result": response_text
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
