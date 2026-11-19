# Spec Delta

## MODIFIED Requirements

### Requirement: 讀取範圍與擁有者隔離
非 `published` 的內容（`draft`、`pending_review`、`rejected`、`superseded`）SHALL 只能由擁有者讀取，所有這類讀取方法 SHALL 接收擁有者的使用者編號並以它為查詢條件；非擁有者 SHALL 得到與「不存在」相同的結果（404），不得洩漏它是否存在。`published` 的版本 SHALL 可由任何已登入的使用者讀取（內建劇本亦同），不依玩家的內容模式篩選，但讀取方法 SHALL 只回傳上架中的版本，不提供只憑識別碼取得非公開內容的方法。管理員讀取他人的待審內容 SHALL 走專用路徑並寫入稽核，不在這個能力範圍內。

#### Scenario: 擁有者讀自己的草稿
- **WHEN** 擁有者以對外識別碼讀取自己的 `draft`
- **THEN** 取得內容

#### Scenario: 非擁有者讀他人草稿
- **WHEN** 另一位使用者以正確的對外識別碼讀取別人的 `draft`
- **THEN** 得到與不存在相同的 404

#### Scenario: 公開讀取只回傳上架版本
- **WHEN** 劇本同時有 `published` 的第 1 版與 `draft` 的第 2 版，另一位玩家讀取公開內容
- **THEN** 只看到第 1 版

#### Scenario: 被取代的版本不公開
- **WHEN** 另一位玩家嘗試讀取已被取代的舊版本
- **THEN** 得到與不存在相同的結果

#### Scenario: 依內容模式篩選
- **WHEN** 安全模式開的玩家列出公開劇本，其中一個未綁定安全模式（含舊值 `standard`）
- **THEN** 清單中仍有該劇本
