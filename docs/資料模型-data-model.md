# 資料模型

> 狀態：已實作 ｜ 最後更新：2026-10-30 ｜ 對應 change：member-auth、age-verification-birthdate、cs-chat-core、usage-limits、admin-console ｜ 資料庫版本：`0007_admin_audit_logs` ｜ 規範依據：AGENTS.md 第 4、10、11 節

本文件說明資料表之間的關係、約束與保存規則。欄位與約束以 `app/models/` 與 `migrations/versions/` 為準；這裡只列對設計有意義的欄位。

## 1. 實體關聯圖

```mermaid
erDiagram
    users ||--o{ user_identities : "擁有 CASCADE"
    users ||--o{ auth_tokens : "擁有 CASCADE"
    users ||--o{ chat_conversations : "擁有 CASCADE"
    chat_conversations ||--o{ chat_messages : "包含 CASCADE"
    users |o--o{ llm_call_logs : "產生 SET NULL 匿名化"
    chat_conversations ||..o{ llm_call_logs : "只存編號 無外鍵"
    users ||..o{ deletion_tombstones : "只存內部編號 無外鍵"
    users |o--o{ admin_audit_logs : "管理員操作 SET NULL 紀錄保留"
    users ||..o{ admin_audit_logs : "目標只存對外識別碼 無外鍵"

    users {
        int id PK
        string public_id UK "對外識別碼 UUID"
        string email UK "原始地址"
        string email_canonical UK "小寫與 Gmail 別名正規化"
        string display_name
        string password_hash "Argon2id 可為空 僅 Google 登入"
        datetime email_verified_at
        string role "user 或 admin"
        string member_tier "free plus vip 只影響額度"
        string status "pending_verification active suspended deletion_pending"
        int failed_login_count
        datetime locked_until
        int token_version "加 1 撤銷既有登入"
        string terms_version
        datetime terms_accepted_at
        date birth_date "個資 不對外 不進日誌與模型"
        string age_range
        datetime age_confirmed_at
        string age_policy_version
        bool is_adult_at_confirmation "僅供紀錄 不用於判斷"
        datetime last_login_at
    }
    user_identities {
        int id PK
        int user_id FK
        string provider "google"
        string provider_subject
    }
    auth_tokens {
        int id PK
        int user_id FK
        string purpose "verify_email 或 reset_password"
        string token_hash UK "SHA-256 只存雜湊"
        datetime expires_at
        datetime used_at "單次使用"
    }
    deletion_tombstones {
        int id PK
        int user_id "僅內部編號"
        datetime deleted_at
        string reason
    }
    chat_conversations {
        int id PK
        string public_id UK
        int user_id FK
        string client_id "與 user_id 唯一 建立對話冪等"
        string title
        text summary "累積摘要"
        int summary_upto_seq "已摘要到的序號"
        json state_json "只含列舉值"
        int state_version "狀態與標題共用的版本"
        datetime processing_since "處理標記 60 秒過期"
    }
    chat_messages {
        int id PK
        int conversation_id FK
        string client_msg_id "與對話唯一 冪等"
        int seq "與對話唯一 單調遞增"
        string role "user 或 assistant"
        text content
        string intent "僅助理 供統計"
        json entities_json
        json suggested_actions_json
        float confidence
        string msg_status "ok 或 fallback"
    }
    llm_call_logs {
        int id PK
        int user_id FK "可為空"
        int conversation_id "不設外鍵"
        string kind "chat 或 summary"
        string provider
        string model
        int attempt
        string status
        bool counted "計入額度"
        int prompt_tokens
        int completion_tokens
        int thinking_tokens
        int latency_ms
        string finish_reason
        text error_detail "驗證失敗的模型原文 截斷 2000 字"
    }
    admin_audit_logs {
        int id PK
        int admin_user_id FK "可為空 SET NULL"
        string action "8 個固定動作"
        string target_user_public_id "對外識別碼 無外鍵"
        string target_conversation_public_id "對外識別碼 無外鍵"
        string reason "管理員填寫 至多 200 字"
        json detail_json "只放列舉值與整數"
        datetime created_at
    }
```

## 2. 約束與設計理由

| 項目 | 規則 | 原因 |
|---|---|---|
| 對外識別碼 | 網址與 API 只用 `public_id`（UUID） | 內部整數主鍵不外露；授權另由擁有者條件保證，不靠不可猜測性 |
| 信箱唯一 | `email` 與 `email_canonical` 都唯一；Gmail 去點與去 `+標籤` 後比對 | 一組信箱只能對應一個帳號；Service 層先正規化，雙軌資料庫行為一致 |
| 外鍵動作 | 指向 `users.id` 一律 `CASCADE`，或可為空的 `SET NULL`（`llm_call_logs`） | 刪除帳號時內容真的消失；帳本匿名化後保留 |
| 帳本不設對話外鍵 | `llm_call_logs.conversation_id` 只存編號 | SQL Server 不允許經由多條路徑的串聯動作（錯誤 1785）；刪除對話時由 Repository 清為空值 |
| 冪等 | `(user_id, client_id)`、`(conversation_id, client_msg_id)`、`(conversation_id, seq)` 唯一 | 重送不重複；助理訊息的識別碼由使用者訊息決定性推得，一則使用者訊息至多一則回覆 |
| JSON 欄位 | `state_json`、`entities_json`、`suggested_actions_json` 由 Repository 序列化 | 不依內容查詢（兩種資料庫的 JSON 語法不同）；需要統計的 `intent` 獨立成欄位並建索引 |
| 用量計數不另設計數器 | 每日輪數、全站用量與暫停都從 `llm_call_logs` 彙總，每分鐘訊息數從 `chat_messages` 計數；為此新增 `ix_llm_call_logs_created_at` 與 `ix_chat_messages_created_at` | 沒有「計數器與真相不一致」的問題；索引只加不改，舊版程式不受影響（`0006`） |
| 稽核只有一個外鍵 | `admin_audit_logs.admin_user_id → users.id`（`SET NULL`）；目標使用者與對話只存對外識別碼（UUID，不是個資），不設外鍵 | SQL Server 不允許同一張表有兩個指向同一個父表且帶動作的外鍵（錯誤 1785）；管理員或目標被刪除後，「誰對誰做了什麼」仍然成立，這正是稽核該有的性質 |
| 稽核不可變 | 動作是固定列舉；`detail_json` 只放列舉值與整數；`AdminAuditRepository` 只有 `add` 與 `list`（測試以反射守住） | 紀錄不能被改寫；不記錄 email、訊息、模型原文與密碼 |
| 字串型別 | 一律 `Unicode` 與 `UnicodeText` | SQL Server 對應 NVARCHAR，中文不亂碼 |
| 時間 | 資料庫存 UTC（`UTCDateTime`）；日界與年齡以 Asia/Taipei 計 | 兩種資料庫的時區行為一致 |
| 不設軟刪除 | 使用者刪除對話即永久刪除；帳號刪除靠 `CASCADE` | 不留下內容殘影；`deletion_tombstones` 只存內部編號與日期，備份還原後可重新執行刪除 |
| 生日是個資 | `birth_date`、`age_range` 不進 API 回應、日誌與模型 | AGENTS.md 第 10、11 節；是否成年一律由生日於當下計算 |

## 3. Migration 歷程

```mermaid
flowchart LR
    M1["0001 baseline<br/>只建立版本機制"] --> M2["0002 會員<br/>users、user_identities、auth_tokens"]
    M2 --> M3["0003 年齡確認欄位<br/>age_confirmed_at、age_policy_version"]
    M3 --> M4["0004 生日與年齡區間<br/>birth_date、age_range、deletion_tombstones"]
    M4 --> M5["0005 chat_core<br/>對話、訊息與模型用量帳本"]
    M5 --> M6["0006 usage_indexes<br/>llm_call_logs.created_at、chat_messages.created_at 索引"]
    M6 --> M7["0007 admin_audit_logs<br/>管理員稽核紀錄與兩個索引"]
```

- 一律 expand-only：新增欄位先可為空，先部署相容新舊結構的程式，再回填與加約束。
- migration 是部署流程中獨立的步驟（升級前先備份），應用程式啟動時不會自動執行；正式環境不使用 `create_all()`。
- migration 腳本使用 Alembic op API 與 ORM 型別，不含方言專屬的原生 SQL；`alembic.ini` 與範本必須純 ASCII。

## 4. 保存期限與刪除

```mermaid
flowchart TD
    Msg["對話與訊息"] -->|"使用者主動刪除 或 最後一則訊息後 12 個月（C-4）"| Del1["永久刪除，帳本的對話編號清為空值"]
    Acc["帳號（含生日與年齡區間）"] -->|"連續 12 個月未登入（已實作，管理員不納入）"| Del2["直接刪除 + 寫入 deletion_tombstones"]
    Acc -->|"使用者申請刪除（C-4）"| Pend["deletion_pending：立即停用，7 天寬限期"] --> Del2
    Log["llm_call_logs"] -->|"12 個月（C-4）"| Del3["清除"]
    Err["error_detail（失敗原文）"] -->|"14 天（C-4c）"| Del4["清空，僅管理員可讀（讀取端已由查詢限制 14 天）"]
    Tok["auth_tokens"] -->|"到期後 7 天（C-4c）"| Del5["清除"]
    Aud["admin_audit_logs"] -->|"24 個月（C-4c）"| Del6["清除"]
```

已實作的只有「閒置 12 個月帳號清除」（`flask purge-inactive-accounts`）與使用者主動刪除對話；其餘清除由 C-4c（`privacy-lifecycle`）實作，進度見 [路線圖-roadmap.md](路線圖-roadmap.md)。管理員在後台刪除違規對話與使用者自己刪除相同：先把帳本的對話編號清為空值（它沒有外鍵）再刪對話，訊息靠 `CASCADE` 一起刪，帳本保留。帳號刪除時，C-4c 的流程同樣必須先清除帳本的對話編號，再刪使用者；稽核的管理員欄位會因 `SET NULL` 變空，紀錄保留。

---

**何時更新本文**：新增或修改資料表、欄位、約束、索引、外鍵動作；新增 migration（更新「資料庫版本」與歷程圖）；保存期限或刪除流程改變時。`tests/test_design_docs.py` 會檢查每張資料表都出現在本文，且資料庫版本與最新的 migration 一致。
