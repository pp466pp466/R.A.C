# Spec Delta

## ADDED Requirements

### Requirement: 生成預覽不決定來歷
AI 生成的主角預覽 SHALL NOT 包含來歷：提示詞 SHALL NOT 要求模型輸出來歷，模型輸出含來歷欄位時 SHALL 被丟棄或使驗證失敗；來歷 SHALL 由玩家在創角時選擇（預設本地人），並隨角色卡送出。創角所需資料的 API SHALL 列出來歷的選項。

#### Scenario: 預覽沒有來歷
- **WHEN** 生成主角預覽
- **THEN** 回傳的角色卡沒有來歷欄位

#### Scenario: 模型多給了來歷
- **WHEN** 模型輸出的角色卡含來歷
- **THEN** 該欄位不會出現在預覽中

#### Scenario: 玩家選擇
- **WHEN** 玩家接受預覽並選擇穿越者後開存檔
- **THEN** 存檔的來歷為穿越者
