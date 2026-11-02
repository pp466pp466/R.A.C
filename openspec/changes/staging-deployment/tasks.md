# Tasks

> 本 change 不碰任何雲端資源，不產生費用。第 6 組的人工驗收（Google 登入、收信）需要使用者在場；不在場就標為「未驗證」並記下狀態，不得自行繼續或以預期結果代替。雲端實跑在 `cloud-staging-run`。

## 1. 前提確認（本機）

- [x] 1.1 向使用者確認並記錄本機預演的前提：ngrok（已安裝、已登入、最好有固定網域以免 OAuth 重新導向 URI 一直要改）、Brevo（SMTP 登入名稱、已驗證的寄件者；SMTP 金鑰已在 `.env`）、預備環境專用的 Google OAuth 用戶端（網頁應用程式型，**不重用**開發用的與 Firebase 自動建立的；同意畫面為「測試中」並把使用者的 Gmail 加為測試使用者，範圍只有 `openid email profile`；授權重新導向 URI 為 `<ngrok 網址>/auth/google/callback`）、WSL 的 Docker 能否啟動（先試，結果只決定第 6.5 項是否進行）。驗證：README 新增「預備環境前提」小節，逐項列出狀態（已提供／待提供／未預演，不含任何機密）；`.env.staging` 與 `staging.vars` 已存在且被版本控制忽略（`git check-ignore` 驗證）

## 2. 開發用通道（ngrok）

- [x] 2.1 `app/config.py` 新增 `TRUST_PROXY_HEADERS`（只在 development 生效，其餘環境設定即啟動失敗並只指出變數名稱，值只接受 true／false），`app/__init__.py` 在 development 且為真時掛 ProxyFix；`init_security_headers` 的 `hsts` 改取 `is_production_like`（通道不讓開發環境送 HSTS）；`.env.example` 補上說明。驗證：新增 `tests/test_development_tunnel.py`——帶 `X-Forwarded-Proto: https` 與 https 的 Origin 的 POST 通過 CSRF；不設定時維持被拒絕；testing、staging、production 設定了即啟動失敗且訊息不含值；不合法的值被拒絕；development 經通道仍沒有 `Strict-Transport-Security`；`BASE_URL` 為通道網址時 Google 回呼網址由它組成
- [x] 2.2 營運文件新增「開發用通道（ngrok）」：步驟（通道指令、`BASE_URL`、開發用 OAuth 用戶端的授權重新導向 URI、`TRUST_PROXY_HEADERS=true`）、暴露風險（開發工具頁與測試帳號對網際網路可見）與存取控制建議、網址變動時要同步 OAuth 設定、開發用 OAuth 用戶端不用於預備環境；README 環境變數表補上新變數。驗證：`pytest tests/test_design_docs.py tests/test_readme.py` 通過，且 `tests/test_development_tunnel.py` 斷言文件載明暴露風險與存取控制

## 3. 設定

- [x] 3.1 `app/config.py` 新增 `GEMINI_BASE_URL`（https；development 與 testing 另允許回送位址的 http；不得含帳密、查詢與片段；production 設定即啟動失敗；訊息只指出變數名稱），`create_app` 把它傳給 `GeminiProvider`；`.env.example` 補上。驗證：擴充 `tests/test_config.py` 與新增轉接器測試——合法網址、production 拒絕、帳密與查詢被拒、錯誤訊息不含網址、未設定用預設端點、轉接器以假傳輸確實呼叫該網址
- [x] 3.2 `WEB_CONCURRENCY` 與 `GUNICORN_THREADS` 納入 `.env.example`，並在 `scripts/launch_plan.py` 歸為一般設定（兩個值必須是正整數的說明寫在 `.env.example`）。驗證：擴充 `tests/test_launch_plan.py` 的歸類測試通過（`.env.example` 的每個變數恰好屬於一類）；`gunicorn.conf.py` 的預設值不變

## 4. 假模型伺服器與階梯式加壓

- [x] 4.1 新增 `loadtest/fake_llm/`（Flask 應用、`Dockerfile`、最小的 `requirements.txt`）：Gemini 相容的 `generateContent`、存活檢查、延遲與錯誤率環境變數（規則同本機假供應商）、只接受測試金鑰、日誌只有彙總。驗證：新增 `tests/test_fake_llm_server.py`——專案的 `GeminiProvider` 以假伺服器網址（本機執行緒伺服器）呼叫成功並解析、摘要與對話兩種回覆、金鑰不符回 403、約 10% 的伺服器錯誤設定生效、不合法的設定啟動失敗且不回顯值、日誌掃描沒有金鑰與本文、主映像的複製與忽略清單不含它、相依清單不含資料庫驅動
- [x] 4.2 新增 `loadtest/shapes.py`（階梯式加壓：逐步增加虛擬使用者數、每階段持續固定時間），`evaluate.py` 的輸出補上「環境」欄位以便每一輪留存。驗證：新增 `tests/test_loadtest_shapes.py`——各階段的使用者數與持續時間、結束條件、輸出只有彙總；擴充 `tests/test_loadtest_evaluate.py`
- [x] 4.3 營運文件「負載測試」補上：階梯式情境、每一輪的記錄格式（環境、伺服器設定、負載參數、判定、是否可作為容量依據）、本機結果不是容量依據的規則。驗證：`pytest tests/test_design_docs.py tests/test_launch_docs.py` 通過

## 5. 冒煙驗收腳本

- [x] 5.1 新增 `scripts/staging_smoke.py`：依規格檢查 `/healthz`／`/readyz`、五個安全標頭且無 CSP、`__Host-` cookie 的屬性、無權杖的 POST 被拒絕、註冊／登入／開新對話（保留網域信箱）、一般會員存取管理員 API 為 404（與不存在的端點相同）、未登入為 401；預設不呼叫模型，同意參數才送一則訊息並檢查回覆不是降級文案；位址守門（https、拒絕 production）；輸出只有項目與結果。驗證：新增 `tests/test_staging_smoke.py`——以 staging 設定的測試應用經 `https://` 驅動全部通過、缺 HSTS／缺 `__Host-`／CSRF 沒擋住各別失敗、預設不送訊息、輸出掃描沒有 cookie／權杖／密碼／回應本文、拒絕 http 與主機名稱含 production
- [x] 5.2 營運文件新增「預備環境驗收」小節（冒煙腳本用法、檢查清單、驗收紀錄格式與兩欄規則）。驗證：`pytest tests/test_design_docs.py tests/test_launch_docs.py` 通過

## 6. 本機預演（使用者在場；不碰雲端）

- [x] 6.1 以 staging 設定在本機啟動並經 ngrok 取得 https 網址：從 `.env.staging` 與 `staging.vars` 載入（值不印出）、SQLite 檔案資料庫、`BASE_URL` 為通道網址、建立預備環境的首位管理員（互動輸入密碼，不經對話）。驗證：缺少必要機密時確實啟動失敗（先故意缺一個再補回）；`/healthz` 與 `/readyz` 經通道回 200；首頁回應帶 HSTS 與 `__Host-` cookie；結果記入 README
- [x] 6.2 對通道網址執行 `staging_smoke.py`。驗證：全部通過（模型呼叫標為略過）；失敗的項目已修正並記錄原因
- [x] 6.3 Google 登入人工驗收：請使用者以真實 Google 帳號在通道網址登入——首次（勾選條款並提供年齡確認後建立沒有密碼的帳號）、第二次（辨識為同一個帳號、不重複建立；登入頁目前仍要求重填條款與生日，已記為與原預期不符）、同信箱既有帳號的合併。驗證：三項結果記入 README；使用者不在場則標為未驗證，不以測試代替
- [x] 6.4 營運告警信真實寄送：在預演的資料庫製造一項告警條件（5 個被鎖定的 `.invalid` 測試帳號），執行 `flask check-alerts`，請使用者確認收到信（含是否進垃圾郵件匣）。驗證：收到的信只有告警名稱、數值與門檻；紀錄寄件者的送達觀察；沒收到則標為未驗證並寫明檢查過的地方與推測原因（可改用其他寄件者或 SMTP 再試一次）；測試帳號與告警狀態已清除
- [x] 6.5 （條件式）本機容器預演：WSL 的 Docker 能啟動時，建置主映像、以 gunicorn gthread 啟動、指向假模型伺服器（`GEMINI_BASE_URL`）、執行階梯式加壓並以 `evaluate.py` 判定。驗證：至少兩輪（基準與調整一個變數）的記錄含環境、`WEB_CONCURRENCY`／`GUNICORN_THREADS`、負載參數與判定，且每一輪標明「不是容量依據」；Docker 起不來則標為未預演並寫明原因
- [x] 6.6 收尾：關閉通道、刪除預演的 SQLite 檔案與測試資料、確認 `.env.staging` 與 `staging.vars` 仍未被追蹤。驗證：`git status` 沒有任何預演產物；README 的預演紀錄分成「本機預演已驗證」與「必須在雲端驗證（尚未做）」兩欄

## 7. 文件、關卡與整體驗證

- [x] 7.1 更新路線圖與上線前關卡：C-5b 為本 change（本機預演）、C-5c 為 `cloud-staging-run`、C-5d 為 `production-launch`；關卡表依實際依據更新狀態（本機預演不使「託管資料庫成本」與「負載測試」變為已通過，其餘維持未開始）。驗證：`pytest tests/test_launch_docs.py tests/test_design_docs.py tests/test_readme.py` 通過，關卡表每個「已通過」都有日期與紀錄位置
- [x] 7.2 全量驗證：`pytest` 全量（含 SQL Server，不略過）、`node --test`、`openspec validate staging-deployment --strict`。驗證：三者通過，數字填入 README 驗收紀錄與路線圖

## Workflow follow-up

- 檢視後提交，再以 `/openspec-archive-change staging-deployment` 同步主規格並封存。
- `cloud-staging-run` 需要一個可計費的 Google Cloud 專案（含已安裝並登入的 `gcloud`、資料庫版本與規格的費用同意）才能開始。
