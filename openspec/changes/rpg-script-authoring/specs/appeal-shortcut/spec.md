# Spec Delta

## Purpose

定義被鎖定或被暫停發佈的玩家在所在的頁面直接看到「前往申訴」按鈕：由哪些伺服器回應判定、在哪些頁面顯示、按鈕的形式與目的地，以及狀態解除後不再顯示。目的是讓玩家不必自己去找申訴頁。

## ADDED Requirements

### Requirement: 申訴捷徑由伺服器的狀態判定
前端 SHALL 只依伺服器的回應判定是否顯示申訴捷徑：API 回應 403 與代碼 `account_locked` 或 `publish_suspended`，或 `/api/auth/me` 的 `restriction` 為 `locked`、`publish_suspended` 為真。前端 SHALL NOT 自行推測、也 SHALL NOT 把狀態存進瀏覽器儲存空間。判定 SHALL 由單一純函式負責，各頁面共用。

#### Scenario: 鎖定的回應
- **WHEN** 任一頁面的 API 回應 403 與代碼 `account_locked`
- **THEN** 判定結果為需要顯示被鎖定的申訴捷徑

#### Scenario: 暫停的回應
- **WHEN** 送審回應 403 與代碼 `publish_suspended`
- **THEN** 判定結果為需要顯示被暫停發佈的申訴捷徑

#### Scenario: 其他錯誤
- **WHEN** API 回應 403 與其他代碼（例如 `csrf_failed`）
- **THEN** 判定結果為不顯示申訴捷徑

### Requirement: 被鎖定時在所在頁面提供前往申訴的按鈕
玩家在使用中被鎖定時，所在頁面 SHALL 以繁體中文說明帳號已被鎖定，並顯示「前往申訴」按鈕連到 `/appeal`；該頁其餘會呼叫 API 的操作 SHALL 停用，已輸入的內容 SHALL 保留。按鈕 SHALL 是一般連結，SHALL NOT 使用 Bootstrap 的 `data-bs-*` 互動，目的地固定為 `/appeal`、不帶任何使用者資料。

#### Scenario: 客服中被鎖定
- **WHEN** 玩家在 `/cs` 送出訊息時帳號已被鎖定
- **THEN** 頁面顯示說明與「前往申訴」按鈕，輸入區停用

#### Scenario: 創作中被鎖定
- **WHEN** 玩家在 `/scripts` 儲存草稿時帳號已被鎖定
- **THEN** 頁面顯示說明與「前往申訴」按鈕，編輯中的內容保留

#### Scenario: 按鈕的形式
- **WHEN** 檢查按鈕的 HTML
- **THEN** 它是指向 `/appeal` 的連結，沒有 `data-bs-*` 屬性與查詢字串

### Requirement: 被暫停發佈時在創作與帳號頁面常駐提供按鈕
被暫停發佈的玩家開啟 `/scripts` 或 `/account` 時，頁面 SHALL 常駐顯示說明（只有送審被暫停、其餘功能照常）與「前往申訴」按鈕；`/scripts` 的送審按鈕 SHALL 停用。送審回應 `publish_suspended` 時 SHALL 顯示同樣的說明與按鈕。

#### Scenario: 開啟創作頁
- **WHEN** 被暫停發佈的玩家開啟 `/scripts`
- **THEN** 頁面頂部顯示說明與「前往申訴」按鈕，送審按鈕停用，編輯與儲存照常

#### Scenario: 開啟帳號頁
- **WHEN** 被暫停發佈的玩家開啟 `/account`
- **THEN** 頁面顯示說明與「前往申訴」按鈕

#### Scenario: 送審被拒
- **WHEN** 玩家在頁面開啟後才被暫停，隨後送審得到 `publish_suspended`
- **THEN** 頁面改為顯示說明與按鈕

### Requirement: 狀態解除後捷徑消失
鎖定或暫停被解除後，玩家重新整理頁面或下一次取得登入狀態時，頁面 SHALL 不再顯示申訴捷徑；捷徑 SHALL NOT 因先前的狀態殘留。

#### Scenario: 解除暫停後
- **WHEN** 管理員解除某位玩家的暫停發佈，玩家重新整理 `/scripts`
- **THEN** 頁面沒有說明與「前往申訴」按鈕，送審按鈕可用

#### Scenario: 解除鎖定後
- **WHEN** 被鎖定的玩家被管理員解除鎖定後再開啟 `/cs`
- **THEN** 頁面正常載入，沒有申訴捷徑
