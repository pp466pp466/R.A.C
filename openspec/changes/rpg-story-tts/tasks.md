# Tasks

## 1. 朗讀狀態機與語音選擇（純函式模組）

- [x] 1.1 新增 `app/static/js/game/story-speech.js`（UMD，不含 `window`、`document`、`localStorage`、`console`）：`createStorySpeech({ synth, Utterance, storage, lang, onChange })` 回傳 `speak`、`stop`、`toggle`、`preview`、`choose`、`destroy`、`state`（`availability`：`unsupported`｜`no-voice`｜`ready`，`speaking`、`voices`、`choice`）；以遞增代號作廢過期事件、`speak` 前先 `cancel()`、`stop()` 同步把 `speaking` 設為假。驗證：`node --test tests/js/story-speech.test.js` 的狀態機測試通過。
- [x] 1.2 語音清單與自動挑選：`voices` 只含中文語音（`key`＝`voiceURI`、`name`、`lang`、`local`，缺 `localService` 時視為本機且不標線上），排序為語言層級、本機優先、名稱；自動挑選 `zh-TW`〔含 `zh_TW`、不分大小寫〕→ `zh-Hant*` → 其他 `zh*`，同層級本機優先；可用性三態（清單為空視為 `ready`、非空且無中文語音為 `no-voice`、`voiceschanged` 重新評估且只在內容真的改變時通知、`destroy()` 移除監聽）。驗證：測試涵蓋「有 zh-TW 與 zh-CN」「只有 zh-CN」「無中文語音」「清單稍後才載入」「同層級本機優先」「缺 `localService`」。
- [x] 1.3 玩家的選擇：`choice` 三態（`none`｜`auto`｜`voice`）；儲存鍵 `rpg.speech.voice`，值 `{"mode":"auto"}` 或 `{"mode":"voice","voiceURI":"…","name":"…"}`；讀到壞掉的 JSON、未知 `mode` 或型別不對視為 `none`；寫入失敗仍以記憶體中的選擇為準；比對先 `voiceURI`、再 `name` 加語言、都找不到自動挑選且不改寫儲存；`choose(auto|voice)` 立即生效。`toggle` 在 `choice === 'none'` 且 `voices` 非空時不朗讀並回傳 `'needs-choice'`，`voices` 為空時直接朗讀。驗證：測試涵蓋「重新載入記得選擇」「voiceURI 變了但名稱相同」「選的語音不存在」「選了自動後直接朗讀」「儲存 getItem／setItem 丟例外」「儲存缺席」「壞 JSON」。
- [x] 1.4 試聽：`preview(key)` 以指定語音朗讀固定常數「你好，這是語音試聽。」，先作廢進行中的朗讀並 `cancel()`，不影響已儲存的選擇。驗證：測試確認試聽前劇情朗讀已停止、試聽的文字等於常數、不寫入儲存。
- [x] 1.5 新增 `tests/js/story-speech.test.js`（假的 `speechSynthesis`、`Utterance` 與儲存，可手動觸發 `end`、`error`、`voiceschanged`）：播放與停止切換、自然結束回到可播放、錯誤回到可播放且不拋出、過期事件被忽略、連續兩次 `speak` 只有最後一次有效、`synth` 或 `Utterance` 缺席為 `unsupported`、`synth.speak` 丟例外時回到可播放、`destroy()` 之後的呼叫與事件無作用、只寫入語音選擇鍵且內容不含任何朗讀文字、模組原始碼不含儲存全域物件與主控台（靜態檢查）。驗證：測試全數通過，且對模組做一次變異驗證（拿掉代號比對、拿掉 `cancel()`、拿掉 `name` 後備比對、把 `auto` 當成 `none`）每項至少有一個測試失敗。

## 2. 接進遊戲畫面

- [x] 2.1 `game-session.js`：以注入的 `speechSynthesis`、`SpeechSynthesisUtterance` 與儲存建立朗讀器，暴露響應式的 `speech`、`speakCurrent()`（朗讀 `card.fullText`；`card.note` 非空或內容為空時不動作；回傳 `'needs-choice'` 時開啟語音選單）、`stopSpeech()`、`openVoiceMenu()`、`chooseVoice(choice)`、`previewVoice(key)`。在 reader 的 `onChange` 比較「頁面身分」（`key`、`index`、`fullText`），改變就停止。驗證：`tests/js/game-story-session.test.js` 新增情境，翻段、回看、換輪、`discard`、`restore`、`load` 後朗讀都停止，且不自動翻頁、不自動接讀；第一次按先開選單、選過之後直接朗讀；開啟選單與試聽會先停止劇情朗讀。
- [x] 2.2 在送出行動、推進劇情、重送、放棄與 `game-revision.js` 開始修訂串流的入口各呼叫一次 `stopSpeech()`（修訂開始時劇情卡內容尚未改變，只靠身分比較會漏）。驗證：`tests/js/game-story-revision.test.js` 新增「朗讀中開始重新生成與編輯即停止」，`tests/js/game-story-session.test.js` 新增「朗讀中送出行動即停止」。
- [ ] 2.3 `game.js`：注入 `window.speechSynthesis`、`window.SpeechSynthesisUtterance`（存取丟例外視為不存在）與包好 try/catch 的 localStorage 存取物件，`pagehide` 與 Vue 卸載時 `destroy()`；選單關閉時遞增焦點計數並在掛載層把焦點還給選單按鈕。驗證：`tests/js/game.test.js` 以替身確認注入、`pagehide` 後呼叫過 `cancel()`、`localStorage` 存取丟例外與缺席時頁面照常掛載、關閉選單後焦點回到選單按鈕。
- [ ] 2.4 `session.html` 與 `game.css`：在 `gm-card__nav` 最左加語音按鈕（`v-if` 為 `speech.availability !== 'unsupported'`；`:disabled` 於 `card.note` 非空、`card.fullText` 為空或 `no-voice`；播放與停止兩個內嵌 SVG；`aria-pressed`、`aria-label`、`title`）與其右側較小的選單按鈕（`aria-label`「選擇語音」、`aria-haspopup="dialog"`、`no-voice` 時停用），沿用 `gm-pagebtn` 的尺寸與焦點樣式、不新增顏色；在 `story-reader.js` 之後、`game-session.js` 之前載入 `story-speech.js`。驗證：`tests/test_game_story_page.py` 斷言兩顆按鈕的屬性、沒有 `v-html` 與 `data-bs-*`、沒有新的外部資源；`tests/test_game_pages.py` 的載入順序更新並通過。
- [ ] 2.5 `session.html` 的語音選單：沿用 `ui-modal`（`v-model` 綁 `modal.voice`、`status-code="VOICE_SELECT"`、`show-cancel="false"`），內容為 `role="radiogroup"`：「自動」加各中文語音（名稱、語言、「本機」或「線上」小標、「試聽」按鈕），選取即寫入並關閉；`voices` 為空時顯示空狀態說明；開啟時焦點落在目前選項。全部為純文字插值，不使用 `v-html`。驗證：`tests/test_game_story_page.py` 斷言 Modal 的結構、標籤、`radiogroup`、空狀態與沒有 `v-html`、`data-bs-*`；`tests/js/game-story-session.test.js` 確認選取後 `choice` 改變且 Modal 關閉。
- [x] 2.6 確認長文字行為：以文字長度上限查出玩家行動頁與劇情段的最長長度；若可能超過約 150 字，評估是否需要朗讀前依句界切成多個 utterance（由同一個代號管理）；結論記入 `design.md` 的風險一節（不需要則寫明理由）。驗證：結論寫入 design.md，若有實作則 `story-speech.test.js` 補對應測試。

## 3. 隱私權政策與文件

- [ ] 3.1 `templates/legal/privacy.html` 在 RPG 模型服務一節之後補一段：朗讀使用瀏覽器內建語音、部分瀏覽器的線上語音可能把文字送到瀏覽器廠商、不經過本平台、也不適用 RPG 模型服務的說明；本平台只在你的瀏覽器記住你選的語音名稱，不記住任何朗讀內容；不使用「即將」「日後」等承諾字眼。`tests/test_legal_pages.py` 新增守門測試（兩個重點各一個斷言），並通過既有的「不承諾未實作功能」測試。驗證：`pytest tests/test_legal_pages.py` 通過；`CURRENT_TERMS_VERSION` 不變（design 的 Open Questions）。
- [ ] 3.2 更新 `docs/前端結構-frontend.md`：載入順序（頁首的 Mermaid 與第 3e-2 節的順序）、`story-speech.js` 的說明（含語音選單與 localStorage 鍵 `rpg.speech.voice`）與測試清單、頁首的「最後更新」與「對應 change」；依 `docs/README.md` 的對照表檢查是否有其他文件受影響。驗證：`pytest tests/test_design_docs.py` 通過。

## 4. 整合檢查

- [ ] 4.1 跑相關測試與全域守門：`node --test tests/js/story-speech.test.js tests/js/story-reader.test.js tests/js/game-story-session.test.js tests/js/game-story-revision.test.js tests/js/game.test.js`，加 `pytest tests/test_game_pages.py tests/test_game_story_page.py tests/test_legal_pages.py tests/test_design_docs.py tests/test_agents_skill_sync.py`。驗證：全數通過；全套 pytest 留給提交前最後執行一次。
- [ ] 4.2 瀏覽器實測（使用者親自或 Claude 以腳本輔助，結果記入 README 的驗收紀錄）：①第一次按語音按鈕先開選單，選語音後再按即朗讀、再按停止；②選單列出中文語音並正確標示本機與線上，試聽用所選語音、不讀劇情；③選過語音後重新整理，仍記得選擇；選「自動」後不再開選單；④打字中按下即朗讀整段；⑤翻段、回看、重新生成、編輯、離開頁面時聲音立即停止，且不自動接讀；⑥「你」的行動頁可朗讀，被擋回合的轉場說明不可朗讀；⑦Windows 的 Chrome 與 Edge 各驗一次，記下實際列出的語音與本機或線上標示是否正確，並以線上語音朗讀一段接近 150 字的劇情，確認不會在中途被中斷（會的話調低 `SPEECH_CHUNK_MAX`，見 design 風險一節）；⑧停用 localStorage（或隱私模式）時功能照常；⑨沒有中文語音的環境驗證 `no-voice` 的停用與說明。驗證：項目逐一記錄結果。
- [ ] 4.3 依 `deck-conflict-watch` 技能檢查是否與 `docs/2.0簡報.md` 及六張圖解衝突（預期沒有：只改前端細節與一段隱私文字）；有衝突只通知使用者，不自行修改。驗證：檢查結果寫入提交說明。

## 批 1 完成註記（2026-11-24）

- **範圍**：2.6（先做）加 1.1–1.5。2.6 的結論寫在 design.md 風險一節：劇情段至多 150 字，但玩家行動頁是整段原文、上限 1000 字，所以**需要切段**，已併入 `story-speech.js`（超過 `SPEECH_CHUNK_MAX`＝150 字時以 `paginate` 依句界切成數個 utterance，依序排進佇列，由同一個代號管理）；劇情段仍是單一 utterance。因此 `story-speech.js` 在載入順序上依賴 `story-pages.js`（批 3 的 2.4 已規定它在 `story-reader.js` 之後載入，順序自然滿足）。
- **與任務文字的出入（已寫進 design D1、D2）**：`state` 多一個 `selectedKey`（選單標示目前選項用）；`toggle` 的回傳值定為 `'started'｜'stopped'｜'needs-choice'｜'unavailable'`；`choose` 的參數為 `{ mode: 'auto' }` 或 `{ mode: 'voice', key }`；`choose` 會停止進行中的朗讀；`speak` 前重讀語音清單。對外名稱加 `SPEECH_` 前綴（`Game.SPEECH_STORAGE_KEY`、`Game.SPEECH_PREVIEW_TEXT`、`Game.SPEECH_CHUNK_MAX`），避免與 `Game` 命名空間既有名稱衝突。
- **驗證**：`node --test tests/js/story-speech.test.js` **56 項全過**；變異驗證在 scratchpad 副本上做（每個變異有 `--test-timeout` 與行程逾時，專案檔案雜湊前後相同、副本已還原）：**61 個變異，60 個被殺死**（含任務指定的四項：拿掉代號比對、拿掉 `cancel()`、拿掉名稱後備比對、把 `auto` 當成 `none`）；第一輪 5 個存活，其中 3 個是測試缺口（重複 `end`、`stop()` 之後的舊 `error`、空 `voiceURI` 的比對）已補測試後殺死，1 個是多餘的檢查（`preview` 的 availability 判斷，`no-voice` 時清單本來就空）已把程式碼簡化掉，**剩 1 個等價變異**（`isObject` 不擋陣列：三處呼叫對陣列的結果都一樣）。全域守門子集 `tests/architecture`、`test_service_errors`、`test_design_docs`、`test_readme`、`test_restricted_session`、`test_legal_pages`、`test_agents_skill_sync`：1100 通過、2 略過、**1 失敗**——`test_the_roadmap_lists_every_archived_and_active_change`：`docs/路線圖-roadmap.md` 沒有列出 `rpg-story-tts`（立案時沒補，**不是本批造成**；與先前 `rpg-story-view` 同一類）。**未跑全套**（批 1 只新增前端模組與測試，沒動 `app/` 後端）；**未驗證**：真實瀏覽器的語音清單、`localService` 實際值、150 字是否被線上語音的約 15 秒限制中斷（任務 4.2）。

## 批 2 完成註記（2026-11-24；只跑相關測試加全域守門子集與 Node 全套，**未跑全套 pytest**）

- **範圍**：2.1、2.2 完成；**2.3 只做了一半，所以沒有勾**：注入 `speechSynthesis`、`SpeechSynthesisUtterance`（存取丟例外視為不存在）與 try/catch 的 localStorage 存取物件、`pagehide` 與離開確認時銷毀朗讀器、`modal.voice` 已完成；**選單關閉後把焦點還給選單按鈕（`voiceMenuButton` 的元素參照與監看）與 `setup()` 回傳新名稱，隨 2.4 在批 3 一起做**。原因：`tests/js/game-template.test.js` 是樣板與 `setup()` 回傳名稱的**雙向**契約（回傳了樣板沒用到的名稱就失敗），而元素參照要綁在樣板上才有意義。
- **做法**：`game-session.js` 建立 `Game.createStorySpeech`（依賴由 `deps.speechSynthesis`、`deps.SpeechSynthesisUtterance`、`deps.speechStorage` 注入，缺席時 `speech.availability` 永遠是 `unsupported`），回傳 `speech`（響應式的狀態）、`voiceMenuOpen`、`speakCurrent()`、`stopSpeech()`、`openVoiceMenu()`、`closeVoiceMenu()`、`chooseVoice(selection)`、`previewVoice(key)`；**停止有兩條路徑**：①身分路徑：閱讀器 `onChange` 比較 `key:index:fullText`，改變就停止；②明確路徑：`runTurn`（送出行動、推進劇情與重送都經過它，在重複點擊的防護之後，所以被拒絕的點擊不會停止朗讀）、`abandonInflight`、`game-revision.js` 的 `start()`（經 `host.stopSpeech`）。`dispose()` 呼叫 `destroy()`。`openVoiceMenu()` 在 `unsupported` 與 `no-voice` 時不開。
- **與 design／tasks 文字的小出入（兩處）**：①語音選單的開關 `voiceMenuOpen` 放在 `game-session.js`（`speakCurrent` 回 `'needs-choice'` 時要開選單，所以由它持有），`game.js` 的 `modal.voice` 與它是**同一個 ref**（`Vue.reactive` 會解開 ref），並納入 `anyModalOpen()`（有選單時鍵盤翻頁與 Esc 不作用）；②不改 `story-speech.js`。
- **有意識改變的既有測試契約（不是掩蓋缺陷）**：`tests/test_game_pages.py::test_the_entry_page_keeps_nothing_in_the_browser_and_never_writes` 原本是「`game/` 目錄任何腳本都不得出現 `localStorage`」，現在改為「只有 `game.js` 可以，且只在 `speechStorage` 這個存取物件（讀與寫各一處，都包 try/catch）」；`sessionStorage` 與 cookie 仍整個目錄不得出現，IndexedDB 仍只在 `game-store.js`。`tests/js/game.test.js` 的「沒有任何瀏覽器儲存被寫」斷言仍成立（還沒選語音就沒有寫入）。
- **測試**：新增 `tests/js/support/fake-speech.js`（假的語音合成、發音物件、語音與儲存）；`game-harness.js` 加 `speech` 選項、`game-sandbox.js` 載入 `story-speech.js` 並加 `speech`、`speechThrows`、`localStorageImpl` 選項。新增情境：`game-story-session.test.js` 14 項（朗讀整段與再按停止且不翻頁、自然結束不自動接讀、翻段與回看與載入新內容即停止、內容不變就不停止、說明行與沒有內容不可朗讀、第一次按先開選單與選過之後直接朗讀、不合格的選擇被拒、開選單與試聽先停止劇情朗讀、不支援與 `no-voice`、送出與推進即停止而被拒絕的點擊不停止、重送與放棄即停止、離開頁面銷毀、只寫語音選擇不寫劇情文字與不進主控台）、`game-story-revision.test.js` 3 項（重新生成與編輯開始即停止、被拒絕的修訂不停止）、`game.test.js` 6 項（注入與不自行朗讀、`pagehide` 後呼叫 `cancel()` 並移除監聽、離開確認也銷毀、缺席或存取丟例外或 localStorage 被擋時照常掛載、`modal.voice` 與 Esc、載入不寫儲存）。
- **驗證**：Node 全套 **1951 通過、0 失敗**；pytest `tests/test_game_pages.py` 109 通過；全域守門子集加 `test_game_story_page.py` 1217 通過、2 略過（既有）、**0 失敗**（`test_design_docs` 的路線圖守門原本因沒列 `rpg-story-tts` 而失敗，已在 `docs/路線圖-roadmap.md` 補一個節點與一列）；`openspec validate rpg-story-tts --strict` 通過。
- **簡報衝突**：檢查過，簡報沒有寫朗讀、`game/*.js` 的細節或 localStorage 規則，沒有衝突；`docs/2.0簡報.md` 的 Node 案例數與 JS 模組數是既有的數字漂移。
- **未驗證**：樣板與樣式（批 3 的 2.4、2.5）；真實瀏覽器的語音；全套 pytest（最後一批）。

## Workflow follow-up

- 封存前確認設計文件已隨這個 change 更新；封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各設計文件標頭的「對應 change」。
- 更新 `HANDOFF.md`（不進 commit）。
