"""AGENTS.md 是唯一權威來源；skill（.claude/skills/python-flask-skill）必須與它逐字一致。

這裡守住「實作回饋後補進規範」的幾條規則：它們必須同時存在於 AGENTS.md 的正確章節與 skill 的對應 reference，
避免日後只改其中一邊而悄悄分歧。
"""
import re

import pytest

from app.config import PROJECT_ROOT

AGENTS = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8-sig")
SKILL_DIR = PROJECT_ROOT / ".claude" / "skills" / "python-flask-skill"

# (AGENTS.md 章節編號, 規則中的一段原文, skill 內對應的檔案)
SYNCED_RULES = [
    (4, "可為空的唯一欄位：SQL Server 的 UNIQUE 約束只允許一個 NULL，SQLite 則允許多個", "references/database.md"),
    (4, "部分唯一索引（sqlite_where 與 mssql_where，以 ORM 表達式建立，不得使用原生 SQL 字串；專案提供 unique_when_not_null()）", "references/database.md"),
    (4, "alembic.ini 與 migration 範本 (script.py.mako) 必須是純 ASCII", "references/database.md"),
    (4, "alembic.ini 不得含連線字串，連線一律取自 DATABASE_URL", "references/database.md"),
    (8, "唯一例外是必須純 ASCII 的檔案（見下方檔案編碼規範）", "references/business-encoding.md"),
    (8, "以系統地區編碼讀取的檔案與腳本：", "references/business-encoding.md"),
    (8, "內含繁體中文或其他非 ASCII 字元的 .ps1 須存成 UTF-8 with BOM", "references/business-encoding.md"),
    (8, "一律以 Python 撰寫（UTF-8、參數以清單傳遞）；.ps1 只作為純 ASCII 的薄包裝", "references/business-encoding.md"),
    # 2026-10-28：備援模型、NPC 角色卡、內容分級、年齡確認與草案個資
    (5, "備援模型：主要模型發生 429、5xx 或單次呼叫逾時 20 秒時，一律切換到備援模型：客服為 gemini-3.5-flash-lite（GEMINI_FALLBACK_MODEL），RPG 為 OPENROUTER_FALLBACK_MODEL（RPG 不切換到 Gemini）", "references/llm-streaming.md"),
    (5, "暫時性錯誤 429、5xx、逾時則改為切換到備援模型，這次切換視為該次重試，不受「已耗時小於 15 秒」的限制", "references/llm-streaming.md"),
    (5, "後端單次請求總預算（含重試與切換備援）30 秒", "references/llm-streaming.md"),
    (6, "RPG 的「當前世界狀態」包含在場 NPC 的角色卡切片", "references/llm-streaming.md"),
    (7, "NPC 角色卡：NPC 的特質（表層或隱藏）、弧線階段", "references/business-encoding.md"),
    # 2026-11-03：分級由 sexual_content_level 0 至 3 改為三種模式（安全、一般、成人），成人模式是玩家個人頁面的開關
    (7, "內容分級：內容程度為三種模式，以單一欄位 content_tier 表示", "references/business-encoding.md"),
    # 2026-10-28 後續修訂：年齡確認改為生日加年齡區間，未滿 13 歲不得註冊，13 至 17 歲只能使用安全模式
    (7, "未滿 18 歲者一律為安全模式，不論任何設定都不得提高", "references/business-encoding.md"),
    (7, "玩家在個人頁面以開關決定是否開啟成人模式，預設關閉，未開啟時為一般模式", "references/business-encoding.md"),
    (7, "系統只提醒玩家可調整，不得自動調高", "references/business-encoding.md"),
    (10, "年齡確認：玩家以 Google 首次登入建立帳號後，須立即填寫生日，並從下拉式選項選擇年齡區間", "references/auth-security.md"),
    (10, "未滿 13 歲者不得使用本平台，發現時立即抹除該帳號與其全部個資", "references/auth-security.md"),
    (10, "13 至 17 歲者可以使用本平台（含 AI 功能），但只能使用安全模式", "references/auth-security.md"),
    (10, "管理員後台只提供年齡區間的彙總統計，不顯示個別使用者的生日", "references/auth-security.md"),
    (11, "連續 12 個月未登入即直接刪除（不經 7 天寬限期", "references/privacy-observability.md"),
    (11, "清除連續 12 個月未登入的帳號", "references/privacy-observability.md"),
    # 2026-10-29：閒置帳號清除不納入管理員（age-verification-birthdate 的假設，經使用者確認後寫入規範）
    (11, "角色為 admin 的帳號不納入，否則唯一的管理員閒置一年就會消失", "references/privacy-observability.md"),
    (11, "閒置帳號清除不納入管理員", "references/privacy-observability.md"),
    (11, "以去識別的統計方式了解用戶群", "references/privacy-observability.md"),
    (10, "玩家撰寫的個性、角色與世界文字同樣視為使用者文字", "references/auth-security.md"),
    (10, "待審的性格原型草案（僅含去識別後的草案內容，不含對話全文）", "references/auth-security.md"),
    (11, "性格原型草案：草案含玩家衍生文字，視為個資", "references/privacy-observability.md"),
    # 2026-10-30：RPG 以記事簿取代摘要（free 2000、plus 5000、vip 10000 字），AI 溢出可用預備空間，玩家編輯不得超過容量
    (6, "RPG 的長期記憶是記事簿（見下方），**RPG 不使用摘要**：「記事簿 + 最近 10 輪原文 + 當前世界狀態」", "references/llm-streaming.md"),
    (6, "容量依會員等級為 free 2000、plus 5000、vip 10000 字（Unicode 字元，NFC 正規化後計），另有容量 25% 的預備空間（500 / 1250 / 2500 字），預備空間只供 AI 寫入", "references/llm-streaming.md"),
    (6, "每完成 10 輪（第 10、20、30… 輪，中斷的回合不計）", "references/llm-streaming.md"),
    (6, "新條目超過剩餘空間時仍完整寫入，但總長不得超過容量加預備空間", "references/llm-streaming.md"),
    (6, "記事簿長度已超過容量時為「已滿」：不呼叫模型、不補抓被跳過的輪次", "references/llm-streaming.md"),
    (6, "儲存後的長度不得超過容量，沒有預備空間", "references/llm-streaming.md"),
    (5, "只有客服使用 GEMINI_MODEL", "references/llm-streaming.md"),
    (9, "變數名稱 GEMINI_API_KEY（只有客服使用）；RPG 用途使用 OpenAI 相容端點，變數名稱 OPENROUTER_API_KEY", "references/deployment.md"),
    (9, "所有機密（Gemini 金鑰、OpenRouter 金鑰、SECRET_KEY", "references/deployment.md"),
    (5, "模型為環境變數 OPENROUTER_MODEL（備援為 OPENROUTER_FALLBACK_MODEL），不使用 Gemini", "references/llm-streaming.md"),
    (6, "OPENROUTER_MODEL，備援 OPENROUTER_FALLBACK_MODEL，不使用 Gemini", "references/llm-streaming.md"),
    (7, "RPG 記事簿容量 2000 / 5000 / 10000 字（見第 6 節）", "references/business-encoding.md"),
    (10, "記事簿內容，含由玩家文字抓取的重點，亦同", "references/auth-security.md"),
    (11, "RPG 記事簿：內容由玩家文字衍生，視為個資", "references/privacy-observability.md"),
    (11, "RPG 模型供應商（OpenAI 相容端點，目前為 OpenRouter）與個資：免費模型的輸入輸出可能被供應商留存或用於改進產品", "references/privacy-observability.md"),
    (5, "RPG 的 OpenAI 相容端點依 OPENROUTER_JSON_SCHEMA 是否支援 JSON schema 決定", "references/llm-streaming.md"),
    # 2026-11-02：玩家只用 Google 登入（畫面只有 Google 與條款，生日登入後才補填，回訪一鍵登入，保持登入），管理員 Google 加密碼；
    # 未補填年齡的帳號最多保留 24 小時，未滿 13 歲立即抹除，登入有效期改為閒置 30 天、每次使用就續期
    (10, "有效期為閒置 30 天、每次使用就續期（由框架在每次回應時重新簽章，不另放簽發時間），不設絕對上限", "references/auth-security.md"),
    (10, "已登入時打開首頁（/）或登入頁，直接導向登入後的預設頁面（單一常數；目前為 /cs，RPG 上線後改為選擇劇本的首頁，不直接進入聊天）", "references/auth-security.md"),
    (10, "密碼：只有管理員有密碼（玩家不設密碼，只用 Google 登入）", "references/auth-security.md"),
    (10, "玩家只以 Google 登入，首次登入即註冊（沒有密碼註冊，也沒有信箱驗證信、玩家端的重設密碼信與 EMAIL_VERIFICATION_REQUIRED", "references/auth-security.md"),
    (10, "新使用者必須勾選，並記錄 terms_version 與 terms_accepted_at；回訪的使用者不必勾選，一鍵登入", "references/auth-security.md"),
    (10, "除了完成年齡確認與登出之外不得使用任何功能", "references/auth-security.md"),
    (10, "帳號自建立起最多保留 24 小時，逾時未完成即由排程刪除", "references/auth-security.md"),
    (10, "管理員通過 Google 之後還須再通過密碼驗證，兩者都通過才建立登入 session", "references/auth-security.md"),
    (10, "管理員帳號（由部署者以一次性指令建立）不受上述 24 小時與全面禁用的限制", "references/auth-security.md"),
    (10, "管理員每次登入都先以 Google 登入，第一次以同信箱登入時綁定 Google 身分，通過後再驗證一次密碼", "references/auth-security.md"),
    (10, "管理員同一帳號的密碼連續失敗 5 次鎖定 15 分鐘", "references/auth-security.md"),
    (9, "staging 為部署於雲端的預備環境，設定與 production 相同", "references/deployment.md"),
    (8, "系統寄出的信件（營運告警與管理員的重設密碼信）一律為 UTF-8 編碼與繁體中文內容", "references/business-encoding.md"),
    (11, "玩家帳號自建立起 24 小時內未完成年齡確認者同樣直接刪除", "references/privacy-observability.md"),
    (11, "清除逾時未完成年齡確認的帳號", "references/privacy-observability.md"),
    (11, "逾時帳號的清除每小時執行一次（其餘為每日）", "references/privacy-observability.md"),
    (11, "年齡確認未在 24 小時內完成、或發現未滿 13 歲時，帳號與這些資料會立即刪除", "references/privacy-observability.md"),
    # 2026-11-02 稽核後：登入與年齡確認兩個頁面、未完成年齡確認者導向年齡確認頁、staging 的唯一例外、管理員忘記密碼寄信重設
    (3, "/login（Google 登入與條款勾選，登入與註冊合一，沒有 /register）、登入後的年齡確認頁、被鎖定時的申訴頁與 /account 等帳號頁面", "references/architecture.md"),
    (10, "尚未完成年齡確認的玩家則一律導向年齡確認頁", "references/auth-security.md"),
    (9, "唯一的例外是 staging 可設定 GEMINI_BASE_URL，把 Gemini 轉接器指向隔離壓測環境的假模型伺服器", "references/deployment.md"),
    (9, "TRUST_PROXY_HEADERS（開發環境經 HTTPS 通道時信任轉送標頭）只有 development 可以設定，其餘環境設定了即啟動失敗", "references/deployment.md"),
    (10, "管理員忘記密碼時，在密碼驗證那一步按「忘記密碼」，系統寄重設信到該管理員的信箱", "references/auth-security.md"),
    (10, "此按鈕只在管理員通過 Google 之後的那一步出現，不接受匿名請求", "references/auth-security.md"),
    (10, "重設權杖為 32 位元組隨機值，資料庫只存其 SHA-256 雜湊，單次使用，1 小時失效", "references/auth-security.md"),
    (5, "客服模組因採非串流，前端直接取得完整 reply_text", "references/llm-streaming.md"),
    # 2026-11-02：帳號鎖定與申訴（鎖 Google 身分、被鎖定者只進申訴頁、管理員只看得到被鎖定者的申訴）
    (10, "帳號鎖定：玩家帳號有「鎖定」狀態，由系統自動鎖定、只有管理員能解除（不會自動解除）", "references/auth-security.md"),
    (10, "同一帳號 15 分鐘內 Google 登入被拒（已辨識出身分但被拒絕，例如帳號停用、年齡資料不符）達 5 次", "references/auth-security.md"),
    (10, "同一帳號 15 分鐘內被每分鐘訊息數上限擋下達 10 次（客服洗版）", "references/auth-security.md"),
    (10, "只能進申訴頁向管理員留言（單則上限 1000 字、同一帳號每小時最多 3 則、同一時間最多 1 則待處理），其他頁面與 API 一律回 403 與代碼 account_locked", "references/auth-security.md"),
    (10, "管理員只能看到被鎖定或被暫停發佈帳號送出的申訴，仍不得任意讀取其他對話全文", "references/auth-security.md"),
    (10, "使用者主動提交的意見回饋對話、被鎖定或被暫停發佈帳號送出的申訴、被內容安全過濾標記的對話", "references/auth-security.md"),
    (11, "申訴（帳號鎖定）：內容由使用者撰寫，視為個資", "references/privacy-observability.md"),
    (11, "帳號鎖定激增（Google 登入被拒與洗版造成的鎖定）", "references/privacy-observability.md"),
    # 2026-11-03：玩家創作並公開分享劇本（先審後發）、暱稱與筆名、自訂結局與已完結的存檔、劇本的個資與刪帳號保留偏好
    (5, "記事簿重點抓取與劇本審查的模型層", "references/llm-streaming.md"),
    (7, "到結局時存檔進入「已完結」", "references/business-encoding.md"),
    (7, "Milestone 的觸發由 Service 依玩家年齡或遊戲內年份", "references/business-encoding.md"),
    (7, "存檔開局時複製劇本內容，之後劇本的修改、下架或刪除不影響進行中的存檔", "references/business-encoding.md"),
    (7, "原始劇本（劇本版本 `script_versions` 的 `milestones_json`）為唯讀", "references/business-encoding.md"),
    (7, "角色年齡為錨定事實，模型與玩家都不能直接寫入；成年角色不得被改成未成年", "references/business-encoding.md"),
    (7, "玩家創作劇本：v1 開放玩家依固定規格創作劇本並可公開分享", "references/business-encoding.md"),
    (7, "公開分享採先審後發", "references/business-encoding.md"),
    (7, "一般的未成年角色（如親子、育兒、校園日常）不得誤擋", "references/business-encoding.md"),
    (7, "規則層於每次儲存時執行，模型層於開存檔或送出公開且內容有變動時執行，未通過者不得用來開存檔", "references/business-encoding.md"),
    (7, "安全模式須嚴格執行，由伺服器端的 system_instruction、安全設定與審查層共同套用", "references/business-encoding.md"),
    (7, "牽手、擁抱、告白與簡短的親吻等一般的親密情感表達允許", "references/business-encoding.md"),
    (7, "界線不明確時一律以較保守的判定處理", "references/business-encoding.md"),
    (7, "玩家已在切換為成人模式時完成成年確認：須勾選「我確定已滿 18 歲」與同意安全規範兩項", "references/business-encoding.md"),
    (7, "滿 18 歲後該限制自動解除，但玩家的模式與既有存檔的旗標不自動調高，介面提示玩家可自行切換", "references/business-encoding.md"),
    (7, "一般模式可有性暗示、深吻與情慾張力，但性行為與性器官只能淡出或暗示，不得描寫", "references/business-encoding.md"),
    (10, "暱稱與筆名：玩家有公開暱稱，於年齡確認的同一頁（初次設定頁）填寫", "references/auth-security.md"),
    (10, "筆名於劇本第一次發佈（審核通過）時鎖定", "references/auth-security.md"),
    (10, "劇本設定，含狀態轉移規則、結局說明與初始對話，亦同", "references/auth-security.md"),
    (10, "使用者送審公開的劇本與被檢舉的劇本", "references/auth-security.md"),
    (11, "玩家創作的劇本與暱稱：劇本設定", "references/privacy-observability.md"),
    (11, "匿名保留我已發佈的劇本", "references/privacy-observability.md"),
    (11, "劇本設定全文、暱稱與筆名", "references/privacy-observability.md"),
    # 2026-11-04：暫停發佈（獨立於帳號鎖定）、申訴範圍擴及被暫停發佈者、劇本限制存於資料庫的設定表、送審紀錄的保存、暱稱不得冒用官方署名
    (7, "同一作者連續被自動拒絕達門檻次數時，暫停其發佈權限（獨立於帳號鎖定的狀態，只停止送審，其餘功能照常；", "references/business-encoding.md"),
    (7, "這些上限與連續被拒的門檻存放於資料庫的設定表，程式每次使用時現讀而不快取於行程內，以 flask 指令修改並寫入稽核紀錄，初始值由遷移寫入", "references/business-encoding.md"),
    (10, "被暫停發佈的帳號（見第 7 節）可進申訴頁（上限與鎖定相同）", "references/auth-security.md"),
    (10, "鎖定、暫停發佈、申訴與解除都寫入稽核紀錄", "references/auth-security.md"),
    (10, "也不得冒用平台的官方署名", "references/auth-security.md"),
    (11, "只有管理員能在被鎖定或被暫停發佈的帳號下檢視", "references/privacy-observability.md"),
    (11, "被拒絕的送審只保留類別列舉值與時間（送審紀錄保存 12 個月，隨帳號刪除並納入匯出）", "references/privacy-observability.md"),
    (11, "auth_tokens 為到期後 7 天；送審紀錄 12 個月；", "references/privacy-observability.md"),
    # 2026-11-04：玩家主角（共用劇本角色規格、AI 生成或自創、一個錨定事實）與開場的「第 0 幕」；自我介紹不存
    (5, "記事簿重點抓取與劇本審查的模型層，以及玩家主角的生成與審查", "references/llm-streaming.md"),
    (6, "開場的「第 0 幕」（見第 7 節）不是一輪：它沒有玩家訊息，不計輪數，不計入每日對話輪數與記事簿「每完成 10 輪」的計數", "references/llm-streaming.md"),
    (6, "完成輪數未滿 10 輪之前，它作為歷史最前面的一則 AI 訊息放入上下文，之後隨最近 10 輪的視窗自然滑出", "references/llm-streaming.md"),
    (7, "主角與錨定事實於開存檔時一併寫入存檔（見「玩家主角」一條）", "references/business-encoding.md"),
    (7, "玩家主角：玩家開存檔時依「劇本角色規格」自創主角，與劇本角色共用同一份欄位、長度與列舉值上限", "references/business-encoding.md"),
    (7, "主角年齡是劇中角色的年齡，與使用者年齡無關", "references/business-encoding.md"),
    (7, "生成只是預覽，不寫入存檔，可重新生成（次數有上限，存放於資料庫的設定表，程式每次使用時現讀而不快取於行程內，以 flask 指令修改並寫入稽核紀錄，初始值由遷移寫入）", "references/business-encoding.md"),
    (7, "生成失敗、被審查擋下或玩家不想用時，改為自創。按「開始冒險」才開存檔", "references/business-encoding.md"),
    (7, "錨定事實每位主角一個，內容為主角與劇本中某個人物、地點或里程碑的關係，必須指向該存檔的劇本內容中存在的項目，由 Service 驗證", "references/business-encoding.md"),
    (7, "模型只提議，玩家接受或自行撰寫後才由 Service 寫入，不由模型直接寫入", "references/business-encoding.md"),
    (7, "自創時預設也須提供一個（選定項目後自行撰寫一句），玩家可略過，略過時開場僅依背景故事與角色卡", "references/business-encoding.md"),
    (7, "主角與對象之一為未成年時，錨定事實不得為戀愛或性的關係", "references/business-encoding.md"),
    (7, "主角的年齡為錨定事實，開存檔後模型與玩家都不能直接寫入", "references/business-encoding.md"),
    (7, "規則層於每次生成與送出時執行，模型層於開存檔時執行，未通過者不得開存檔；拒絕只告知類別", "references/business-encoding.md"),
    (7, "開場：主角建立完成後，由回合引擎依主角角色卡與錨定事實產生「第 0 幕」", "references/business-encoding.md"),
    (7, "產生失敗或被擋時，退回劇本的初始對話，沒有則只顯示背景故事", "references/business-encoding.md"),
    (7, "它的 token 計入全站每日用量，不計入每日對話輪數", "references/business-encoding.md"),
    (10, "玩家主角的角色卡、自我介紹與錨定事實，亦同", "references/auth-security.md"),
    (10, "玩家主角的生成與重新生成另有次數上限（見第 7 節），其 token 計入全站每日上限，不計入每日對話輪數", "references/auth-security.md"),
    (11, "玩家主角：角色卡與錨定事實由玩家文字構成，視為個資", "references/privacy-observability.md"),
    (11, "自我介紹僅用於生成當下，不寫入資料庫（資料最小化）", "references/privacy-observability.md"),
    (11, "玩家主角的角色卡、自我介紹與錨定事實、含權杖的完整網址", "references/privacy-observability.md"),
    (11, "玩家的角色描述、自我介紹、劇本與記事簿同樣屬於個資", "references/privacy-observability.md"),
    # 2026-11-05：遊戲幣、玩家重新生成與編輯輸入（理由回饋）、輸出審查與被擋回合、主角來歷、讀者頻道、故事偏好與對話人設
    (7, "遊戲幣（RPG）：RPG 劇情對話一律消耗遊戲幣；會員制智能客服不消耗", "references/business-encoding.md"),
    (7, "一輪（一則玩家訊息加一則完整的 AI 回應，含該輪的狀態呼叫與記事簿抓取）消耗 30 幣；第 0 幕不消耗", "references/business-encoding.md"),
    (7, "首次註冊登入送 200 免費幣（於年齡確認通過時發放）；每日簽到每次 200 免費幣，台灣時間 0 點刷新", "references/business-encoding.md"),
    (7, "免費幣上限 1000，只限制新發放（簽到、贈幣）", "references/business-encoding.md"),
    (7, "幣不過期（免費、付費相同）；扣款先扣免費幣、再扣付費幣", "references/business-encoding.md"),
    (7, "扣款於回合開始時原子預扣、完成時結算、失敗時原路退回", "references/business-encoding.md"),
    (7, "玩家只能對「最後一輪」重新生成其 AI 回應，或編輯該輪的玩家輸入後重送；AI 的輸出玩家不能直接修改", "references/business-encoding.md"),
    (7, "每個回合前 2 次免費，第 3 次起每次消耗 30 幣", "references/business-encoding.md"),
    (7, "玩家必須選擇分類並填寫理由（5 至 50 字，只需重點）才能送出", "references/business-encoding.md"),
    (7, "新回應須完整通過審查與狀態驗證才原子替換舊回應；失敗、中斷或被擋時保留舊內容、退回預扣、不消耗免費次數；被取代的輸入與輸出不保存", "references/business-encoding.md"),
    (7, "理由只供營運檢視，不得送入模型、不得進入 system_instruction", "references/business-encoding.md"),
    (7, "主角另有「來歷」欄位：本地人或穿越者，劇本角色沒有", "references/business-encoding.md"),
    (7, "來歷為穿越者時，由固定的 system_instruction 加入平台規則", "references/business-encoding.md"),
    (7, "讀者頻道（audience）為單選：男頻、女頻、全讀者；既有劇本視為全讀者", "references/business-encoding.md"),
    (7, "故事偏好：玩家可選男頻、女頻、全讀者，預設全讀者，只影響劇本目錄的排序", "references/business-encoding.md"),
    (7, "對話人設：玩家可保存可重用的主角角色卡模板（不含錨定事實，錨定事實綁特定劇本）", "references/business-encoding.md"),
    (7, "劇情輸出在送達玩家之前，須由伺服器端自有的審查層逐句檢查（以保留一句緩衝的方式串流，通過才釋放）", "references/business-encoding.md"),
    (7, "不另設每回合的模型層審查（模型層用於劇本、主角與記事簿條目）", "references/business-encoding.md"),
    (7, "RPG 劇情對話不使用每日對話輪數與每分鐘訊息數，改以遊戲幣控制", "references/business-encoding.md"),
    (6, "於玩家送出下一輪、該輪回覆寫入後觸發（此時前一輪已不可重新生成或編輯）", "references/llm-streaming.md"),
    (6, "與主角的來歷（本地人或穿越者）", "references/llm-streaming.md"),
    (10, "RPG：輸出被擋時，前端丟棄未完成的內容並以一行劇情轉場文字取代，不顯示錯誤", "references/auth-security.md"),
    (10, "仍被擋則再顯示轉場文字並解鎖輸入，請玩家換個行動；重新生成不另外扣遊戲幣並須記錄", "references/auth-security.md"),
    (10, "玩家於重新生成時填寫的回饋（僅分類統計與理由文字，不含帳號資料，每次讀取都寫入稽核紀錄）", "references/auth-security.md"),
    (10, "停權、刪除內容、調整會員等級與調整遊戲幣餘額等高風險操作須重新輸入密碼並寫入稽核", "references/auth-security.md"),
    (10, "RPG 的用量以遊戲幣控制（見第 7 節），不適用每日對話輪數與每分鐘訊息數", "references/auth-security.md"),
    (11, "遊戲幣與帳本：錢包與帳本（每筆的種類、金額與關聯的存檔識別碼）視為個資", "references/privacy-observability.md"),
    (11, "重新生成回饋：理由由玩家文字構成，視為個資", "references/privacy-observability.md"),
    (11, "玩家於重新生成時填寫的理由會提供營運團隊檢視、玩家的對話人設會經 AI 處理、遊戲幣與帳本會保存", "references/privacy-observability.md"),
    (11, "user_identities、auth_tokens、遊戲幣錢包與帳本與全部對話", "references/privacy-observability.md"),
]


def test_the_retired_password_registration_and_email_verification_rules_are_gone_from_agents_and_the_skill():
    """玩家端的密碼註冊、信箱驗證與 7 天絕對期限已被取代；舊文字不得殘留在權威文件或技能裡（避免日後照舊文字實作）。"""
    retired = (
        "以信箱加密碼註冊後為 pending_verification",
        "production 必須為 true 並已設定寄信服務，否則啟動失敗",
        "存活 7 天",
        "Google 登入與密碼登入共用同一帳號",
        "未滿 13 歲者不得註冊（不建立帳號",
        "除可關閉信箱驗證（見第 10 節）外",
        "系統寄出的信件（信箱驗證、重設密碼）",
        "系統寄出的信件（目前只有營運告警）",
        "管理員忘記密碼的處理另訂",
        "以及 /login、/register 等帳號頁面",
        "登入失敗激增",
        # 2026-11-03：分級改為三種模式，舊的 0 至 3 等級文字不得殘留
        "存檔帶 content_tier 與 sexual_content_level（0 至 3）旗標",
        "未滿 18 歲者一律為安全模式，sexual_content_level 固定為 0",
        "成年者由玩家主動在 sexual_content_level 1（浪漫、曖昧）與 2（感官但不露骨）之間切換",
        "系統只提醒玩家可切換分級",
        "浪漫僅限不含身體描寫的情感表達",
        "玩家已通過強化成年驗證",
        "須先於第 10 節訂定強化成年驗證的做法",
    )
    corpus = {"AGENTS.md": AGENTS}
    for path in [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]:
        corpus[str(path.relative_to(SKILL_DIR))] = path.read_text(encoding="utf-8-sig")
    leftovers = [(name, text) for name, body in corpus.items() for text in retired if text in body]
    assert leftovers == [], f"舊規則文字仍存在：{leftovers}"


def agents_section(number: int) -> str:
    match = re.search(rf"^## {number}\. .*?$(.*?)(?=^## \d+\. |\Z)", AGENTS, re.S | re.M)
    assert match, f"AGENTS.md 找不到第 {number} 節"
    return match.group(1)


@pytest.mark.parametrize(("section", "fragment", "skill_file"), SYNCED_RULES)
def test_rule_is_in_the_right_agents_section_and_mirrored_verbatim_in_the_skill(section, fragment, skill_file):
    assert fragment in agents_section(section), f"AGENTS.md 第 {section} 節缺少：{fragment}"
    skill_text = (SKILL_DIR / skill_file).read_text(encoding="utf-8-sig")
    assert fragment in skill_text, f"skill 的 {skill_file} 與 AGENTS.md 不一致，缺少：{fragment}"


def test_every_bullet_of_section_10_is_mirrored_verbatim_in_the_auth_security_reference():
    """第 10 節的 reference 是完整的逐字鏡像（其他章節的 reference 是摘要，不適用）。

    上面的片段比對只能證明「某一段原文還在」；條文改了其他字（例如密碼長度、年齡確認的做法）卻沒同步時，
    片段比對不會失敗。這裡逐條、整行比對，AGENTS.md 這一節任何一條被改動，鏡像沒跟著改就會失敗。
    """
    mirror = (SKILL_DIR / "references" / "auth-security.md").read_text(encoding="utf-8-sig")
    mirror_lines = {line.rstrip() for line in mirror.split("\n")}
    bullets = [line.rstrip() for line in agents_section(10).split("\n") if re.match(r"\s*- ", line)]
    assert len(bullets) >= 12, "沒有讀到預期數量的條文，比對可能是空洞的"
    stale = [line[:60] for line in bullets if line not in mirror_lines]
    assert not stale, f"references/auth-security.md 沒有與 AGENTS.md 第 10 節逐字一致：{stale}"


@pytest.mark.parametrize("key", ["- 保存期限：", "- 刪除帳號：", "- 當事人權利：", "- 排程工作", "- 玩家創作的劇本與暱稱：", "- 玩家主角：", "- 遊戲幣與帳本：", "- 重新生成回饋："])
def test_the_section_11_retention_and_rights_bullets_are_mirrored_verbatim(key):
    """第 11 節的這幾條在 reference 裡是逐字鏡像（「絕不記錄」在 reference 是摘要版，另以片段檢查）。"""
    agents_lines = [line.strip() for line in agents_section(11).split("\n") if line.strip().startswith(key)]
    assert len(agents_lines) == 1, key
    mirror = (SKILL_DIR / "references" / "privacy-observability.md").read_text(encoding="utf-8-sig")
    assert agents_lines[0] in {line.strip() for line in mirror.split("\n")}, f"privacy-observability.md 沒有逐字鏡像：{key}"


@pytest.mark.parametrize("key", ["- 內容分級：", "- 玩家創作劇本：", "- 無硬性結局", "- NPC 角色卡：", "- 原始劇本", "- 玩家主角：", "- 開場：", "- 遊戲幣（RPG）：", "- 玩家重新生成與編輯輸入（RPG）：", "- 故事偏好：", "- 對話人設："])
def test_the_section_7_rpg_bullets_are_mirrored_verbatim(key):
    """第 7 節 RPG 這幾條在 reference 裡是逐字鏡像（分級、劇本創作、結局與角色卡改動頻繁，整行比對才抓得到只改一邊）。"""
    agents_lines = [line.strip() for line in agents_section(7).split("\n") if line.strip().startswith(key)]
    assert len(agents_lines) == 1, key
    mirror = (SKILL_DIR / "references" / "business-encoding.md").read_text(encoding="utf-8-sig")
    assert agents_lines[0] in {line.strip() for line in mirror.split("\n")}, f"business-encoding.md 沒有逐字鏡像：{key}"


def test_the_three_content_modes_are_defined_once_and_the_levels_zero_to_three_are_not_mentioned_as_the_rule():
    """分級是 safe／standard／adult 三種模式；等級 3 的舊說法不得再當成規則出現（程式的舊 0 至 3 由後續的 change 改掉）。"""
    section7 = agents_section(7)
    for mode in ("safe（安全模式）", "standard（一般模式", "adult（成人模式"):
        assert mode in section7, mode
    assert "sexual_content_level" not in section7


def test_the_birthday_and_age_range_are_never_logged_and_the_old_checkbox_rule_is_gone_everywhere():
    mirror = (SKILL_DIR / "references" / "privacy-observability.md").read_text(encoding="utf-8-sig")
    assert "使用者 email、生日與年齡區間、使用者訊息全文" in agents_section(11)
    assert "使用者 email、生日與年齡區間、使用者訊息全文" in mirror
    for stale in ("以與同意條款分開的勾選確認年滿 18 歲", "不蒐集生日、年齡或證件", "既有帳號於帳號設定頁補確認"):
        assert stale not in AGENTS, f"AGENTS.md 仍有已被取代的年齡規則：{stale}"
        for path in SKILL_DIR.rglob("*.md"):
            assert stale not in path.read_text(encoding="utf-8-sig"), f"{path.name} 仍有已被取代的年齡規則：{stale}"


def test_skill_core_rules_mention_the_new_rules_and_checklist_items():
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8-sig")
    assert "unique_when_not_null" in skill
    assert "alembic.ini 與 script.py.mako 必須純 ASCII" in skill
    assert "UTF-8 with BOM" in skill
    assert skill.count("- [ ]") >= 14, "自查清單應包含新增的項目"
    assert "備援為 gemini-3.5-flash-lite" in skill and "age_confirmation_required" in skill


def test_the_rules_describe_what_the_code_actually_does():
    from app.models import base

    assert hasattr(base, "unique_when_not_null")
    assert (PROJECT_ROOT / "alembic.ini").read_bytes().isascii()
    assert (PROJECT_ROOT / "migrations" / "script.py.mako").read_bytes().isascii()
    assert (PROJECT_ROOT / "scripts" / "verify-container.ps1").read_bytes().isascii()
    assert (PROJECT_ROOT / "scripts" / "verify_container.py").is_file()


def test_every_ps1_script_is_ascii_or_has_a_utf8_bom():
    for script in PROJECT_ROOT.rglob("*.ps1"):
        if {".venv", "node_modules", ".git"} & set(script.relative_to(PROJECT_ROOT).parts):
            continue
        raw = script.read_bytes()
        assert raw.isascii() or raw.startswith(b"\xef\xbb\xbf"), f"{script.name} 含非 ASCII 字元卻沒有 UTF-8 BOM"


def test_rpg_notebook_rules_in_agents_and_rpg_design_doc_state_the_same_numbers():
    """記事簿的數字（容量、預備空間、每 10 輪）同時出現在 AGENTS.md 第 6 節、第 7 節與 docs/RPG.md，且 RPG 上下文不再含摘要。"""
    rpg = (PROJECT_ROOT / "docs" / "RPG.md").read_text(encoding="utf-8-sig")
    for text in (agents_section(6), rpg):
        assert "2000" in text and "5000" in text and "10000" in text
        assert "500 / 1250 / 2500" in text and "25%" in text
    assert "記事簿 + 最近 10 輪原文 + 當前世界狀態" in agents_section(6) and "記事簿 + 最近 10 輪原文 + 當前世界狀態" in rpg
    assert "RPG 不使用摘要" in agents_section(6) and "RPG 不使用摘要" in rpg
    assert "notebook_text" in rpg and "string memory_summary" not in rpg, "資料模型應改用記事簿欄位、不再有摘要欄位"


def test_agents_new_rules_use_the_existing_bullet_structure():
    """新增的條文必須沿用既有的項目符號格式（與同節其他條文一致），而不是另起一套。"""
    section4 = agents_section(4)
    assert re.search(r"^- 可為空的唯一欄位：", section4, re.M)
    assert re.search(r"^  - alembic\.ini 與 migration 範本", section4, re.M), "應為「資料表變更 (Migration)」底下的子項目"
    section8 = agents_section(8)
    assert re.search(r"^  - 以系統地區編碼讀取的檔案與腳本：", section8, re.M), "應為「檔案編碼與 I/O 規範」底下的子項目"
    assert len(re.findall(r"^    - ", section8.split("以系統地區編碼讀取的檔案與腳本：")[1], re.M)) >= 3
    # 2026-10-28 新增的條文同樣是所在章節的項目符號，而不是另起格式
    assert re.search(r"^- 備援模型：", agents_section(5), re.M)
    assert re.search(r"^- 年齡確認：", agents_section(10), re.M)
    assert re.search(r"^  - NPC 角色卡：", agents_section(7), re.M) and re.search(r"^  - 內容分級：", agents_section(7), re.M)
