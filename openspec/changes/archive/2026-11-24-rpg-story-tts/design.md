# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/rpg-story-tts/spec.md`。

現況（已讀程式確認）：

- 劇情卡的當前段由 `story-reader.js` 的狀態機管理；它每次變化都呼叫 `onChange(state)`，`game-session.js` 再以 `Game.cardView` 整理成響應式的 `card`（欄位 `key`、`index`、`fullText`〔當前段完整文字〕、`note`、`speaker`、`typing`）。`state.text` 在打字中也是完整文字，所以朗讀不必等打字機。
- 翻頁按鈕列是 `session.html` 的 `gm-card__nav`；圓形圖示按鈕的樣式是 `game.css` 的 `gm-iconbtn`（建議燈泡在用）。
- 純函式模組採 UMD、不碰 `window`／`document`，依賴由 `game.js`（唯一碰 DOM 的遊戲腳本）注入，Node 以替身測試；載入順序由 `tests/test_game_pages.py` 固定。
- 前端規範：Vue 完全接管顯示狀態、不用 `data-bs-*`、不用 `v-html`、不用 CDN。專案目前沒有 CSP（`security_headers.py` 刻意不送），不影響 `speechSynthesis`。
- 隱私頁 `templates/legal/privacy.html` 已有「對話內容傳送至 Gemini」「RPG 模型服務」兩節；`tests/test_legal_pages.py` 逐段守門。

## Goals / Non-Goals

**Goals:**
- 朗讀狀態機是純函式模組，Node 以假的 `speechSynthesis` 完整測試，包含語音延後載入、過期事件與錯誤。
- 朗讀與閱讀器**單向耦合**：朗讀只讀劇情卡的狀態，閱讀器、串流、扣幣、同步完全不知道朗讀的存在。
- 任何「畫面內容改變」的路徑都不可能留下殘餘的聲音。

**Non-Goals:**
- 不做自動連續朗讀、語速與音量設定、歷史 Modal 朗讀、客服朗讀、跨裝置同步所選語音（語音清單依裝置而異，同步沒有意義）。
- 不引入任何語音庫、音檔或雲端服務。
- 不處理「朗讀時高亮目前的字」（需要 `boundary` 事件，各瀏覽器支援不一）。

## Decisions

### D1. 新模組 `story-speech.js`：自己維護狀態，不信任瀏覽器事件的順序
`app/static/js/game/story-speech.js`（UMD）匯出 `createStorySpeech(options)`，`options`：`synth`（`speechSynthesis`）、`Utterance`（`SpeechSynthesisUtterance` 建構式）、`storage`（`{ getItem, setItem }`，由 `game.js` 包好 try/catch 後注入，缺席或丟例外視為沒有儲存）、`lang`（預設 `'zh-TW'`）、`onChange(state)`。回傳 `speak(text)`、`stop()`、`toggle(text)`、`preview(voiceKey)`、`choose(choice)`、`destroy()`、`state`。

狀態 `{ availability, speaking, voices, choice, selectedKey }`（`voices`、`choice` 與 `selectedKey` 見 D2）：`availability` 為 `'unsupported'`（沒有 `synth` 或 `Utterance`）、`'no-voice'`（語音清單已載入但沒有中文語音）、`'ready'`。

呼叫介面（apply 批 1 定案）：`toggle(text)` 回傳 `'started'`｜`'stopped'`｜`'needs-choice'`｜`'unavailable'`；`speak(text)`、`preview(key)`、`choose(selection)` 回傳布林；`choose` 的參數為 `{ mode: 'auto' }` 或 `{ mode: 'voice', key }`（`key` 必須在目前的中文語音清單中，否則拒絕）。`choose` 與 `stop` 一樣會停止進行中的朗讀或試聽，所以選單選定後不會留下殘餘的試聽聲。`speak` 前會重讀一次語音清單（部分瀏覽器不觸發 `voiceschanged`）。`onChange` 只在狀態內容真的改變時呼叫，且畫面端丟出例外不得破壞狀態。

各瀏覽器對 `cancel()` 之後的事件不一致（有的送 `end`、有的送 `error: 'interrupted'` 或 `'canceled'`、有的什麼都不送），所以狀態機**自己**決定 `speaking`：每次 `speak` 產生新的遞增代號，只有目前代號的 `end`／`error` 能把 `speaking` 設回假；`stop()` 先把代號作廢、同步把 `speaking` 設為假，再呼叫 `cancel()`。`speak` 一律先 `cancel()`（Chrome 在上一句尚未結束時直接 `speak` 偶爾會卡住）。
替代案：直接用 `synth.speaking` 當狀態——否決：它在 `cancel()` 之後到下一個事件之間仍可能為真，按鈕會閃爍或卡在「停止」。

### D2. 語音選擇：玩家的選擇優先，其次語言層級，同層級優先本機語音
**選擇的三態**（`choice`）：`none`（從未選過）、`auto`（玩家選了「自動」）、`voice`（玩家選了某個語音）。「自動」不等於「清除成從未選過」，否則玩家選了自動之後，每次按語音按鈕又會跳出選單。

**儲存**：單一 localStorage 鍵 `rpg.speech.voice`，值為 JSON：`{"mode":"auto"}` 或 `{"mode":"voice","voiceURI":"…","name":"…"}`；鍵不存在＝`none`。只存這兩個欄位，不存任何文字。讀寫由 `game.js` 注入的 `storage` 完成（包 try/catch），讀到壞掉的 JSON、未知的 `mode` 或型別不對，一律視為 `none`；寫入失敗時本次載入內仍以記憶體中的選擇為準。

**比對**：先以 `voiceURI` 完全相同找；找不到再以 `name` 加語言相同找（Chrome 與 Edge 更新後 `voiceURI` 偶爾會變而名稱不變）；仍找不到就自動挑選，且**不改寫**已儲存的選擇（語音可能只是暫時沒載入）。

**自動挑選**：`zh-TW`（含 `zh_TW`、不分大小寫）→ `zh-Hant*` → 其他 `zh*`；同一層級內**優先 `localService` 為真者**（文字不離開本機）。`utterance.lang` 一律設為 `zh-TW`；有挑到語音才設 `utterance.voice`。
理由：Chrome 在 Windows 的「Google 國語（臺灣）」是線上語音，文字會送給 Google；本機語音（例如 Windows 的 Hanhan／Yating）音質稍低但不出本機，與專案的隱私取向一致。玩家想用線上語音可以在選單自己選，所以「預設保守、玩家可改」。

**`voices`（給選單）**：只含中文語音，每項 `{ key, name, lang, local }`（`key` 為 `voiceURI`，空的時候用名稱；相同 `key` 只留第一個），排序＝語言層級、再本機優先、再名稱。`selectedKey` 是玩家選的語音在目前清單中的 `key`（`choice` 為 `voice` 且找得到時），否則為空字串，給選單標示目前的選項。自動挑選就是排序後的第一個語音。`local` 取自 `SpeechSynthesisVoice.localService`；部分瀏覽器（舊版 Safari、Firefox 的某些平台）該欄位恆為真或缺席，缺席時視為本機並不標示「線上」，避免誤導。

**試聽**：`preview(key)` 以指定語音朗讀平台固定常數「你好，這是語音試聽。」，沿用 `speak` 的代號與取消機制（試聽也會先 `cancel()` 並作廢進行中的劇情朗讀）。常數放在模組內，不來自玩家文字。

**第一次按語音按鈕**：`toggle` 在 `choice === 'none'` 且 `voices` 非空時不朗讀，回傳 `'needs-choice'`，由 `game-session.js` 開啟選單；`voices` 為空時直接朗讀。

### D3. 可用性三態與語音清單的延後載入
`getVoices()` 在 Chrome 初次呼叫常回空陣列，之後才觸發 `voiceschanged`。規則：清單為空＝「還不知道」，視為 `'ready'`（交給瀏覽器依 `lang` 挑；Android 上清單為空仍能朗讀）；清單非空而沒有任何中文語音＝`'no-voice'`；`voiceschanged` 時重新評估並通知畫面。`destroy()` 移除該監聽。

### D4. 停止時機：以「頁面身分」與明確呼叫兩條路徑，缺一不可
- 身分路徑：`game-session.js` 在 reader 的 `onChange` 裡比較「頁面身分」`key + ':' + index + ':' + fullText` 與上一次，不同就 `stop()`。這涵蓋翻段、回看、換輪、劇情被丟棄（`discard`）、載入與還原。
- 明確路徑：閱讀器的狀態不一定在修訂開始時就改變（重新生成串流開始前，劇情卡內容仍是舊的），所以送出行動、重新生成與編輯開始、放棄、重送的入口各呼叫一次 `stop()`。實作時以單一函式 `stopSpeech()` 集中，tasks 逐一列出呼叫點。
- 頁面路徑：`game.js` 在 `pagehide` 與 Vue 卸載時呼叫 `destroy()`。（`visibilitychange` 不處理：切到別的分頁繼續聽是合理的。）
替代案：只靠身分路徑——否決：修訂進行中舊畫面仍在，聲音會一直讀舊內容。

### D5. 按鈕的停用條件與顯示
按鈕在 `card.visible` 時出現於 `gm-card__nav` 最左邊（在 `‹` 前，同列右對齊的現有排版不動）：語音按鈕緊接一顆較小的選單按鈕（向下箭頭圖示，`aria-label`「選擇語音」；`availability` 為 `unsupported` 時一併隱藏，`no-voice` 時停用）。選單按鈕不受 `card.note` 與空內容的影響（沒有可朗讀的內容時仍可挑選語音）。`v-if="speech.availability !== 'unsupported'"`；`:disabled` 於「`card.note` 非空、`card.fullText` 為空、`availability === 'no-voice'`」。播放與停止用兩個內嵌 SVG（`v-if` 切換），樣式沿用 `gm-pagebtn` 的尺寸與焦點樣式，不新增顏色。`:aria-pressed`、`:aria-label`（朗讀此段／停止朗讀）、`:title`（停用時說明原因）。朗讀的是 `fullText`，即使打字機仍在進行；`gm-sr` 區塊維持現狀（打字結束才提供給輔助科技），朗讀與它互不影響。

### D6. 注入與生命週期
`game.js` 把 `window.speechSynthesis`、`window.SpeechSynthesisUtterance`（存取丟例外時視為不存在）與一個包好 try/catch 的 localStorage 存取物件放進 `deps`；`game-session.js` 建立 `createStorySpeech` 並把 `speech`（響應式的 `{ availability, speaking, voices, choice }`）與 `speakCurrent()`（讀目前 `card.fullText`；`'needs-choice'` 時開啟選單）、`stopSpeech()`、`openVoiceMenu()`、`chooseVoice(choice)`、`previewVoice(key)` 暴露給樣板。純函式模組內不出現 `window`、`document`、`localStorage`、`console`。
本專案的 localStorage 慣例（記於 AGENTS.md 第 7 節的 Sidebar 條款）是讀寫須包 try/catch、失敗退回預設值，這裡照做。

### D7. 隱私頁
在 `privacy.html` 既有的 RPG 模型服務一節之後加一段（或該節最後一句）：朗讀使用瀏覽器內建語音、部分瀏覽器的線上語音可能把文字送到瀏覽器廠商、不經過本平台；並說明本平台只在玩家的瀏覽器記住所選語音的名稱、不記住任何朗讀內容。`tests/test_legal_pages.py` 加守門測試；既有的「不承諾未實作功能」測試不應被新段落觸發（段落不用「即將」「日後」這類詞）。

### D8. 語音選單 Modal
沿用專案現有的 `ui-modal`（歷史視窗用的同一個元件，`v-model` 綁 `modal.voice`、`title`、`status-code="VOICE_SELECT"`、`show-cancel="false"`），顯示狀態完全由 Vue 接管，不使用任何 `data-bs-*`。內容是 `role="radiogroup"` 加 `aria-label`：第一個選項「自動」，其後依 `voices` 排序的各語音（`role="radio"` 的原生 `<input type="radio">`，標籤含名稱、語言與「本機」或「線上」的小標），每個語音列旁一個「試聽」按鈕。選取即寫入選擇並關閉；`voices` 為空時顯示空狀態說明段落而不是選項。
無障礙：開啟時焦點落在目前選中的選項（`none` 時落在「自動」）；Esc 與遮罩點擊的行為沿用 `ui-modal`；關閉後把焦點還給選單按鈕（由 `game-session.js` 在關閉時遞增一個焦點計數，掛載層監看，與建議貼上後的 `focusTick` 同一種做法）。開啟與試聽時先停止劇情朗讀。選單按鈕本身 `aria-haspopup="dialog"`。
替代案：下拉式 `<select>`——否決：無法標示本機與線上、無法逐項試聽，且手機的原生選單樣式與專案風格不一致。

## Risks / Trade-offs

- [沒有中文語音（部分 Linux、極簡的瀏覽器）] → `no-voice` 停用並說明；不是錯誤，不打擾。
- [Chrome 對長文字在約 15 秒後中斷（線上語音）] → **任務 2.6 的結論（2026-11-24 查程式）**：劇情段每段最多 150 字（`story-pages.js` 的 `PAGE_MAX_CHARS`，單句超長時先於逗號處切、再硬切於字素邊界），但**玩家行動頁是整段原文、不分段，上限 1000 字**（後端 `game_service.ACTION_MAX` 驗證，頁面初始狀態的 `limits.action_max` 交給前端的 `actionProblem` 檢查；`session.html` 本身沒有 `maxlength` 屬性），所以行動頁可能遠超過 150 字，**需要切段**。已實作於 `story-speech.js`：文字超過 150 字（`SPEECH_CHUNK_MAX`，取自 `PAGE_MAX_CHARS`）時，以 `paginate(text, { done: true })` 依句界切成數段，每段各是一個 utterance，一次依序排進瀏覽器的朗讀佇列，全部由同一個代號管理（最後一段的 `end` 才算結束、任何一段 `error` 則丟掉佇列並回到可播放、`stop` 與 `destroy` 一併作廢）；劇情段本身不超過 150 字，所以一律是單一 utterance、文字與 `fullText` 完全相同。`story-speech.js` 因此在載入順序上依賴 `story-pages.js`（與 `story-reader.js` 相同的 UMD 依賴寫法）。**未驗證**：150 字的上限是否仍會被 Chrome 線上語音的約 15 秒限制中斷（中文語速約每秒 4 至 5 字，150 字可能超過 15 秒），沒有在真實瀏覽器量測；常數集中在 `SPEECH_CHUNK_MAX`，若瀏覽器實測（任務 4.2 的項目⑦）發現會被中斷，只需調低它。
- [行動建議、歷史與 `gm-sr` 與朗讀搶焦點或重複朗讀] → 朗讀不使用 `aria-live`，不改變焦點；螢幕閱讀器使用者可自行忽略此按鈕。
- [iOS Safari 的語音清單載入較慢、第一次必須由使用者手勢觸發] → 按鈕本身就是使用者手勢；清單為空視為 ready。
- [安全模式關閉時的露骨內容被大聲播出] → 完全由玩家按下才朗讀，與閱讀同樣是玩家自己的畫面；不另設限制。
- [`voiceschanged` 重複觸發造成畫面閃爍] → 只在 `availability` 或 `voices` 內容真的改變時才通知。
- [`voiceURI` 在不同瀏覽器或更新後可能改變] → 先比 `voiceURI`、再比 `name` 加語言；仍找不到就自動挑選並保留儲存值（語音可能只是暫時沒載入），不顯示錯誤。
- [Android 等環境語音清單恆為空，選單沒有東西可選] → 選單顯示空狀態說明（此瀏覽器沒有提供可選的語音，將使用瀏覽器預設），語音按鈕直接朗讀，不卡在「請先選語音」。
- [`localService` 欄位在部分瀏覽器不可靠（恆為真或缺席）] → 缺席時不標示「線上」，只在確定為假時才標示；自動挑選的「本機優先」在這類瀏覽器上等於沒有作用，不影響正確性。
- [玩家選了線上語音，文字會離開本機] → 這是玩家主動選的，且選單明確標示「線上」；隱私頁已說明。
- [儲存被禁用（隱私模式）使每次載入都視為從未選過，第一次按又開選單] → 可接受；選單選項與朗讀仍可用，只是不記住。
- [雙軌／migration 風險] → 無，沒有資料表或後端改動。

## Migration Plan

純前端與一段文字，沒有 migration、環境變數與資料。部署照一般流程；回復＝還原提交。靜態資源有版本字串機制時（若有）隨一般發版更新；驗收環境改 template 需重啟伺服器（模板有快取），JS 直接生效。

## Open Questions

- 隱私頁的文字更動是否要升 `CURRENT_TERMS_VERSION`？本 change 預設**不升**（只是說明瀏覽器行為，沒有新的蒐集或處理，也不需要使用者重新同意）；若之後法務覆核認為要升，不影響規格與任務拆分。
