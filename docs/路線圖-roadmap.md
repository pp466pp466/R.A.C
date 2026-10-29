# 路線圖與待決定事項

> 狀態：持續更新 ｜ 最後更新：2026-10-29 ｜ 對應 change：全部（每次封存後更新） ｜ 規劃依據：[report.md](report.md) 第 5.2 節、[RPG.md](RPG.md)

本文件說明各個 change 的順序、相依與目前狀態，以及尚未決定的事項。細節與理由在 report.md 與 RPG.md；這裡只維護「現在在哪裡、下一步是什麼」。

## 1. Change 狀態與相依

```mermaid
flowchart LR
    C1["C-1 platform-foundation<br/>骨架、設定、資料庫、日誌、容器"]:::done
    C2["C-2 member-auth<br/>註冊、登入、Google、權限"]:::done
    AG["age-gate<br/>年齡閘門（共用層）"]:::done
    AB["age-verification-birthdate<br/>生日與年齡區間、閒置帳號清除"]:::done
    C3["C-3 cs-chat-core<br/>客服對話核心與模型呼叫層"]:::done
    C4["C-4 usage-safety-ops<br/>額度、限流、保留期限、管理員後台、匯出與刪除帳號"]:::plan
    C5["C-5 production-launch<br/>部署、入口層、備份演練、營運手冊"]:::plan
    C6["C-6 order-integration<br/>訂單查詢與退款申請"]:::plan
    RC1["RC-1 rpg-data-and-script<br/>劇本與存檔資料"]:::plan
    RC2["RC-2 rpg-turn-engine<br/>回合引擎、SSE、世界狀態"]:::plan
    RC3["RC-3 rpg-game-ui<br/>/game 頁面"]:::plan
    RC4["RC-4 rpg-ops-loadtest<br/>串流壓測與監控"]:::plan
    RC5["RC-5 rpg-persona-library<br/>性格原型庫"]:::plan
    RC6["RC-6 external-data-gateway<br/>外部資料閘道（共用能力）"]:::plan

    C1 --> C2 --> AG --> AB --> C3
    C3 --> C4 --> C5
    C3 --> C6
    C4 --> RC1 --> RC2 --> RC3 --> RC4
    RC2 --> RC5
    C4 --> RC6

    classDef done fill:#1f3d2b,stroke:#4c9a6a,color:#e2e4e9
    classDef plan fill:#1c1e23,stroke:#2a2d34,color:#8a8f99
```

圖例：深綠＝已封存；灰＝規劃中。

| change | 狀態 | 日期／提交 | 說明 |
|---|---|---|---|
| platform-foundation（C-1） | 已封存 | 2026-10-27 | 專案骨架、設定與機密、資料庫雙軌、離線資產與共用元件、日誌與健康檢查、容器 |
| member-auth（C-2） | 已封存 | 2026-10-28 | 註冊、信箱驗證、登入、Google 登入、CSRF、`AccessPolicy` |
| age-gate | 已封存 | 2026-10-28 | 年齡閘門：未確認年齡不得使用任何會呼叫模型的功能 |
| age-verification-birthdate | 已封存 | 2026-10-29 | 以生日加年齡區間取代單一勾選；13 至 17 歲固定安全模式；閒置 12 個月帳號清除 |
| cs-chat-core（C-3） | 已封存 | 2026-10-29（實作 `3cd6284`；封存時同步 6 個新能力、修改 4 個既有能力） | 客服對話、模型呼叫層、補償式摘要、`/cs` 頁面；新增主規格 `cs-conversations`、`cs-conversation-memory`、`cs-nlp-contract`、`cs-chat-ui`、`llm-provider`、`llm-structured-calls`，並修改 `access-control`、`service-observability`、`app-configuration`、`application-layering` |
| usage-safety-ops（C-4） | 規劃中（下一個） | — | **C-4 之前不得對公開網路開放客服**：目前沒有額度、限流、全站預算與安全過濾累計暫停 |
| production-launch（C-5） | 規劃中 | — | 入口層、HTTPS、備份與還原演練、營運手冊；上線前要重新確認供應商條款與年齡規則 |
| order-integration（C-6） | 規劃中 | — | 新增 `order_query`、`refund_request` 兩個意圖與對應實體 |
| RPG 系列（RC-1 至 RC-6） | 規劃中 | — | 見 [RPG.md](RPG.md)；沿用客服的模型呼叫層、`parse_llm_json`、`LlmService` 與帳本，另加串流 |

## 2. 待決定與待辦

| 事項 | 狀態 | 影響的文件 |
|---|---|---|
| 客服提示詞的小偏差 ③：建議動作偶爾不貼題（意見回饋後出現「了解會員額度」；已列出全部等級又給三個「查看 X 額度」） | 偏差 ①（說了「改善」）與 ②（重複範圍說明）已微調並以 5 則真實呼叫快速驗證，兩者改善；③ 未處理，是否調整待使用者決定，調整後須再同意一次真實呼叫 | [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md)、README |
| 客服回覆的繁體中文用語與語氣 | 待使用者目視確認 | — |
| **RPG 的開場流程**：第一輪前先顯示背景故事，再顯示符合故事的隨機開場文字；劇情的 350 至 1000 字只是期望值（RPG.md T-13） | 已記錄需求，未立案；立案前以 `/openspec-explore` 設計 | [RPG.md](RPG.md)、[前端結構-frontend.md](前端結構-frontend.md) |
| 正式環境改用 OpenAI API（使用者計畫） | 未開始；屆時新增轉接器與環境變數 | [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) 第 9 節、[帳號與安全-auth-security.md](帳號與安全-auth-security.md)（年齡規則的前提）、AGENTS.md 第 5、9、11 節、隱私權政策 |
| 過渡期的模型：本機與驗收環境用 `gemini-3.5-flash-lite` | 已決定（`gemini-3.8-flash` 免費層每日約 20 次） | [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) |
| 逾時（20 / 30 / 35 秒）要不要調整 | 等正式模型確定後以量測資料決定 | [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) |
| 登入後的預設頁面（現為 `/account`，可改為 `/cs`，單一常數 `app/security/redirects.py`） | 待決定 | [帳號與安全-auth-security.md](帳號與安全-auth-security.md) |
| 提示詞中的會員額度是 AGENTS.md 第 7 節的初始值（`cs_prompt._QUOTA_FACTS`） | C-4 讓額度可設定時須改取自同一份設定 | [客服對話-chat-service.md](客服對話-chat-service.md)、[模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) |
| 13 至 17 歲可使用 AI 功能與 Gemini 條款不符 | 僅限個人測試；對外開放前須以實際供應商條款重新確認（RPG.md R-15.4） | [帳號與安全-auth-security.md](帳號與安全-auth-security.md) |

## 3. 每次封存後要做的事

1. 在上方狀態表把該 change 標為已封存並填入日期。
2. 依 [README.md](README.md) 的對照表更新受影響的設計文件（圖、表、「最後更新」與「對應 change」）。
3. 執行 `tests/test_design_docs.py`：它會檢查資料表、路由、migration 版本、環境變數、分層與前端檔案是否都出現在對應的文件。

---

**何時更新本文**：每次 propose、apply 完成與 archive 之後；待決定事項有結論時。`tests/test_design_docs.py` 會檢查已封存與進行中的 change 名稱都出現在本文。
