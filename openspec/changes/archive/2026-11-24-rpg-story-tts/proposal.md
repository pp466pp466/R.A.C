# Proposal

## Why

RPG 的劇情卡現在是「一次一段、點擊翻頁」的視覺小說式閱讀（rpg-story-view）。玩家想在閱讀時聽到當前這一段的語音，例如不想盯著小字、需要輔助閱讀，或只是想要更有臨場感。目前沒有任何朗讀功能。

引擎選用**瀏覽器內建的 `speechSynthesis`**（探索階段已決定）：純前端、免費、不新增後端端點與模型供應商，AGENTS.md 的模型規範（客服只用 Gemini、RPG 用 OpenAI 相容端點）完全不動，也不需要擔心露骨內容被雲端 TTS 拒絕。

## What Changes

- 劇情卡的翻頁按鈕列（`gm-card__nav`）旁新增一顆語音按鈕（內嵌 SVG，樣式比照 `gm-iconbtn`），按下朗讀**當前段**；再按一次停止，按鈕在「播放」與「停止」之間切換，並提供 `aria-label` 與 `aria-pressed`。
- 朗讀內容是閱讀器狀態的 `text`（當前段的完整文字），不等打字機打完；「你」的行動頁也可朗讀。轉場或中斷說明那一行（`note`）與沒有內容時不可朗讀。
- 翻段、回看、重新生成與編輯、輪次被擋或丟棄、離開頁面時**立即停止**；不自動接讀下一段（維持「玩家以自己的節奏翻頁」）。
- 語言固定 `zh-TW`；沒有玩家選擇時自動挑選：優先 `zh-TW` 的語音，其次其他 `zh` 語音，同層級優先本機語音。瀏覽器沒有 `speechSynthesis` 時不顯示按鈕；語音清單已載入但沒有任何 `zh` 語音時按鈕停用，並以 `title` 說明原因。
- 語音按鈕旁新增一顆小的選單按鈕，開啟自製 Modal（沿用現有的 `ui-modal`，由 Vue 狀態接管）：列出所有中文語音（名稱、語言，並依 `localService` 標示「本機」或「線上」），每項可試聽（朗讀平台固定的短句，不朗讀劇情），另有「自動」選項。玩家從未選過語音時，第一次按語音按鈕會先開選單（清單為空時直接朗讀）。
- 只記住玩家選的語音：存在瀏覽器的 localStorage（讀寫包 `try/catch`，失敗退回自動挑選），只存語音識別（`voiceURI` 與 `name`）或「自動」，不存任何文字與播放狀態；選的語音不存在時退回自動挑選。不使用 IndexedDB，不新增後端設定。
- 新增純函式模組 `story-speech.js`（UMD，不碰 `window`、`document`，`speechSynthesis`、`SpeechSynthesisUtterance` 與儲存都由外部注入），Node 以假的 `speechSynthesis` 與假儲存測試。
- 隱私權政策（`/privacy`）補一段：朗讀使用瀏覽器內建語音，部分瀏覽器的線上語音可能把文字送到瀏覽器廠商，不經過本平台；本平台只在你的瀏覽器記住你選的語音名稱，不記住任何朗讀內容。
- 同步 `docs/前端結構-frontend.md`。

範圍外：客服（`/cs`，沒有分段）、後端與雲端 TTS、語速與音量設定、自動連續朗讀、歷史 Modal 的朗讀、跨裝置同步所選語音、第 0 幕以外的任何新資料。

## Capabilities

### New Capabilities

- `rpg-story-tts`: 劇情卡的當前段朗讀（按鈕、播放與停止的狀態、自動停止的時機、語音選擇與不可用時的降級、無障礙屬性、不保存偏好、不新增網路請求），以及隱私權政策對瀏覽器語音的揭露（現有規格中沒有隱私頁專屬的能力，這一條需求隨本能力一起定義）。

### Modified Capabilities

（無。`rpg-story-pages` 的分段與翻頁需求不變，朗讀只讀取閱讀器的狀態。）

## Impact

- 前端：新增 `app/static/js/game/story-speech.js`；擴充 `game-session.js`（建立與銷毀朗讀器、劇情卡狀態變化時停止、對外暴露朗讀狀態、語音清單與選擇）、`game.js`（注入 `speechSynthesis`、`SpeechSynthesisUtterance` 與 localStorage 存取、`pagehide` 停止）、`templates/game/session.html`（語音按鈕、選單按鈕、語音選單 Modal 與載入順序）、`css/game.css`（按鈕與選單樣式）。
- 後端：只改 `templates/legal/privacy.html` 一段文字；不新增路由、資料表、migration、環境變數、設定。
- 瀏覽器儲存：新增一個 localStorage 鍵（只存所選語音的識別與名稱）。
- 測試：新增 `tests/js/story-speech.test.js`；擴充 `tests/test_game_pages.py`（載入順序）與 `tests/test_game_story_page.py`（按鈕與 Modal 屬性、沒有 `v-html`、沒有 `data-bs-*`）。
- 文件：`docs/前端結構-frontend.md`（載入順序與第 3e-2 節）；隱私頁測試若鎖了段落則同步。
- 規範：AGENTS.md 不修改；第 1 節（離線）與第 2 節（Vue 接管狀態、無 `data-bs-*`）直接適用。
