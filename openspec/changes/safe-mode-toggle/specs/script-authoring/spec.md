# Spec Delta

## REMOVED Requirements

### Requirement: 宣告的內容模式不得高於作者的上限
**Reason**: 全站只限滿 18 歲，作者沒有依年齡而異的內容上限；內容模式也不再是三選一的「宣告上限」，改為「是否綁定安全模式」的選擇（見 `script-safe-binding`）。
**Migration**: 作者在創作頁勾選或取消「綁定安全模式」，內容的 `content_tier` 只會是 `safe`（綁定）或 `adult`（未綁定）；驗證不再接受或比較作者的年齡上限。
