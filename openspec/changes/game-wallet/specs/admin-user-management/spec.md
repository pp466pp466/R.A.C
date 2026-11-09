# Spec Delta

## ADDED Requirements

### Requirement: 調整遊戲幣餘額
`POST /api/admin/users/<public_id>/wallet/adjust` SHALL 接受 `bucket`（`free` 或 `paid`）、`delta`（非零整數，正負皆可）、`reason`（必填的文字，1 至 200 字，NFC 正規化並去除控制字元）與管理員的密碼，調整該使用者指定的餘額並寫入 `wallet.adjust` 稽核（含 `bucket`、`delta` 與調整後的餘額，理由放在稽核的理由欄）。`bucket` 不合法、`delta` 不是非零整數、理由缺少或過長 SHALL 得到 400；調整會使該餘額低於 0 SHALL 得到 409 與代碼 `insufficient_coins`；目標是管理員或不存在 SHALL 依「只能操作一般使用者」的既有規則處理。調整 SHALL 不受免費幣上限限制。管理員的使用者檢視 SHALL 多顯示免費與付費兩個餘額（只有兩個整數，不含帳本）。

#### Scenario: 增加付費幣
- **WHEN** 管理員以正確密碼為玩家增加 500 付費幣並附理由
- **THEN** 玩家付費餘額增加 500，有一筆 `wallet.adjust` 稽核含 `paid`、500 與調整後的餘額

#### Scenario: 扣到負數
- **WHEN** 管理員要從餘額 100 的免費餘額扣 300
- **THEN** 回應 409 `insufficient_coins`，餘額不變，沒有調整的稽核

#### Scenario: 不合法的欄位
- **WHEN** `delta` 為 0，或 `bucket` 是 `gold`，或沒有理由
- **THEN** 回應 400，餘額與稽核都沒有改變

#### Scenario: 超過上限
- **WHEN** 管理員為免費餘額 1000 的玩家增加 500 免費幣
- **THEN** 免費餘額為 1500

#### Scenario: 不能調整管理員
- **WHEN** 管理員嘗試調整另一位管理員的餘額
- **THEN** 被拒絕，沒有任何改變

#### Scenario: 檢視餘額
- **WHEN** 管理員檢視一位玩家
- **THEN** 看到該玩家的免費與付費餘額，沒有帳本與任何其他錢包資料

## MODIFIED Requirements

### Requirement: 危險操作須重新輸入密碼
停權、解除停權、調整等級、調整遊戲幣餘額與刪除對話 SHALL 在請求中帶管理員自己的密碼並由伺服器驗證；缺少或不正確時 SHALL 得到 400 並指出是密碼欄位，且不執行任何操作。不正確的密碼 SHALL 與登入失敗共用失敗計數與鎖定（連續 5 次鎖定 15 分鐘），並寫入 `reauth.failed` 稽核（含嘗試的動作），即使回應是錯誤。沒有設定密碼的管理員 SHALL 得到 400 與代碼 `no_password`。

#### Scenario: 密碼不正確
- **WHEN** 管理員以錯誤的密碼停權使用者
- **THEN** 回應 400 指出密碼欄位，使用者不變，並有一筆 `reauth.failed` 稽核

#### Scenario: 連續失敗
- **WHEN** 管理員連續 5 次送出錯誤的密碼
- **THEN** 帳號被鎖定 15 分鐘，之後即使密碼正確也被拒絕直到鎖定到期

#### Scenario: 沒有設定密碼
- **WHEN** 只以 Google 登入而沒有密碼的管理員送出停權
- **THEN** 回應 400 `no_password`，不執行

#### Scenario: 調整遊戲幣也要密碼
- **WHEN** 管理員以錯誤的密碼調整遊戲幣餘額
- **THEN** 回應 400 指出密碼欄位，餘額不變，並有一筆 `reauth.failed` 稽核含嘗試的動作
