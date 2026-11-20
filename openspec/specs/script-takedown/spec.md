# script-takedown Specification

## Purpose
定義已上架劇本的下架與恢復：作者自己下架與恢復（不需重審）、管理員下架與恢復（須重新輸入密碼並寫稽核）、各自能恢復的範圍、恢復的前置條件、下架對新存檔與既有存檔的影響，以及作者如何看到審核結果與下架狀態。

## Requirements

### Requirement: 作者可自行下架上架中的版本
`POST /api/scripts/versions/<id>/remove` SHALL 讓作者把自己 `published` 的版本改為 `removed`，記錄下架者為 `author` 與決定時間，不需要重新審核、不需要密碼、不寫管理員稽核（只記日誌事件）。非擁有者與不存在的識別碼 SHALL 得到相同的 404；版本不是 `published` SHALL 回 409 與代碼 `not_published`。被暫停發佈的作者 SHALL 仍可下架。

#### Scenario: 自行下架
- **WHEN** 作者下架自己上架中的版本
- **THEN** 版本為 `removed`，下架者為 `author`，立即不在公開目錄

#### Scenario: 他人的版本
- **WHEN** 另一位玩家對別人的版本呼叫下架
- **THEN** 回應 404，與不存在相同，版本不變

#### Scenario: 不是上架中
- **WHEN** 作者對自己的草稿或被退回的版本呼叫下架
- **THEN** 回應 409 `not_published`

#### Scenario: 被暫停發佈者
- **WHEN** 被暫停發佈的作者下架自己的上架版本
- **THEN** 下架成功

### Requirement: 作者只能恢復自己下架的版本
`POST /api/scripts/versions/<id>/restore` SHALL 讓作者把自己下架（下架者為 `author`）的 `removed` 版本恢復為 `published`，不需重新審核，內容不變，`published_at` 不變，並清除下架的記錄。管理員下架者 SHALL 得到 403 與代碼 `removal_locked`；版本不是 `removed` SHALL 回 409 `not_removed`；同劇本已有另一個 `published` 版本 SHALL 回 409 `live_exists`，版本保持 `removed`。

#### Scenario: 自行恢復
- **WHEN** 作者恢復自己下架的版本
- **THEN** 版本回到 `published`，出現在公開目錄，內容與發佈時間不變

#### Scenario: 管理員下架的不能自行恢復
- **WHEN** 作者對管理員下架的版本呼叫恢復
- **THEN** 回應 403 與 `removal_locked`，版本仍是 `removed`

#### Scenario: 已有另一個上架版本
- **WHEN** 作者下架第 1 版後第 2 版被核可上架，再嘗試恢復第 1 版
- **THEN** 第 1 版已是 `superseded`，回應 409 `not_removed`

#### Scenario: 並行恢復
- **WHEN** 兩個恢復請求同時送出
- **THEN** 最多一個成功，另一個得到 409

### Requirement: 管理員可下架與恢復並須重新輸入密碼
`POST /api/admin/scripts/versions/<id>/remove` SHALL 把 `published` 的版本改為 `removed`，下架者為 `admin`，並 SHALL 要求固定列舉的 `category`（審查類別列舉加 `other_policy`）、選填的審核備註（至多 500 字）與管理員的密碼，寫入 `script.remove` 稽核（細節含類別）。`POST …/restore` SHALL 只適用下架者為 `admin` 的版本，要求密碼並寫入 `script.restore` 稽核。密碼錯誤 SHALL 被拒絕、寫入 `reauth.failed`，版本不變；下架者為作者的版本 SHALL 回 409 `removed_by_author`；恢復時同劇本已有另一個上架版本 SHALL 回 409 `live_exists`。內建劇本（沒有作者）SHALL 不可下架（409 `builtin_not_removable`）。

#### Scenario: 管理員下架
- **WHEN** 管理員以正確的密碼與類別下架玩家劇本
- **THEN** 版本為 `removed`、下架者為 `admin`，有一筆 `script.remove` 稽核，公開目錄不再出現

#### Scenario: 缺少密碼
- **WHEN** 下架請求沒有密碼
- **THEN** 回應 400 指出密碼欄位，版本仍上架

#### Scenario: 密碼錯誤
- **WHEN** 下架請求的密碼錯誤
- **THEN** 被拒絕，有 `reauth.failed` 稽核，版本不變

#### Scenario: 管理員恢復
- **WHEN** 管理員以正確的密碼恢復自己下架的版本
- **THEN** 版本回到 `published`，有一筆 `script.restore` 稽核，決定的類別與備註清除

#### Scenario: 不恢復作者自己下架的
- **WHEN** 管理員對作者自行下架的版本呼叫恢復
- **THEN** 回應 409 `removed_by_author`

#### Scenario: 內建劇本
- **WHEN** 管理員對內建劇本呼叫下架
- **THEN** 回應 409 `builtin_not_removable`，版本仍上架

### Requirement: 下架的版本不能開新存檔
`removed` 的版本 SHALL NOT 被用來開新存檔，包含作者自己，其開存檔、創角資料與 AI 生成預覽 SHALL 與不存在的版本一樣得到 404。已經開出的存檔 SHALL 不受影響（開局時已複製內容，之後劇本的下架或刪除不影響進行中的存檔）。

#### Scenario: 他人用下架的版本開存檔
- **WHEN** 玩家嘗試以已下架版本的識別碼開存檔
- **THEN** 回應 404，沒有開出存檔

#### Scenario: 作者自己也不行
- **WHEN** 作者嘗試以自己被管理員下架的版本開存檔
- **THEN** 回應 404

#### Scenario: 既有存檔
- **WHEN** 劇本被下架時已有玩家的進行中存檔
- **THEN** 該存檔可繼續遊玩，劇情內容不變

### Requirement: 下架後仍可建立新版本
劇本最新的版本為 `removed` 時，作者 SHALL 能以它的內容建立下一個草稿並重新送審；新版本被核可上架時，同劇本的 `removed` 舊版 SHALL 改為 `superseded`（不可再恢復）。管理員下架後作者以新版本重新送審，SHALL 仍經過完整審核。

#### Scenario: 由下架的版本建立新草稿
- **WHEN** 作者對最新版已被下架的劇本建立新草稿
- **THEN** 新草稿內容等於被下架版本，版本編號加 1

#### Scenario: 新版本上架使舊版不可恢復
- **WHEN** 新版本被核可
- **THEN** 下架的舊版為 `superseded`

### Requirement: 作者看得到審核結果與下架狀態
`GET /api/scripts` 與版本詳情的每個版本摘要 SHALL 含 `decision_category`（被退回或被管理員下架時為固定類別列舉，否則為空）、`decided_at` 與 `removed_by`（`author`、`admin` 或空），SHALL NOT 含審核備註。`/scripts` SHALL 以繁體中文顯示：「已退回」附類別說明與時間、「已下架」附下架者（管理員下架時含類別說明與「如有疑問，可到客服（/cs）以意見回饋留下紀錄，或修改後重新送審」之類的引導，**不導向申訴頁**（申訴頁只開放被鎖定或被暫停發佈的帳號））；`published` 版本有「下架」按鈕、作者自行下架的版本有「恢復上架」按鈕，兩者以自製 Modal 確認；管理員下架的版本不顯示恢復按鈕。不寄信。

#### Scenario: 被退回的顯示
- **WHEN** 作者的版本被人工以 `other_policy` 退回
- **THEN** 列表顯示「已退回」與對應類別說明，沒有任何備註文字

#### Scenario: 自行下架後的按鈕
- **WHEN** 作者下架自己的版本
- **THEN** 該列顯示「已下架（作者）」與「恢復上架」按鈕

#### Scenario: 管理員下架後的按鈕
- **WHEN** 管理員下架作者的版本
- **THEN** 該列顯示「已被管理員下架」與類別說明，沒有「恢復上架」按鈕

#### Scenario: 不寄信
- **WHEN** 版本被核可、退回、下架或恢復
- **THEN** 沒有任何郵件被送出
