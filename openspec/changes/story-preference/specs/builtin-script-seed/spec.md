# Spec Delta

## ADDED Requirements

### Requirement: 加入讀者頻道不使既有內建劇本被重複發佈
內建劇本的資料檔沒有 `audience` 時 SHALL 視為全讀者。已在資料庫中、內容未改的內建劇本，在系統加入讀者頻道欄位之後重跑種子指令 SHALL 判定為未變更，SHALL NOT 因此新增版本或取代上架版本。內建劇本的資料檔明確宣告 `male` 或 `female` 時，SHALL 視為內容改變，依「內容改變時新增版本取代舊版」處理。

#### Scenario: 升級後重跑
- **WHEN** 資料庫中已有首發劇本的第 1 版（建立於加入讀者頻道之前），資料檔未改，重跑指令
- **THEN** 結果為未變更，沒有新增版本

#### Scenario: 資料檔宣告頻道
- **WHEN** 資料檔新增 `"audience": "male"` 後重跑指令
- **THEN** 新增下一版為 `published` 且頻道為 `male`，舊版為 `superseded` 且內容不變

#### Scenario: 資料檔寫了明確的全讀者
- **WHEN** 資料檔新增 `"audience": "all"`，其餘未改
- **THEN** 視為沒有改變，沒有新增版本
