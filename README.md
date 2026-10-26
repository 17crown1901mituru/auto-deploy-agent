# Auto Deploy Agent

スマホ1台で「資料やコードを渡す → 解析 → 成果物の仮作成と仮実行 → 承認 → GCPに本番デプロイ」までを進めるツールです。
Googleアカウントでログインし、デプロイした成果物は **既定でそのアカウントだけが開ける**（限定公開）状態になります。

```
APK（画面）──localhost──▶ Termuxブリッジ ──HTTPS(ログインのセッション)──▶ GCPバックエンド（Cloud Run）
  Googleログイン・承認の操作      成果物の仮作成・仮実行                     解析エージェント(ADK)
  結果の表示                      ログの保存（~/deploy-logs）                gcloud run deploy / 権限付与
```

## 流れ

1. **初回起動**: APKが初期設定の画面を出す
   - ① バックエンドのURLを入れて **Googleでログイン**（外部ブラウザで行う。ログインできるのは `ALLOWED_EMAILS` に入れたアカウントだけ）
   - ② **Termuxに貼る1行コマンド**をコピーして実行（パッケージ導入、`termux.properties` の `allow-external-apps` 設定、合言葉の保存、ブリッジ起動を自動で行う）
   - ③ 接続を確認して、ログイン情報をブリッジに渡す
2. 資料・説明・ファイルを渡すと、エージェントがREADME/docsを先に読み、コードと照らし合わせる。デプロイ先などが足りなければ **質問** が出る
3. ブリッジがDockerfileなどを作り、Dockerなしで **仮実行**（インストール→起動→ヘルスチェック→停止）
4. 成果物・仮実行の結果・ログを見て、**公開範囲**（自分だけ［既定］／誰でも）を選び、承認・修正依頼・却下のどれかを選ぶ
5. 承認するとCloud Runにデプロイ。ビルドログはTermuxのログファイルに追記される
6. **権限が足りないとき**: エージェントが「何の権限が必要か」を提案し、APKに承認カードが出る。**Googleの同意画面で承認した分だけ**が、あなた自身の権限で1回実行され、終わるとデプロイが自動で再開する

ログは `~/deploy-logs/<プロジェクト>/<日時>_<ジョブID>.log` に残ります（GCP側のログも `[gcp]` として入ります）。

## 限定公開のしくみ

既定では、Cloud Runの **IAP（Identity-Aware Proxy）** を有効にしてデプロイし、ログインしたアカウントに `roles/iap.httpsResourceAccessor` を付けます。
URLを開くとGoogleのログイン画面が出て、そのアカウントでログインしたときだけ表示されます。承認画面で「誰でも（公開）」を選んだときだけ、認証なしで公開されます。

- IAPには制限があります（プロジェクトの組織の有無によって初回設定が変わる、など）。個人のGmailなどで組織が無い場合、**初回だけCloud Runのコンソールで設定が必要**なことがあります。その場合はAPKに案内が出ます
- 限定公開のサービスは、ブラウザでログインして開く使い方向けです（他のプログラムから呼ぶには、IDトークンが必要です）

## セットアップ

### 1. Google Cloud（最初に1回）

**OAuthクライアントを作る**（コンソールでの手作業。自動化できません）
1. 「APIとサービス」→「OAuth同意画面」で、外部・テスト中のまま作り、**テストユーザーに自分のGmailを追加**
2. 「認証情報」→「OAuthクライアントID」→ 種類「**ウェブアプリケーション**」を作る
3. 「承認済みのリダイレクトURI」は、手順2でバックエンドのURLが決まってから `https://<バックエンドのURL>/auth/callback` を追加

**バックエンドをデプロイする**
```bash
cd gcp-backend
gcloud run deploy auto-deploy-backend --source . --region asia-northeast1 \
  --allow-unauthenticated --timeout 900 --no-cpu-throttling --max-instances 1 \
  --set-env-vars "SESSION_SECRET=$(openssl rand -base64 32),ALLOWED_EMAILS=あなたのGmail,GOOGLE_CLIENT_ID=<クライアントID>,GOOGLE_CLIENT_SECRET=<シークレット>,GCP_PROJECT=<プロジェクトID>,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=<プロジェクトID>,GOOGLE_CLOUD_LOCATION=us-central1"

# 表示されたURLを、リダイレクトURIに登録したうえで、PUBLIC_BASE_URL を設定する
gcloud run services update auto-deploy-backend --region asia-northeast1 \
  --update-env-vars "PUBLIC_BASE_URL=https://<バックエンドのURL>"
```
- `--allow-unauthenticated` はバックエンド自身の設定です（`/auth/*` を開くため）。`/api/*` は、ログインのセッションが無いと拒否されます
- `ALLOWED_EMAILS` が空だと、誰もログインできません（未設定のまま公開されないため）。複数ならカンマ区切り
- `--max-instances 1` は、ログインや承認の受け渡し、デプロイの排他をメモリ上で行っているためです。`--no-cpu-throttling` は、通信が切れてもデプロイを最後まで走らせるためです
- 解析にVertex AIのGeminiを使う場合、バックエンドのサービスアカウントに `roles/aiplatform.user` が必要です（`GOOGLE_API_KEY` を使うなら不要）
- モデルは `MODEL_NAME`（既定 `gemini-2.5-flash`）で変えられます
- `gcloud` で見つけられない場合は、`GCP_PROJECT_NUMBER`（プロジェクト番号）も環境変数に入れてください（Cloud Run上では通常は自動で取得します）

**デプロイに必要な権限は、最初は手作業で付けなくてかまいません。** 最初のデプロイで不足が出ると、エージェントが必要な権限を提案し、APKで承認すると付与されます（下記）。

### 2. APK（GitHub Actionsでビルド）

1. このリポジトリをGitHubにpushする（`main` ブランチ）
2. Actions の **Build Android APK** が走る（手動なら "Run workflow"）
3. 完了後、`auto-deploy-agent-debug-apk` のArtifactからAPKをダウンロードして、スマホにインストールする（提供元不明のアプリを許可）
4. 起動すると初期設定の画面になる。バックエンドのURLを入れて、Googleでログイン → Termuxのコマンドをコピーして実行 → 自動で進む

ビルドの中身: Capacitorで `frontend/` をWebViewアプリに包む → `scripts/prepare_android.py` でTermux起動プラグインとManifestの設定を足す → `gradlew assembleDebug`。署名はdebug鍵です（自分用の利用向け）。リポジトリのURLはビルド時に画面へ埋め込まれます（非公開リポジトリの場合、Termuxの `git clone` に認証が要ります）。

### 3. Termux

APKの初期設定が出すコマンドを貼り付けるだけです。手で行う場合:
```bash
pkg install -y git
git clone <このリポジトリ> ~/auto-deploy-agent
BRIDGE_TOKEN=<アプリに表示される合言葉> bash ~/auto-deploy-agent/termux-bridge/setup.sh
```
`setup.sh` が行うこと: ①パッケージ導入（`pydantic` のビルドで失敗する場合は `pkg install python-pydantic` のあと再実行）②`~/.termux/termux.properties` に `allow-external-apps = true` を設定（他の設定は残す）③合言葉を `~/.auto-deploy/bridge_token` に保存（権限600）④ブリッジの再起動。何度実行しても同じ結果になります。
**注意:** 合言葉を含むコマンドはクリップボードを通ります。合言葉はスマホ内のブリッジへの接続にしか使えません。

## 権限付与の安全のしくみ

- エージェントとバックエンドは「何が必要か」を、**種類つきの提案**として出すだけです。自由なロール名は受け付けません。種類は次の6つだけです: APIの有効化（許可リスト内）、デプロイ用アカウントへの基本ロール、Cloud Buildのアカウントへのビルド用ロール、IAPからCloud Runを呼ぶ権限、指定アカウントへのIAPアクセス、IAPのサービスエージェント作成
- 提案の中身（対象アカウント、ロール、サービス名）は、**サーバー側で作り直します**。アプリや資料に書かれた値は信用しません
- 実行は、あなたがGoogleの同意画面で承認したときに、**あなた自身のトークンで1回だけ**行います。バックエンドのサービスアカウントに、IAMを書き換える強い権限は持たせません。gcloud の `--access-token-file`（公式リファレンスでは「有効なアカウントの認証情報は無視され、このトークンだけで認証する」フラグ）で渡すので、バックエンド自身のサービスアカウントの権限では実行されません。トークンは一時ファイルに置き、実行後に削除します
- 承認したアカウントが、ログイン中のアカウントと違うときは実行されません
- 承認するまでは、何も変更されません

デプロイ用のロールは、現在のGoogleのドキュメントに合わせて `roles/run.admin`、`roles/serviceusage.serviceUsageConsumer`、`roles/iam.serviceAccountUser`（デプロイ用アカウント）と `roles/run.builder`（Cloud Buildのアカウント）です。

## その他の安全のためにしていること

- ブリッジは `127.0.0.1` だけで待ち受け、合言葉（`X-Bridge-Token`）が無いと操作できない
- バックエンドの `/api/*` は、署名つきのセッションと許可リスト（`ALLOWED_EMAILS`）が無いと拒否する。セッションの有効期限は30日
- 仮実行で動かせるコマンドは許可リスト制（python, pip, node, npm, npx, uvicorn, gunicorn）。シェルを通さず、`&&` や `|` は拒否。秘密（セッションや合言葉）は仮実行のプロセスに渡さない
- Python製のアプリは、仮実行用に `.venv` を作ってその中にインストールする
- 資料に書かれた指示は「データ」として扱うようエージェントに伝えている（完全な防御ではないので、**承認前に成果物を必ず確認**してください）
- 本番デプロイは、承認ボタン＋確認ダイアログを通らないと実行されない

## できないこと・注意

- 渡せるのはテキストファイルだけ（画像やzipは不可）。1ファイル200KB、合計1MBまで
- Termuxでは通常Dockerが動かないため、仮実行は Python/Node のコマンドを直接動かして確認します。Dockerfileの検証は、本番のCloud Buildで行われます
- デプロイ先は Cloud Run のみ
- ブリッジが停止（Androidの省電力など）すると、処理中だったジョブは失敗扱いになります。「やり直す」か「結果を確認」で再開できます
- バックエンドが再起動すると、承認待ちの付与は失われます（アプリに表示されるので、もう一度承認してください）
- Googleの同意画面は、アプリが未確認のため警告が出ます（自分用のテスト利用では、そのまま進めます）

## ディレクトリ

| パス | 役割 |
|---|---|
| `frontend/index.html` | APKの画面（WebView） |
| `android-overlay/`, `scripts/prepare_android.py` | Termux起動プラグインとManifest設定（CIで適用） |
| `termux-bridge/` | Termux上のAPI（`core.py` が本体、`server.py` がHTTP入口、`start.sh`、`setup.sh`） |
| `gcp-backend/` | Cloud Run上のAPI（`main.py`、`core.py`、`auth.py`、`perms.py`、`Dockerfile`） |
| `.github/workflows/build-apk.yml` | APKのビルド |
| `tests/` | テスト（下記） |

## テスト

```bash
python3 -m unittest discover -s tests -p "test_gcp_core.py"      # GCP側（ログイン、権限、デプロイ）
python3 -m unittest discover -s tests -p "test_termux_core.py"   # Termux側（偽のGCPと結合。要 flask）
python3 -m unittest discover -s tests -p "test_setup_sh.py"      # setup.sh
python3 tests/ui_smoke.py                                        # 画面（要 flask, playwright）
```

## ジョブの状態

`queued`（オフライン待機）→ `analyzing` →（`needs_info`）→ `drafting` → `trial_running` → `awaiting_approval` → `deploying` →（`needs_permission`）→ `deployed`。途中で `failed` / `rejected` になることがあります。
