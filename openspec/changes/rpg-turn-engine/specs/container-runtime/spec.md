# Spec Delta

## ADDED Requirements

### Requirement: RPG 串流使用獨立的 gunicorn 設定
專案 SHALL 提供 RPG 串流專用的 gunicorn 設定（`gunicorn.rpg.conf.py`），與客服池的設定分開：多執行緒（gthread）、worker 逾時大於單次生成上限與心跳間隔、優雅結束時間足以讓進行中的回合寫回，且同樣不使用開發伺服器。映像 SHALL 包含這份設定；啟動時選用哪一份設定由部署決定，實際的路徑分流屬後續的 change。

#### Scenario: 設定存在
- **WHEN** 檢查專案與映像
- **THEN** 兩份 gunicorn 設定都存在，RPG 設定的逾時大於 60 秒的生成上限

#### Scenario: 不使用開發伺服器
- **WHEN** 以 RPG 設定啟動
- **THEN** 主行程為正式 Web 伺服器
