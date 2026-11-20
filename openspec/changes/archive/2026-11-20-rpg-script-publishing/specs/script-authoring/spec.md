# Spec Delta

## MODIFIED Requirements

### Requirement: 由既有版本建立新草稿
`POST /api/scripts/<id>/versions` SHALL 以劇本最新一個非草稿版本（已發佈、被退回、被取代或已下架）的內容建立下一個草稿版本，並沿用里程碑的 `id`。劇本已有進行中的版本（草稿或待審）SHALL 得到 409 與代碼 `open_version_exists`。已發佈的版本 SHALL 保持不變。

#### Scenario: 由被退回的版本修改
- **WHEN** 作者對最新版被退回的劇本建立新草稿
- **THEN** 新草稿的內容等於被退回版本，版本編號加 1

#### Scenario: 已有進行中的版本
- **WHEN** 劇本已有一個草稿時再建立新草稿
- **THEN** 回應 409 與代碼 `open_version_exists`

#### Scenario: 由已下架的版本修改
- **WHEN** 作者對最新版已被下架的劇本建立新草稿
- **THEN** 新草稿的內容等於被下架版本，版本編號加 1，被下架的版本不變
