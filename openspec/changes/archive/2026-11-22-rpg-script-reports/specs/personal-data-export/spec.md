# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入自己送出的劇本檢舉
個人資料匯出 SHALL 納入使用者自己送出的劇本檢舉：每筆有被檢舉版本的對外識別碼、理由陣列、自寫文字與建立時間。匯出 SHALL NOT 含結案狀態與結案結果（檢舉人不會被通知結果）、其他人的檢舉，與被檢舉作者的任何資訊。JSON 為 `script_reports` 陣列；CSV 為 `script_reports.csv`（utf-8-sig，理由以分號連接）。沒有檢舉時 SHALL 是空陣列與只有標題列的檔案。

#### Scenario: JSON
- **WHEN** 送出過兩筆檢舉的使用者匯出 JSON
- **THEN** `script_reports` 有兩筆，含版本識別碼、理由、文字與時間，沒有結案結果

#### Scenario: CSV
- **WHEN** 匯出 CSV zip
- **THEN** 有 `script_reports.csv`，以 utf-8-sig 編碼，中文不亂碼

#### Scenario: 沒有檢舉
- **WHEN** 沒送出過檢舉的使用者匯出
- **THEN** JSON 為空陣列、CSV 只有標題列

#### Scenario: 不含他人的資料
- **WHEN** 其他玩家也檢舉了同一版本
- **THEN** 匯出只有自己的那一筆

### Requirement: 匯出納入匿名保留偏好
帳號資料 SHALL 納入布林欄位 `keep_published_scripts`（偏好「匿名保留我已發佈的劇本」）。

#### Scenario: 帳號資料
- **WHEN** 使用者匯出帳號資料
- **THEN** 有 `keep_published_scripts`，值為目前偏好
