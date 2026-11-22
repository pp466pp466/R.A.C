# Spec Delta

## Purpose

定義管理員如何處理被檢舉的劇本：依優先度列出被檢舉的上架版本、讀取檢舉的理由與自寫文字（每次讀取留下稽核、不含檢舉人身分）、結案為「不成立」，以及結案為「下架」與既有管理員下架的銜接。檢舉只升高處理優先度，是否下架由管理員決定。

## ADDED Requirements

### Requirement: 被檢舉清單只列上架中且有未結案檢舉的版本
`GET /api/admin/scripts?status=reported` SHALL 只回傳狀態為 `published` 且至少有一筆未結案檢舉的版本。內建劇本不會出現（它們不可被檢舉）。每一筆 SHALL 含審核清單的一般欄位，外加 `report_count`（未結案的檢舉數）與 `red_line_reported`（未結案的檢舉中是否有人勾選 `minor_sexual`、`sexual_violence` 或 `real_person_sexual`）。清單 SHALL NOT 含檢舉的自寫文字與檢舉人身分。其他狀態的清單每一筆也 SHALL 有這兩個鍵。

#### Scenario: 只有未結案的
- **WHEN** 版本甲有一筆未結案檢舉、版本乙的檢舉都已結案、版本丙沒有檢舉
- **THEN** 被檢舉清單只有甲

#### Scenario: 其他清單也有計數
- **WHEN** 讀取 `status=published` 的清單
- **THEN** 每一筆都有 `report_count` 與 `red_line_reported`，沒有檢舉的為 0 與 false

#### Scenario: 清單沒有文字
- **WHEN** 讀取被檢舉清單
- **THEN** 回應沒有任何檢舉的自寫文字與檢舉人資訊

### Requirement: 被檢舉清單依優先度排序
被檢舉清單 SHALL 依序排序：`red_line_reported` 為真者在前；其次未結案檢舉數由多到少；其次最早的未結案檢舉時間由舊到新；最後版本編號由小到大。檢舉理由的紅線集合 SHALL 是 `minor_sexual`、`sexual_violence`、`real_person_sexual`。

#### Scenario: 紅線優先於人數
- **WHEN** 甲有 5 筆「抄襲」檢舉、乙有 1 筆「涉及未成年的性內容」檢舉
- **THEN** 乙排在甲前面

#### Scenario: 同為紅線時人數多的在前
- **WHEN** 甲有 2 筆、乙有 3 筆含紅線理由的檢舉
- **THEN** 乙排在甲前面

#### Scenario: 同人數時先檢舉的在前
- **WHEN** 甲與乙都只有一筆非紅線的檢舉，甲較早
- **THEN** 甲排在乙前面

### Requirement: 管理員可讀取版本的檢舉並每次留下稽核
`GET /api/admin/scripts/versions/<id>/reports` SHALL 回傳該版本的全部檢舉（含已結案）：每筆有理由陣列、自寫文字、建立時間、結案時間與結案結果（`removed`、`dismissed`、`superseded` 或空）。回應 SHALL NOT 含檢舉人的身分、帳號資訊與內部編號。先 SHALL 寫入 `report.read` 稽核（細節只有版本與劇本的對外識別碼與筆數），稽核寫入失敗就 SHALL 不回傳。草稿與不存在的版本 SHALL 得到相同的 404，且沒有任何寫入。

#### Scenario: 讀取
- **WHEN** 管理員讀取一個有兩筆檢舉的版本
- **THEN** 回傳兩筆含理由與自寫文字，回傳之前已有一筆 `report.read` 稽核

#### Scenario: 沒有檢舉人
- **WHEN** 讀取任一版本的檢舉
- **THEN** 回應中找不到檢舉人的對外識別碼、email 與暱稱

#### Scenario: 草稿
- **WHEN** 管理員讀取草稿版本的檢舉
- **THEN** 回應 404，沒有稽核被寫入

#### Scenario: 稽核不含文字
- **WHEN** 管理員讀取含自寫文字的檢舉
- **THEN** `report.read` 的細節沒有自寫文字與標題

### Requirement: 結案為不成立不需要密碼
`POST /api/admin/scripts/versions/<id>/reports/dismiss` SHALL 把該版本全部未結案的檢舉一次結為「不成立」（`dismissed`）並記錄結案時間，版本的狀態與公開目錄 SHALL 不變，不需要重新輸入密碼，寫入 `report.dismiss` 稽核（細節只有版本與劇本的對外識別碼與結案筆數）。沒有未結案的檢舉 SHALL 回 409 與代碼 `no_open_reports`；版本不存在回 404。結案之後新的玩家仍可檢舉該版本。

#### Scenario: 結案
- **WHEN** 管理員對有三筆未結案檢舉的版本結案
- **THEN** 三筆都是 `dismissed`、有結案時間，版本仍 `published`，有一筆 `report.dismiss` 稽核（筆數為 3）

#### Scenario: 沒有未結案的
- **WHEN** 管理員對沒有未結案檢舉的版本結案
- **THEN** 回應 409 `no_open_reports`，沒有稽核被寫入

#### Scenario: 結案後再被檢舉
- **WHEN** 結案之後另一位玩家檢舉同一版本
- **THEN** 新的檢舉成為未結案，版本再次出現在被檢舉清單

#### Scenario: 一般使用者
- **WHEN** 一般使用者呼叫結案端點
- **THEN** 回應 404，與不存在的路徑相同

### Requirement: 結案為下架沿用管理員下架
管理員以既有的 `POST /api/admin/scripts/versions/<id>/remove`（須重新輸入密碼）下架被檢舉的版本時，該版本全部未結案的檢舉 SHALL 與下架同一個交易結為「下架」。系統 SHALL NOT 另提供不經密碼的下架捷徑。下架失敗（密碼錯誤、版本不是上架中、內建劇本）時檢舉 SHALL 維持未結案。

#### Scenario: 下架並結案
- **WHEN** 管理員以正確的密碼下架被檢舉的版本
- **THEN** 版本為 `removed`，原本未結案的檢舉為 `removed` 並有結案時間，被檢舉清單不再出現它

#### Scenario: 密碼錯誤
- **WHEN** 下架請求的密碼錯誤
- **THEN** 被拒絕，檢舉仍未結案

### Requirement: 被匿名保留的劇本仍可被管理員下架
作者已註銷、匿名保留的上架版本 SHALL 可被管理員下架（須密碼、寫稽核）與結案，且 SHALL 與其他玩家劇本一樣出現在被檢舉清單。這類版本的稽核目標使用者識別碼 SHALL 為空。管理員的下架按鈕條件 SHALL 依「是否內建劇本」，不依「是否有作者」。

#### Scenario: 下架已註銷作者的劇本
- **WHEN** 管理員下架一份匿名保留的上架版本
- **THEN** 下架成功，稽核的目標使用者為空

#### Scenario: 內建劇本仍不可下架
- **WHEN** 管理員下架內建劇本
- **THEN** 回應 409 `builtin_not_removable`

#### Scenario: 後台按鈕
- **WHEN** 管理員在後台檢視匿名保留的上架版本
- **THEN** 有「下架」按鈕；檢視內建劇本時沒有

### Requirement: 後台「被檢舉」檢視
`/admin` 的「劇本審核」分頁 SHALL 新增「被檢舉」檢視，與待審、上架中、已下架並列；清單每一列顯示標題、筆名、未結案檢舉數與紅線標示。開啟版本時 SHALL 先讀取全文（既有的 `script.read` 稽核）再讀取檢舉面板（`report.read` 稽核），面板列出各筆理由（中文標籤）、自寫文字（純文字，嚴禁 `v-html`）與時間，並提供「不成立（結案）」與「下架」兩個動作；「下架」沿用既有的 Modal（類別、備註、密碼）。他人已處理（409）時 SHALL 關閉視窗、顯示原因並重新載入。所有狀態一律由 Vue 接管。

#### Scenario: 切換檢視
- **WHEN** 管理員選擇「被檢舉」
- **THEN** 載入 `status=reported` 的清單並依優先度顯示

#### Scenario: 結案
- **WHEN** 管理員在面板按「不成立（結案）」
- **THEN** 該列從被檢舉清單消失，顯示已結案的提示

#### Scenario: 他人先處理
- **WHEN** 另一位管理員已下架該版本，這位管理員按「不成立」
- **THEN** 顯示版本狀態已改變的訊息並重新載入清單
