# Spec Delta

## Purpose
定義管理員的劇本審核：待審佇列的清單與排序、送審過的劇本全文的讀取與稽核、核可與退回的動作（類別、只有管理員看得到的審核備註、不需重新輸入密碼）、稽核與日誌的內容界線，以及 `/admin` 的「劇本審核」分頁。先審後發的公開分享靠這個流程才能運作。

## ADDED Requirements

### Requirement: 劇本審核端點只開放給管理員
`/api/admin/scripts*` 的所有端點 SHALL 只開放給角色為管理員且帳號啟用中的使用者，其他已登入的使用者 SHALL 得到與「不存在」相同的 404，且該判斷 SHALL 早於請求本文與查詢參數的解析。回應 SHALL 只含明確組出的欄位，SHALL NOT 含作者的使用者內部編號、email、生日、年齡區間與密碼雜湊。

#### Scenario: 一般使用者
- **WHEN** 一般使用者呼叫 `GET /api/admin/scripts`
- **THEN** 回應 404，與不存在的路徑逐字相同

#### Scenario: 格式錯誤的本文
- **WHEN** 一般使用者對核可端點送出非 JSON 的本文
- **THEN** 回應 404，不是 400 或 415

### Requirement: 審核清單依狀態列出並排序
`GET /api/admin/scripts?status=` SHALL 接受 `pending_review`、`published`、`removed`（預設 `pending_review`），其他值 SHALL 回 400。`pending_review` 的清單 SHALL 把模型層結果為 `flag` 的版本排最前，其餘依送審時間由舊到新（同時間依編號）；`published` 依發佈時間由新到舊；`removed` 依下架時間由新到舊。`limit` 預設 50、至多 100，`offset` 預設 0。每筆 SHALL 只含：版本與劇本的對外識別碼、版本編號、標題、簡介、筆名與短代號、作者的對外識別碼（內建劇本為空）、讀者頻道、內容模式、狀態、送審與發佈與決定的時間、模型層結果與有疑慮的類別（僅待審）、下架者與決定的類別，以及是否有審核備註。清單 SHALL NOT 含劇本內文與審核備註全文。

#### Scenario: 有疑慮的排最前
- **WHEN** 待審有三份：甲（較舊、通過）、乙（較新、有疑慮）、丙（最新、通過）
- **THEN** 順序為乙、甲、丙

#### Scenario: 沒有疑慮時由舊到新
- **WHEN** 待審兩份都通過審查
- **THEN** 先送審的排在前面

#### Scenario: 清單沒有內文
- **WHEN** 讀取任一狀態的清單
- **THEN** 回應沒有背景、角色、里程碑與審核備註的文字

#### Scenario: 不合法的狀態
- **WHEN** `status` 為 `draft`
- **THEN** 回應 400 並指出該欄位

#### Scenario: 分頁
- **WHEN** 以 `limit=1&offset=1` 讀取有三份待審的清單
- **THEN** 回應排序後的第二份；`limit` 為 101 時回應 400

### Requirement: 送審過的版本可讀全文且每次讀取留下稽核
`GET /api/admin/scripts/versions/<id>` SHALL 回傳該版本的完整標準化內容、狀態、模型層結果與類別（僅當審查雜湊與目前內容相符）、決定的類別與時間、下架者與審核備註，並 SHALL 在回傳前寫入 `script.read` 稽核（含版本與劇本的對外識別碼與狀態）。只有送審過的版本（`pending_review`、`published`、`rejected`、`removed`、`superseded`）可讀；`draft` 與不存在的識別碼 SHALL 得到完全相同的 404，且不寫稽核。

#### Scenario: 讀取待審版本
- **WHEN** 管理員讀取待審版本
- **THEN** 回應全文，並有一筆 `script.read` 稽核

#### Scenario: 草稿不可讀
- **WHEN** 管理員以他人草稿的識別碼讀取
- **THEN** 回應 404，與不存在的識別碼逐字相同，沒有稽核

#### Scenario: 撤回的版本
- **WHEN** 作者撤回待審版本使它回到草稿後，管理員再讀取
- **THEN** 回應 404

### Requirement: 核可使版本上架
`POST /api/admin/scripts/versions/<id>/approve` SHALL 把 `pending_review` 的版本改為 `published`，同一劇本原本的上架版本改為 `superseded`（同一交易，失敗時兩者不變），第一次發佈時鎖定筆名，並寫入 `script.approve` 稽核。核可 SHALL NOT 要求重新輸入密碼，SHALL NOT 寄信。版本不是待審時（含已被作者撤回或並行的另一次決定）SHALL 回 409 與代碼 `not_pending`，沒有任何狀態或稽核改變。

#### Scenario: 核可
- **WHEN** 管理員核可一份待審版本
- **THEN** 該版本為 `published` 並出現在公開目錄，有一筆 `script.approve` 稽核

#### Scenario: 取代舊版
- **WHEN** 劇本的第 1 版上架中，管理員核可第 2 版
- **THEN** 第 2 版為 `published`，第 1 版為 `superseded`

#### Scenario: 第一次發佈鎖定筆名
- **WHEN** 從未發佈過的劇本被核可
- **THEN** 筆名被鎖定並有鎖定時間

#### Scenario: 不需要密碼
- **WHEN** 核可請求沒有密碼欄位
- **THEN** 核可成功

#### Scenario: 已被撤回
- **WHEN** 作者撤回後，管理員才送出核可
- **THEN** 回應 409 與 `not_pending`，沒有稽核

#### Scenario: 並行的兩個決定
- **WHEN** 兩位管理員同時對同一版本分別核可與退回
- **THEN** 只有一個成功，另一個得到 409 `not_pending`

### Requirement: 退回只告知類別並可附只有管理員看得到的備註
`POST /api/admin/scripts/versions/<id>/reject` SHALL 要求固定列舉的 `category`（審查類別列舉加人工專用的 `other_policy`），可附選填的審核備註（NFC 正規化、去除控制字元、至多 500 字）。成功時 SHALL 把版本改為 `rejected`、記錄類別、備註與決定時間，並寫入 `script.reject` 稽核（細節只含類別）。退回 SHALL NOT 要求重新輸入密碼，SHALL NOT 計入送審次數或連續被自動拒絕的計算，SHALL NOT 寄信。類別不在列舉內 SHALL 回 400；版本不是待審 SHALL 回 409 `not_pending`。

#### Scenario: 退回
- **WHEN** 管理員以類別 `other_policy` 與備註退回待審版本
- **THEN** 版本為 `rejected`，記有類別、備註與時間，有一筆只含類別的 `script.reject` 稽核

#### Scenario: 缺少類別
- **WHEN** 退回請求沒有類別
- **THEN** 回應 400 並指出 `category`，版本仍是待審

#### Scenario: 不是自動拒絕
- **WHEN** 作者的版本被人工退回
- **THEN** 該作者的連續被自動拒絕計數與每日送審次數不變

#### Scenario: 備註過長
- **WHEN** 備註為 501 字
- **THEN** 回應 400 並指出 `note`

### Requirement: 審核備註不外洩
審核備註 SHALL 只出現在管理員讀取版本的回應中，SHALL NOT 出現在作者的任何回應、公開目錄、個人資料匯出、稽核紀錄與日誌；它隨所屬版本保存，版本或帳號被刪除時一併刪除。

#### Scenario: 作者讀取
- **WHEN** 作者讀取自己被退回的版本與劇本列表
- **THEN** 回應有類別與時間，沒有備註的文字或欄位

#### Scenario: 稽核與日誌
- **WHEN** 管理員以含備註的退回請求完成退回
- **THEN** 稽核紀錄與日誌中找不到備註的文字

#### Scenario: 帳號刪除
- **WHEN** 被退回劇本的作者刪除帳號
- **THEN** 該版本連同備註不存在

### Requirement: 稽核動作與重新輸入密碼的界線
審核相關的稽核動作 SHALL 為 `script.read`、`script.approve`、`script.reject`、`script.remove`、`script.restore`，管理員欄位為執行者，細節含版本與劇本的對外識別碼、狀態與類別列舉，SHALL NOT 含劇本文字、標題、筆名、審核備註與作者的 email。讀取、核可與退回 SHALL NOT 要求重新輸入密碼；下架與恢復 SHALL 要求（見 `script-takedown`）。稽核 SHALL 與動作在同一個交易中成立。

#### Scenario: 稽核內容
- **WHEN** 檢查一次核可產生的稽核
- **THEN** 有版本與劇本的對外識別碼，沒有標題、筆名與任何劇本文字

### Requirement: 「劇本審核」分頁
`/admin` SHALL 有「劇本審核」分頁，含待審、上架中、已下架三個清單，點進一個版本以 Modal 顯示全文（純文字，嚴禁 `v-html`），待審版本有「核可」與「退回」（退回的 Modal 有類別下拉與備註輸入），上架中有「下架」，已下架且由管理員下架者有「恢復」；下架與恢復的 Modal 有密碼欄位。全部的顯示與 Modal 由 Vue 的響應式狀態接管，不使用 Bootstrap 的 JavaScript。操作成功顯示 Toast，失敗顯示繁體中文原因（含 409 時重新載入清單）。

#### Scenario: 文字不被當成標記
- **WHEN** 劇本標題含 `<b>` 字元
- **THEN** 清單與 Modal 顯示原文，沒有被解析為 HTML

#### Scenario: 核可後清單更新
- **WHEN** 管理員核可一份待審版本
- **THEN** 它離開待審清單並出現在上架中，顯示成功 Toast

#### Scenario: 他人已處理
- **WHEN** 管理員按核可時該版本已被撤回
- **THEN** 顯示「這份劇本已不在待審」的訊息並重新載入清單
