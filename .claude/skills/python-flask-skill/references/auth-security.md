# 身分驗證與安全 (Authentication & Security)

對應 AGENTS.md 第 10 節。

- 登入與 session：採簽章 session cookie（HttpOnly、Secure、SameSite=Lax；正式環境使用 __Host- 前綴，存活 7 天），內容只含 user_id 與 token_version；不使用 JWT。改密碼、登出所有裝置或停權時使 token_version 加 1，以撤銷既有 session。
- 密碼：以 Argon2id 雜湊，長度限制 10 至 128 字，不強制字元組合。
- 帳號唯一：一組 email 只能對應一個帳號，email 正規化為小寫後唯一；判斷是否重複時，gmail.com 與 googlemail.com 的信箱另去除「.」與「+標籤」後比對（email_canonical 欄位，同樣唯一），寄信仍使用原始地址。Google 登入與密碼登入共用同一帳號（見合併規則）。帳號刪除後，同一信箱可重新註冊。
- 註冊與驗證：以信箱加密碼註冊後為 pending_verification，須驗證信箱才可使用客服對話。信箱驗證與重設密碼信件由環境變數 EMAIL_VERIFICATION_REQUIRED 控制：development、testing、staging 可設為 false，此時註冊後直接為 active、email_verified_at 保持空值，忘記密碼功能一併停用（不得以日誌輸出權杖替代寄信）；production 必須為 true 並已設定寄信服務，否則啟動失敗。註冊時須勾選同意服務條款與隱私權政策，並記錄 terms_version 與 terms_accepted_at。驗證與重設密碼的權杖為 32 位元組隨機值，資料庫只存其 SHA-256 雜湊，單次使用；驗證信 24 小時、重設信 1 小時失效。連結的 GET 只顯示確認頁，由 POST 完成動作。
- 防帳號列舉：註冊、忘記密碼與登入失敗一律回應相同的通用訊息，不洩漏帳號是否存在。重設密碼成功後 token_version 加 1，並寄通知信。
- Google 登入：採 Authorization Code 流程搭配 PKCE、state 與 nonce；須驗證 ID token 的 iss、aud、exp、nonce，且要求 email_verified 為真；開發與正式環境使用不同的 OAuth 用戶端。
- 同信箱帳號合併：以 email_verified 為前提。既有本機帳號的信箱已驗證者，直接綁定 Google 身分；未驗證者，清除其未驗證的密碼憑證後綁定並視為已驗證。以信箱註冊時若該信箱已有 Google 帳號，不建立新帳號。
- CSRF：所有非 GET 請求須通過 CSRF 檢查（token 置於頁面 meta，axios 預設帶 X-CSRF-Token 標頭）並驗證 Origin。不得使用需載入外部腳本的驗證碼服務（違反離線前端架構）。
- 登入失敗限制：同一帳號連續失敗 5 次鎖定 15 分鐘，計數存放於資料庫。
- 輸入限制：客服單則訊息上限 1000 字（以 Unicode 字元計），送出前去除控制字元並做 NFC 正規化，空白訊息在 Service 層擋下，不呼叫 Gemini。
- LLM 安全：指令只放在 Gemini 的 system_instruction，使用者文字不得串接進指令；System Prompt 內放不公開的標記字串，輸出若包含則視為驗證失敗；客服 v1 不提供工具呼叫 (function calling)。reply_text 一律以純文字渲染，嚴禁 v-html。同一使用者一日內被內容安全過濾阻擋達 3 次，暫停對話 1 小時並記錄。
- 資料隔離與權限：
  - 所有對話相關的 Repository 方法簽名必須接收 user_id，查詢一律帶 user_id 條件，不提供只憑 ID 取資料的方法給 API 路徑使用；Service 層以 AccessPolicy 集中判斷擁有者與管理員權限，Controller 不自行寫權限判斷。
  - 非擁有者存取一律回 404（非 403），避免洩漏資源是否存在。
  - 角色僅 user 與 admin。管理員存取他人資料須走專用的 Repository 方法，並寫入稽核紀錄 (admin_audit_logs)。
  - 管理員後台為獨立的 /admin 頁面與 /api/admin/*，僅 role 為 admin 者可存取，功能含停權、查看用量與刪除違規內容。首位管理員由部署者以一次性 flask 指令建立。管理員預設只能檢視統計資料、使用者主動提交的意見回饋對話，以及被內容安全過濾標記的對話，不得任意讀取其他對話全文；停權、刪除內容與調整會員等級等高風險操作須重新輸入密碼。
- 用量控制：每位使用者設有速率限制與每日對話輪數上限（依會員等級）；另設全站每日 token 上限，達 80% 告警、達 100% 以繁體中文回覆系統忙碌文案。計數以資料庫帳本 (llm_call_logs) 彙總，fallback 訊息不計入；日界以 Asia/Taipei 計算（資料庫存 UTC）。同一對話同時只處理 1 則訊息。超額與限流的回覆文案一律為繁體中文。
