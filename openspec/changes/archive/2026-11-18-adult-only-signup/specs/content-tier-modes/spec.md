# Spec Delta

## REMOVED Requirements

### Requirement: 帳號頁對未成年者顯示安全模式的提示
**Reason**: 全站改為只限滿 18 歲者使用（AGENTS.md 第 10 節，2026-11-17），不再有 13 至 17 歲的使用者，帳號頁也不再顯示「因年齡限制而為安全模式」的提示。
**Migration**: 無。內容模式的其餘規則（三種模式與年齡上限）由後續的 `safe-mode-toggle` 處理；在那之前，沒有任何使用者會落入這個提示的條件。
