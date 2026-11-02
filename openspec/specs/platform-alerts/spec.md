# platform-alerts Specification

## Purpose
定義只有平台看得到的告警：5xx 比率、服務存活與就緒、寄信失敗、排程工作失敗。這些情況下應用程式自己的告警指令可能根本跑不動或寄不出信，所以必須由平台的監控獨立發現並以電子郵件通知。

## Requirements

### Requirement: 5xx 比率告警
平台告警 SHALL 在 5 分鐘內伺服器錯誤（狀態碼 500 以上）占全部請求的比率超過 5% 時通知，且 5 分鐘內請求數少於 20 時 SHALL NOT 通知（避免低流量時單一錯誤造成誤報）。來源 SHALL 是平台的請求指標或請求日誌，SHALL NOT 依賴應用程式的資料庫。

#### Scenario: 超過比率
- **WHEN** 5 分鐘內有 200 個請求，其中 12 個為 5xx
- **THEN** 觸發告警

#### Scenario: 低流量
- **WHEN** 5 分鐘內只有 10 個請求，其中 3 個為 5xx
- **THEN** 不觸發告警

### Requirement: 服務存活與就緒探測
平台告警 SHALL 以外部探測定期請求 `/healthz`（存活）與 `/readyz`（就緒，含資料庫），任一個連續失敗 3 次（探測間隔 1 分鐘）時通知。探測 SHALL 來自服務之外，且 SHALL NOT 需要登入。`/readyz` 失敗而 `/healthz` 正常 SHALL 被視為資料庫問題並在通知中分辨。

#### Scenario: 連續失敗
- **WHEN** `/healthz` 連續 3 次探測失敗
- **THEN** 觸發存活告警

#### Scenario: 只有就緒失敗
- **WHEN** `/healthz` 正常而 `/readyz` 連續 3 次失敗
- **THEN** 觸發就緒告警，通知中標明資料庫

#### Scenario: 單次失敗
- **WHEN** 只有一次探測失敗
- **THEN** 不觸發告警

### Requirement: 寄信失敗告警
平台告警 SHALL 以日誌事件 `mail.send_failed` 為來源，5 分鐘內出現 3 次以上時通知。事件本身 SHALL 不含收件人地址（沿用日誌白名單）。

#### Scenario: 連續寄信失敗
- **WHEN** 5 分鐘內出現 3 筆 `mail.send_failed`
- **THEN** 觸發告警

### Requirement: 排程工作失敗告警
平台告警 SHALL 在任何排程工作（`check-alerts`、`execute-account-deletions`、`purge-expired-data`、`purge-inactive-accounts`）執行失敗（以非零代碼結束）時通知，且 `check-alerts` SHALL 另外在**超過 15 分鐘沒有成功執行**時通知，因為它自己無法通報自己停擺。

#### Scenario: 刪除排程失敗
- **WHEN** `execute-account-deletions` 以非零代碼結束
- **THEN** 觸發告警，指出工作名稱

#### Scenario: 告警檢查停擺
- **WHEN** `check-alerts` 已超過 15 分鐘沒有成功執行
- **THEN** 觸發告警

### Requirement: 告警定義以程式表達並只通知電子郵件
平台告警的定義 SHALL 由部署腳本產生（日誌型指標、探測與告警政策），預設預覽；通知管道 SHALL 只有電子郵件，收件人 SHALL 由呼叫者提供且不寫入計畫的預設值。定義 SHALL NOT 使用會把例外內容送出本系統的外部錯誤追蹤服務。

#### Scenario: 預覽告警定義
- **WHEN** 預覽平台告警步驟
- **THEN** 輸出四類告警的條件、窗口與電子郵件通知管道，沒有收件人的預設值

#### Scenario: 文件與定義一致
- **WHEN** 比對營運文件的平台告警表與腳本產生的定義
- **THEN** 每一項告警的門檻與窗口相同
