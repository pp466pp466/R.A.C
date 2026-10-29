# Spec Delta

## ADDED Requirements

### Requirement: 管理員判斷只有一個入口，非管理員得到 404
所有後台功能 SHALL 先通過政策物件的管理員檢查；呼叫者不是「角色為 `admin` 且帳號為 `active`」時，檢查 SHALL 以與「資源不存在」完全相同的 404 拒絕，SHALL NOT 回應 403 或任何會透露後台存在的差異。每一次被拒絕 SHALL 記錄一筆只含內部使用者編號的事件。端點與其他 Service SHALL NOT 自行比對角色。

#### Scenario: 一般使用者
- **WHEN** 一般使用者通過後台的檢查
- **THEN** 以 404 拒絕，並記錄一筆含其內部編號的事件

#### Scenario: 管理員
- **WHEN** 狀態為 `active` 的管理員通過檢查
- **THEN** 允許

#### Scenario: 停權的管理員
- **WHEN** 狀態為 `suspended` 的管理員通過檢查
- **THEN** 以 404 拒絕

### Requirement: 管理員存取他人資料只走後台專用路徑並留下稽核
管理員讀取或改變他人的資料 SHALL 只能經由後台專用的 Service 與 Repository 方法，且每個這類操作 SHALL 寫入稽核紀錄；SHALL NOT 以放寬一般端點的擁有者條件來達成。一般使用者端點（例如 `/api/cs/*`）對管理員 SHALL 與對一般使用者相同。

#### Scenario: 一般端點不因角色放行
- **WHEN** 管理員以 `/api/cs/*` 讀取他人的對話
- **THEN** 回應 404

#### Scenario: 後台讀取留下稽核
- **WHEN** 管理員經由後台讀取他人的對話
- **THEN** 有一筆對應的稽核紀錄
