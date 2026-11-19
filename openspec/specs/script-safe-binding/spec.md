# script-safe-binding Specification

## Purpose
定義劇本「綁定安全模式」的語意與畫面：作者可選擇讓劇本不論玩家設定一律以安全模式遊玩；劇本內容以 `content_tier` 表示（`safe` 為綁定、`adult` 為未綁定），舊值 `standard` 讀作未綁定。涵蓋創作頁的勾選與限制提示、目錄卡片的標示，以及綁定對審查的意義。生效模式的計算見 `content-tier-modes`。

## Requirements

### Requirement: 綁定安全模式的表示與預設
劇本內容的 `content_tier` SHALL 以 `safe` 表示「綁定安全模式」、以 `adult` 表示「未綁定」；新建的劇本草稿預設 SHALL 為未綁定（`adult`）。已儲存的舊值 `standard` SHALL 一律讀作 `adult`（未綁定），且 SHALL NOT 因讀取而改寫已發佈的版本。綁定與否 SHALL 屬於版本內容：已發佈的版本不可變，修改綁定須產生新版本並依既有規則重新送審。

#### Scenario: 新草稿的預設
- **WHEN** 作者開始建立新劇本
- **THEN** 草稿的 `content_tier` 為 `adult`，創作頁的「綁定安全模式」未勾選

#### Scenario: 舊值讀作未綁定
- **WHEN** 讀取 `content_tier` 為 `standard` 的既有版本
- **THEN** 結果視為未綁定，版本內容與其雜湊沒有被改寫

#### Scenario: 修改綁定產生新版本
- **WHEN** 作者修改已發佈版本的綁定並儲存
- **THEN** 產生新的草稿版本並須重新送審，原版本不變

### Requirement: 綁定的劇本一律以安全模式遊玩
劇本為綁定（`safe`）時，所有以它開的存檔 SHALL 不論玩家的安全模式設定一律以安全模式遊玩；劇本為未綁定時 SHALL 跟隨玩家目前的設定。綁定與否 SHALL 取自存檔開啟時複製的劇本內容，之後劇本的下架、修改或刪除不影響進行中的存檔。

#### Scenario: 綁定的劇本
- **WHEN** 已關閉安全模式的玩家遊玩綁定安全模式的劇本
- **THEN** 該存檔的生效模式為安全

#### Scenario: 未綁定的劇本
- **WHEN** 已關閉安全模式的玩家遊玩未綁定的劇本
- **THEN** 該存檔的生效模式為安全關

#### Scenario: 劇本日後改版
- **WHEN** 玩家用綁定的第 1 版開了存檔，作者之後發佈未綁定的第 2 版
- **THEN** 該存檔仍依第 1 版的綁定以安全模式遊玩

### Requirement: 創作頁的勾選與限制提示
創作頁 SHALL 以一個「綁定安全模式」勾選框取代原本的三選一內容模式下拉；勾選框旁 SHALL 說明：綁定後不論玩家設定一律以安全模式遊玩，因此劇本內容須符合安全模式界線，不適合露骨或性暗示的情節；未勾選時跟隨玩家設定，但四類絕對禁止的內容仍會被擋下。送出內容 SHALL 為 `safe` 或 `adult`。編輯唯讀的版本時勾選框 SHALL 為禁用。

#### Scenario: 勾選與取消
- **WHEN** 作者勾選「綁定安全模式」並儲存
- **THEN** 送出的 `content_tier` 為 `safe`；取消勾選則為 `adult`

#### Scenario: 提示文字
- **WHEN** 作者開啟創作頁的編輯畫面
- **THEN** 勾選框旁有綁定的限制說明，沒有「一般模式」「成人模式」的選項

#### Scenario: 編輯舊的 standard 版本
- **WHEN** 作者開啟 `content_tier` 為 `standard` 的舊草稿
- **THEN** 勾選框顯示為未勾選，儲存後內容為 `adult`

#### Scenario: 唯讀版本
- **WHEN** 作者檢視已發佈或待審的版本
- **THEN** 勾選框為禁用

### Requirement: 目錄卡片標示綁定
劇本目錄的卡片 SHALL 以標籤標示綁定安全模式的劇本（例如「綁定安全模式」），未綁定的劇本 SHALL NOT 顯示三種舊模式標籤（安全、一般、成人）。標籤文字 SHALL 以純文字渲染。

#### Scenario: 綁定的卡片
- **WHEN** 目錄含一個綁定安全模式的劇本
- **THEN** 該卡片有「綁定安全模式」標籤

#### Scenario: 未綁定的卡片
- **WHEN** 目錄含一個 `content_tier` 為 `standard` 的舊版本
- **THEN** 該卡片沒有綁定標籤，也沒有「一般模式」字樣

### Requirement: 綁定安全模式時的審查尺度
劇本為綁定（`safe`）時，審查層 SHALL 以安全模式的界線檢查劇本文字：出現性暗示或露骨內容 SHALL 判為 `tier_mismatch`（有疑慮，進人工）；未綁定的劇本 SHALL NOT 因性內容而判為 `tier_mismatch`。四類必須拒絕的類別 SHALL 不論綁定與否一律執行。

#### Scenario: 綁定卻含露骨內容
- **WHEN** 綁定安全模式的劇本文字含露骨性描寫
- **THEN** 結果為 `flag`，類別含 `tier_mismatch`

#### Scenario: 未綁定的露骨內容
- **WHEN** 未綁定的劇本含成年角色之間的露骨描寫，沒有任何必須拒絕的類別
- **THEN** 不因此判為 `tier_mismatch`

#### Scenario: 絕對禁止項不受綁定影響
- **WHEN** 未綁定的劇本同一句同時出現未滿 18 歲的年齡與性行為的描述
- **THEN** 結果為 `reject`，類別為 `minor_sexual`
