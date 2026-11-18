# Spec Delta

## MODIFIED Requirements

### Requirement: 查詢錢包
`GET /api/wallet` SHALL 回傳目前玩家的免費餘額、付費餘額、合計、免費幣上限、每輪幣價（現在只有 `standard`），以及簽到狀態：今日是否可簽到、簽到數、若現在領取會入帳多少與會作廢多少、下次可簽到的時間（台北時間下一個 0 點，ISO 8601）。回應 SHALL 另含布林欄位 `coin_exempt`：角色為 admin 且帳號啟用中時為 `true`，此時每輪幣價 SHALL 為 0；其餘使用者為 `false`，幣價為設定值。回應 SHALL NOT 包含內部編號。查詢 SHALL 先處理該使用者的逾時預扣與既有玩家的贈幣補發。

#### Scenario: 新玩家的錢包
- **WHEN** 剛完成年齡確認的玩家查詢錢包
- **THEN** 免費餘額 200、付費餘額 0、合計 200，今日可簽到，簽到預覽為入帳 200、作廢 0，`coin_exempt` 為 `false`

#### Scenario: 今日已簽到
- **WHEN** 玩家今日已簽到後查詢
- **THEN** 回報今日不可簽到，並給出下次可簽到的時間

#### Scenario: 接近上限的預覽
- **WHEN** 免費餘額 900 的玩家今日尚未簽到並查詢
- **THEN** 預覽為入帳 100、作廢 100

#### Scenario: 回應不含內部編號
- **WHEN** 檢查回應內容
- **THEN** 沒有使用者或帳本的內部整數編號

#### Scenario: 管理員的錢包
- **WHEN** 啟用中的管理員查詢錢包
- **THEN** `coin_exempt` 為 `true`、每輪幣價 `standard` 為 0，餘額照實回報（不因查詢而發放任何幣）

#### Scenario: 一般玩家的幣價不變
- **WHEN** 一般玩家（任何會員等級）查詢錢包
- **THEN** `coin_exempt` 為 `false`，每輪幣價為設定值
