# Design

## Context

- AGENTS.md 第 11 節列七項告警與管道（電子郵件或 webhook），且禁止使用會把例外內容送出本系統的外部錯誤追蹤服務。目前一項都沒有；`usage.global_budget_warning` 每個請求都記一筆，註明「由告警層去重」，該層不存在。
- 帳本 `llm_call_logs` 有 `kind`、`status`（`ok`、`validation_failed`、`rate_limited`、`server_error`、`timeout`、`network`、`blocked`、`config_error`）、`latency_ms`、`created_at`（已有索引），足以算模型錯誤率、設定錯誤與延遲。全站用量由 `LlmCallLogRepository.usage_totals` 與 `quota_policy` 的日界給出，上限在 `UsageSettings`。`users.locked_until` 記錄登入鎖定（15 分鐘），沒有登入失敗的歷史表；寄信失敗與 5xx 只有日誌。
- 寄信已有 `SmtpMailSender`（單次 5 秒逾時、收件人與標題換行會被拒絕、失敗時拋出例外）；`MailSettings` 目前只在信箱驗證開啟時才解析。
- 維運工作一律是 `flask` 指令，由平台排程器觸發（C-5 設定），不使用行程內計時器；每個指令輸出與日誌只有筆數或固定列舉。
- 使用者已決定（見 proposal）：只算資料庫量得到的項目，5xx、存活、寄信失敗歸平台；管道只做電子郵件。

動機與範圍見 proposal.md；行為見 specs。

## Goals / Non-Goals

**Goals:**
- 每項告警「進入、升級、提醒、恢復」各通知一次，**不隨檢查頻率重複寄信**。
- 告警內容**絕不含個資**；寄信失敗不被吞掉，也不被當成已通知。
- 判斷邏輯是純函式，門檻與邊界可以逐一測試。

**Non-Goals:**
- webhook、簡訊、通知值班輪替；後台的告警頁面；可由環境變數調整的門檻（見 D5）。
- 登入失敗與寄信失敗的歷史資料表、5xx 比率與服務存活（平台負責，見 D9）。
- 以 IP 為單位的登入失敗統計（資料庫沒有 IP；規範的「單一 IP」留給入口層的限流與平台日誌，C-5）。

## Decisions

**D1　三層：純函式規則、有狀態的服務、薄指令。**
`app/services/alert_rules.py`：每項告警一個純函式，輸入是已查好的數字，輸出是「正常／警告／嚴重＋數值與門檻」或「樣本不足，維持原狀（Hold）」。`app/services/alert_service.py`：查詢、套用規則、依狀態決定動作、寄信、更新狀態。`flask check-alerts [--dry-run]` 只組裝並印結果。規則不碰資料庫與時間，所以邊界（剛好 10%、第 95 百分位）都能用表格式測試。

**D2　`alert_states` 資料表（migration `0009`，只新增）。**
`alert_key`（`Unicode(40)`，唯一）、`state`（`ok`／`firing`）、`severity`（`warning`／`critical`，可為空）、`changed_at`、`last_notified_at`（可為空）、`last_value`（`Float`，可為空）、`updated_at`。至多 5 列（固定的五項告警），不外連任何表、不含個資，所以不需要保存期限清除，也不進抹除流程的登記清單（該架構測試只看指向使用者與對話的欄位）。第一次檢查時才建立列（初始為 `ok`）；兩個行程同時建立時，唯一約束讓其中一個以 SAVEPOINT 失敗後重新讀取（與 `add_user` 同一個做法）。

**D3　狀態轉換與「誰負責寄這一封」。**
決策函式 `decide(previous, evaluation, now)` 回傳動作：`none`、`firing`（正常→告警）、`escalate`（嚴重度升高）、`remind`（持續告警且距 `last_notified_at` 滿 6 小時）、`resolve`（告警→正常）。檢查行程可能重疊（排程重疊或手動執行），所以**先搶佔、再寄信**：以單一條件式 `UPDATE`（`WHERE alert_key = ? AND state = ? AND severity IS ? AND last_notified_at IS ?`，比對的是讀到的舊值）寫入新狀態與 `last_notified_at = now`，`rowcount == 1` 的行程提交後負責寄信，其餘行程什麼都不做。寄信**全部收件人都失敗**時，以條件式 `UPDATE`（`WHERE last_notified_at = :claimed`）還原成舊狀態，下次檢查會再嘗試；至少一位成功就維持已通知（避免已收到的人重複收到信），但指令仍以非零代碼結束。沒有動作時只更新 `last_value` 與 `updated_at`，不影響搶佔的比對欄位。
替代方案「先寄信再更新狀態」被否決：兩個行程會各寄一封。已知取捨：行程在「搶佔之後、寄信之前」被殺掉，這次通知會遺失（至多一次），下一次狀態變化或 6 小時的提醒才會再通知；告警是為了「讓人知道」，遺失一封的代價小於重複洪水，且提醒機制是安全網。

**D4　五項告警的資料來源，全部是唯讀的 ORM 查詢。**

| 代碼 | 查詢 | 規則 |
|---|---|---|
| `usage_budget` | `usage_totals` 今日（台灣日界）與 `UsageSettings` 的兩個上限 | 任一項 ≥ 80% 警告、≥ 100% 嚴重，數值取較高的比例 |
| `llm_error_rate` | 近 5 分鐘 `kind = chat` 的各 `status` 筆數（`LlmCallLogRepository.chat_status_counts`） | （429＋逾時＋伺服器錯誤＋網路）／全部 > 10%；樣本 < 10 → Hold；`blocked` 與 `config_error` 不計入分子，仍在分母 |
| `llm_config_error` | 近 5 分鐘 `status = config_error` 的筆數 | ≥ 1 嚴重 |
| `llm_latency_p95` | 近 15 分鐘 `kind = chat` 且 `status` 為 `ok` 或 `validation_failed` 的延遲 | 最近排名法的 p95 > 10,000 毫秒警告；樣本 < 20 → Hold |
| `login_lockouts` | `UserRepository.count_locked(now)`：`locked_until > now` 的帳號數 | ≥ 5 警告 |

p95 不用資料庫的百分位函式（SQLite 沒有）：先 `COUNT`，再以 `ORDER BY latency_ms LIMIT 1 OFFSET ceil(0.95·n) − 1` 取第 k 個，只讀一列，記憶體與樣本數無關。窗口之間可以重疊（每 5 分鐘檢查、窗口 5 與 15 分鐘），這是刻意的：重疊讓短暫的尖峰不會因為剛好落在兩次檢查之間而漏掉。

**D5　門檻是程式裡的常數，不是環境變數。**
80%／100%、10%、5 分鐘、10 秒、15 分鐘、5 個鎖定帳號、樣本下限與 6 小時的提醒間隔都集中在 `alert_rules.py` 的常數。原因：(a) 這些值是「規範的建議初值」，要由量測資料調整，調整就是一次有測試的程式變更；(b) 環境變數會多出一組要在三個環境維護、又沒有任何防呆的旋鈕。唯一的環境設定是收件人。全站用量的上限本來就是環境變數（`USAGE_DAILY_*`），告警跟著它走。

**D6　設定：`ALERT_EMAIL_TO`。**
逗號分隔、去除空白與重複、至多 5 個、每個必須是單純的電子郵件地址（沿用 `MAIL_FROM` 的格式檢查，拒絕顯示名稱與換行）。設定了它，`_resolve_mail` 就必須通過（信箱驗證關閉時也一樣），否則啟動失敗；staging 與 production 沒有設定也啟動失敗（只列變數名稱）。`SmtpMailSender` 建立的條件從「有 `MailSettings`」不變，只是 `MailSettings` 現在也會在有收件人時存在。測試用的 `production_like_env` 補上假的收件人，其餘既有測試不受影響。

**D7　信件：`app/services/alert_messages.py`，常數加 `str.format`，不使用 Jinja。**
與 `mail_messages.py` 相同的規則（顯示的全是系統自己的文字，沒有使用者輸入，但仍不走範本引擎）。標題：`【智能客服】告警：<名稱>（<進入|升級|提醒|已恢復>）`。內文：名稱、嚴重度、目前數值與門檻、開始時間（台灣時間，沿用 `taiwan_time_text`）、一句處理建議（例如「請檢查模型金鑰是否被撤銷」）。**沒有任何資料庫內容**，只有數字。每位收件人各寄一封（`MailSender.send` 一次一個收件人，標題與收件人的換行檢查維持原樣）。

**D8　錯誤隔離。**
每項告警各自 `try`：查詢或規則丟出例外時回復該項的交易、記錄 `alert.check_failed`（只有告警代碼）、繼續其餘項目，最後以非零代碼結束並在輸出標明。寄信例外不外洩訊息（沿用 `MAIL_SEND_FAILED` 的做法：只記事件）。沒有設定收件人（development 與 testing）時只評估並印出結果，**不改變狀態**，這樣之後設定了收件人，已成立的告警才會被通知，而不是因為狀態早已更新而沉默。

**D9　平台負責的三項，與 `usage.global_budget_warning`。**
營運文件新增「告警條件一覽」：5xx 比率（5 分鐘內 > 5%，Cloud Monitoring 對請求日誌或 Cloud Run 指標）、服務存活（`/healthz` 與 `/readyz` 的外部探測連續失敗）、寄信失敗（日誌事件 `mail.send_failed` 連續出現）。這些是 C-5 設定平台時的清單，**不是本 change 的程式**。`usage.global_budget_warning` 日誌事件維持原樣（每個請求一筆，供除錯與平台日誌分析），但**不再作為通知來源**：通知由 `usage_budget` 告警的狀態去重負責。

**D10　日誌事件。**
`app/observability/alert_events.py`（與 `lifecycle_events.py` 同一個模式）：`alert.firing`、`alert.resolved`、`alert.delivery_failed`、`alert.check_failed`；欄位是事件名稱、`reason`（告警代碼，固定列舉）、`severity`（固定列舉，新增白名單欄位）與整數 `count`（沿用）。傳入列舉以外的值立刻出錯。提醒與升級沿用 `alert.firing`（`severity` 區分）。預覽不記事件。

## Risks / Trade-offs

- **檢查本身依賴資料庫與寄信**：資料庫不通時這個指令也跑不動，所以「服務存活」必須由平台外部探測（D9）；寄信服務掛掉時告警寄不出去，所以「寄信失敗」也要由平台日誌告警，而且指令會以非零代碼結束，排程器應把它接到平台自己的告警。→ 營運文件明列這個相依。
- **登入失敗激增用鎖定帳號數代理**：慢速、分散、每帳號少於 5 次的嘗試不會被看見；規範的「單一 IP」沒有資料。→ 已知限制，入口層限流與平台日誌（C-5）補足；若日後要精確，再加歷史資料表。
- **至多一次的通知**：搶佔之後行程被殺會遺失這一封（D3）。→ 6 小時提醒是安全網；`--dry-run` 與狀態表可供人工核對。
- **門檻是初值**：10 秒、10%、5 個帳號都未經實際流量驗證。→ 常數集中、測試表格化，調整是小變更；上線後第一週檢視告警頻率。
- **雙軌差異**：`OFFSET` 與 `COUNT` 都是 ORM 的標準寫法；SQL Server 的 `OFFSET` 需要 `ORDER BY`（本來就有）。→ 在真實 SQL Server 上跑完整測試。
- **信件被當垃圾信**：沿用既有 SMTP 與寄件者。→ C-5 的寄信服務設定（SPF、DKIM）負責。

## Migration Plan

1. 備份資料庫（既有流程），`alembic upgrade head` 到 `0009`（只新增一張表，舊版程式不受影響）。
2. 部署新版程式；在 staging 與 production 設定 `ALERT_EMAIL_TO`（缺少會啟動失敗，所以要先設再部署）。
3. 手動執行 `flask check-alerts --dry-run` 檢視各項目前的數值，再執行一次正式的；確認收到信（可暫時把某個常數對應的條件人為製造，例如在 staging 把用量上限調低）。
4. 排程器每 5 分鐘觸發（C-5）。
5. 回復：程式可以回到舊版（舊版不認識新資料表也不受影響，只是沒有告警）；`downgrade` 只刪除 `alert_states`。

## Open Questions

- 各項門檻在真實流量下是否合適，待上線後依告警頻率調整（不影響規格的結構）。
- 是否在後台總覽顯示目前告警中的項目：目前不做，需要時另立 change。
