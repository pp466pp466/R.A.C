# Spec Delta

## Purpose

定義玩家檢舉已上架劇本的行為：誰可以檢舉哪些劇本、理由與自寫文字的格式、每個版本每人一次與每日上限、版本離開上架時的自動結案、檢舉內容的隱私，以及劇本詳情頁的檢舉入口。檢舉只升高管理員的處理優先度，不會自動下架，檢舉人不會收到任何通知。

## ADDED Requirements

### Requirement: 玩家可檢舉上架中的劇本版本
`POST /api/sessions/catalog/<版本識別碼>/report` SHALL 讓已登入且通過年齡確認的玩家檢舉一個 `published` 的版本，本文為 `{"reasons": [理由…], "text": 選填文字}`。成功 SHALL 回 `{ok: true}` 與 HTTP 201，並建立一筆未結案的檢舉。受限登入狀態（鎖定、等待刪除）SHALL 被既有的閘門擋下；尚未完成年齡確認 SHALL 回 403 與代碼 `age_confirmation_required`，且不建立任何資料。

#### Scenario: 成功檢舉
- **WHEN** 通過年齡確認的玩家以一個理由檢舉他人上架中的劇本版本
- **THEN** 回應 201，資料庫有一筆未結案的檢舉，版本狀態不變，仍在公開目錄

#### Scenario: 尚未完成年齡確認
- **WHEN** 尚未完成年齡確認的帳號送出檢舉
- **THEN** 回應 403 與 `age_confirmation_required`，沒有檢舉被建立

#### Scenario: 未登入
- **WHEN** 沒有登入的請求送出檢舉
- **THEN** 回應 401，沒有檢舉被建立

### Requirement: 只有上架中的版本可被檢舉
目標版本不是 `published`（草稿、待審、被退回、被取代、已下架）、不存在或識別碼格式不對 SHALL 得到相同的 404，且 SHALL NOT 寫入任何資料。這個判斷 SHALL 早於本文的驗證。

#### Scenario: 已下架的版本
- **WHEN** 玩家檢舉一個已下架的版本
- **THEN** 回應 404，與不存在的識別碼相同

#### Scenario: 他人的草稿
- **WHEN** 玩家以他人草稿的識別碼檢舉
- **THEN** 回應 404，沒有檢舉被建立

### Requirement: 自己的劇本與內建劇本不可檢舉
作者檢舉自己的劇本版本 SHALL 回 403 與代碼 `report_own_script`；內建劇本（有種子鍵）的版本 SHALL 與不存在得到相同的 404。兩者都 SHALL NOT 寫入任何資料。判斷內建劇本 SHALL 依種子鍵，不依作者是否為空。

#### Scenario: 檢舉自己的劇本
- **WHEN** 作者檢舉自己上架中的版本
- **THEN** 回應 403 與 `report_own_script`，沒有檢舉被建立

#### Scenario: 內建劇本
- **WHEN** 玩家檢舉內建劇本的版本
- **THEN** 回應 404

#### Scenario: 匿名保留的劇本可被檢舉
- **WHEN** 玩家檢舉一份作者已註銷、匿名保留的上架版本
- **THEN** 檢舉成功（它不是內建劇本，也不是檢舉人自己的）

### Requirement: 檢舉理由是固定列舉
`reasons` SHALL 是一至七個不重複的值，只能是：`minor_sexual`（涉及未成年的性內容）、`sexual_violence`（性暴力或非自願情節）、`real_person_sexual`（以真人名義的性內容）、`tier_mismatch`（內容分級不符）、`plagiarism`（抄襲或侵權）、`privacy_harassment`（洩漏個資或騷擾）、`other`（其他）。空陣列、列舉以外的值、非陣列與重複值 SHALL 回 400 並指出 `reasons` 欄位；重複值不被靜默去除。

#### Scenario: 多選
- **WHEN** 玩家同時選 `minor_sexual` 與 `plagiarism`
- **THEN** 檢舉成功，兩個理由都被記錄

#### Scenario: 沒有理由
- **WHEN** `reasons` 為空陣列
- **THEN** 回應 400 並指出 `reasons`

#### Scenario: 未知的理由
- **WHEN** `reasons` 含 `spam`
- **THEN** 回應 400，沒有檢舉被建立

### Requirement: 自寫文字限 200 字且是純文字
`text` SHALL 是選填的字串，NFC 正規化、去除控制字元與前後空白後至多 200 個 Unicode 字元，超過 SHALL 回 400 並指出 `text`；空字串與未提供視為沒有文字。文字 SHALL 只被當成純文字保存與顯示，SHALL NOT 解析任何標記語言。

#### Scenario: 恰好 200 字
- **WHEN** 文字為 200 個字元
- **THEN** 檢舉成功

#### Scenario: 201 字
- **WHEN** 文字為 201 個字元
- **THEN** 回應 400 並指出 `text`，沒有檢舉被建立

#### Scenario: 控制字元
- **WHEN** 文字含控制字元
- **THEN** 保存的文字已去除控制字元

### Requirement: 同一人對同一版本只能檢舉一次
同一位玩家對同一個版本 SHALL 只能有一筆檢舉，不論先前的檢舉是否已結案；重複 SHALL 回 409 與代碼 `already_reported`，且 SHALL NOT 消耗每日上限。同一劇本的新版本經核可上架後，先前檢舉過舊版本的玩家 SHALL 可以檢舉新版本。兩個並行的相同請求 SHALL 只有一個成功，另一個得到 409。

#### Scenario: 重複檢舉
- **WHEN** 玩家對同一版本送出第二次檢舉
- **THEN** 回應 409 `already_reported`，仍只有一筆

#### Scenario: 先前的檢舉已結案
- **WHEN** 玩家先前的檢舉已被結案為「不成立」，他再次檢舉同一版本
- **THEN** 回應 409 `already_reported`

#### Scenario: 新版本
- **WHEN** 玩家檢舉過第 1 版，第 2 版核可上架後他再檢舉第 2 版
- **THEN** 檢舉成功

#### Scenario: 並行
- **WHEN** 同一玩家同時送出兩個相同的檢舉
- **THEN** 一個回 201、另一個回 409，只有一筆資料

### Requirement: 每人每日有檢舉上限
每位玩家每個台灣日（0 點刷新）的檢舉筆數 SHALL 不超過營運限制 `script_report_daily`（初始 5）；達到上限 SHALL 回 429 與代碼 `report_limit`，並附距離明日 00:00（台灣時間）的 `retry_after` 秒數，且 SHALL NOT 建立檢舉。上限為 0 表示暫停接受檢舉。上限 SHALL 每次使用時從資料庫現讀。

#### Scenario: 第 6 次
- **WHEN** 玩家當日已檢舉 5 個不同的版本，再檢舉第 6 個
- **THEN** 回應 429 `report_limit`，帶 `retry_after`，沒有檢舉被建立

#### Scenario: 隔日重置
- **WHEN** 台灣時間跨過 0 點後玩家再檢舉
- **THEN** 檢舉成功

#### Scenario: 調整立即生效
- **WHEN** 部署者把上限改為 1，玩家當日已檢舉 1 次
- **THEN** 下一個檢舉請求得到 429

#### Scenario: 上限為 0
- **WHEN** 上限為 0
- **THEN** 任何檢舉都得到 429

### Requirement: 檢舉不自動下架且檢舉人不會被通知
檢舉 SHALL NOT 改變版本的狀態或使它離開公開目錄。系統 SHALL NOT 對檢舉人寄信或顯示處理結果；檢舉成功的回應 SHALL NOT 含任何關於其他檢舉、檢舉數量或審核狀態的資訊。

#### Scenario: 達到很多檢舉
- **WHEN** 一個版本收到多位玩家的檢舉
- **THEN** 它仍是 `published`，仍在公開目錄

#### Scenario: 回應沒有統計
- **WHEN** 玩家成功檢舉
- **THEN** 回應只有成功標記，沒有檢舉數量

### Requirement: 版本離開上架時未結案的檢舉由系統自動結案
版本因下架（作者或管理員）離開上架時，該版本全部未結案的檢舉 SHALL 與下架同一個交易結為「下架」（`removed`）；版本被新的上架版本取代時 SHALL 結為系統專用的「被取代」（`superseded`）。下架或取代失敗而回復時，結案 SHALL 一併回復。已結案的檢舉 SHALL NOT 被重新開啟，包含版本之後被恢復上架。

#### Scenario: 管理員下架
- **WHEN** 管理員下架一個有三筆未結案檢舉的版本
- **THEN** 三筆檢舉結為「下架」，有結案時間

#### Scenario: 作者自行下架
- **WHEN** 作者下架自己被檢舉的版本
- **THEN** 未結案的檢舉結為「下架」

#### Scenario: 被新版本取代
- **WHEN** 新版本核可上架，舊版本有未結案的檢舉
- **THEN** 舊版本的檢舉結為「被取代」

#### Scenario: 恢復不重開
- **WHEN** 已因下架結案的版本之後被恢復上架
- **THEN** 原檢舉仍是已結案，沒有未結案的檢舉

#### Scenario: 轉移失敗
- **WHEN** 下架因並行的另一個操作而失敗
- **THEN** 檢舉維持未結案

### Requirement: 檢舉的文字與理由不送入模型也不進日誌與稽核
檢舉的理由與自寫文字 SHALL NOT 被送入任何模型、SHALL NOT 進入 `system_instruction`。日誌與稽核 SHALL 只含內部使用者編號、整數（理由數量、文字長度、筆數）與固定列舉，SHALL NOT 含自寫文字、劇本標題與筆名。檢舉成功與被拒絕的日誌事件 SHALL 各是固定名稱。

#### Scenario: 日誌不含文字
- **WHEN** 玩家以含特殊字串的文字檢舉
- **THEN** 該請求期間收集的日誌找不到那段文字

#### Scenario: 沒有模型呼叫
- **WHEN** 玩家檢舉
- **THEN** 沒有任何模型被呼叫

### Requirement: 檢舉人刪除帳號時其檢舉一併刪除
檢舉人的帳號被抹除時，他送出的檢舉 SHALL 一併刪除（不論是否結案）；管理員已做出的下架決定與已寫入的稽核紀錄 SHALL NOT 受影響。作者的帳號被抹除時，其被刪除版本上的檢舉 SHALL 隨版本刪除；匿名保留的版本上的檢舉 SHALL 保留。

#### Scenario: 檢舉人被抹除
- **WHEN** 檢舉過劇本的玩家帳號被抹除
- **THEN** 他的檢舉不存在，被檢舉的版本與稽核紀錄不變

#### Scenario: 作者被抹除
- **WHEN** 有被檢舉版本的作者帳號被抹除（未開啟匿名保留）
- **THEN** 版本與它的檢舉都不存在，抹除沒有被阻止

### Requirement: 劇本詳情頁提供檢舉入口
劇本詳情頁 SHALL 依詳情回應的 `report_state` 顯示：`available` 顯示「檢舉」按鈕、`reported` 顯示停用的「已檢舉」、`unavailable` 不顯示。按下「檢舉」SHALL 開啟自製 Modal（狀態由 Vue 接管，不使用 Bootstrap 的 JavaScript），內有七個理由核取方塊、選填的文字框與字數計數（至多 200），沒有選任何理由時 SHALL 不能送出。成功後 SHALL 顯示 Toast 並把狀態改為已檢舉；409 `already_reported` SHALL 同樣改為已檢舉；429 SHALL 顯示上限訊息；404 SHALL 顯示找不到；送出中 SHALL 禁用按鈕，結束後一律恢復。文字一律以純文字渲染，嚴禁 `v-html`。

#### Scenario: 可檢舉
- **WHEN** 玩家開啟他人劇本的詳情頁
- **THEN** 看得到「檢舉」按鈕

#### Scenario: 自己的劇本
- **WHEN** 作者開啟自己上架劇本的詳情頁
- **THEN** 沒有檢舉按鈕

#### Scenario: 必須選理由
- **WHEN** Modal 中沒有勾選任何理由
- **THEN** 確認按鈕停用

#### Scenario: 送出成功
- **WHEN** 玩家勾選理由並送出
- **THEN** Modal 關閉、顯示 Toast、按鈕變成停用的「已檢舉」

#### Scenario: 已達每日上限
- **WHEN** 伺服器回 429
- **THEN** Modal 內顯示當日上限已達的說明，按鈕恢復可用
