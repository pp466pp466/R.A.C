# Tasks

## 1. 轉接器

- [ ] 1.1 把 `HttpTransport`、`UrllibTransport` 與 JSON 解析從 `app/llm/gemini.py` 抽到 `app/llm/transport.py`，`gemini.py` 改為匯入（行為不變）。驗證：`pytest tests/test_gemini_provider.py tests/test_llm_service.py tests/architecture` 與抽出前的結果完全相同
- [ ] 1.2 新增 `app/llm/openai_compatible.py` 的請求端：`build_body`（系統指令、角色對應、輸出上限）、基底網址加 `/chat/completions`、`Authorization: Bearer` 標頭、`__repr__` 遮罩金鑰。驗證：`tests/test_openai_compatible_provider.py` 以假傳輸檢查請求本文、網址與標頭，金鑰不在網址與字串表示中
- [ ] 1.3 回應端：用量（token 為空值而非 0）、結束原因正規化、內容過濾 → 輸出被阻擋；失敗對應表（429、408、5xx、逾時、連線失敗 → 暫時性；400、401、402、403、404 → 設定錯誤；本文含 `error` 物件或沒有結果的處理），不保留供應商本文。驗證：同一測試檔逐項涵蓋，並斷言錯誤物件與日誌中找不到回應本文與金鑰
- [ ] 1.3a 結構化輸出的退化：預設在系統指令末端附結構描述與只輸出 JSON 的要求且不送 `response_format`；`OPENROUTER_JSON_SCHEMA` 開啟才送結構描述參數。驗證：兩種模式的請求本文各一個測試，且輸出仍由 `parse_llm_json` 驗證
- [ ] 1.4 資料保存偏好：開啟時每個請求（含備援模型）帶零保存偏好；供應商因沒有符合的端點而拒絕時歸為設定錯誤，且傳輸只被呼叫一次（不重送不帶偏好的請求）。驗證：`tests/test_openai_compatible_provider.py` 涵蓋有偏好、備援模型也帶、被拒絕時沒有第二次請求
- [ ] 1.5 免費與付費只差設定：同一個轉接器與呼叫流程不依模型名稱有無免費標記而不同。驗證：以帶與不帶 `:free` 的名稱各呼叫一次，請求除模型名稱外完全相同

## 2. 設定

- [ ] 2.1 `app/config.py`：新增 `OpenAICompatibleSettings`（金鑰、主要與備援模型、基底網址、零保存、是否支援結構描述），模型名稱規則依供應商（OpenAI 相容端點允許 `/` 與 `:`、最長 100；Gemini 不變），設定要嘛完整要嘛沒有，備援未設定時等於主要模型，基底網址只允許 https（development 與 testing 另允許回送位址的 http，不得含帳號、密碼與查詢字串）。`OPENROUTER_API_KEY` 不加入必要機密。驗證：`tests/test_config.py` 涵蓋 spec 的每個情境，所有錯誤訊息只含變數名稱
- [ ] 2.2 `app/config.py`：新增 `ContentSettings`（`SEXUAL_CONTENT_LEVEL3_ENABLED`、`SEXUAL_CONTENT_LEVEL3_MODELS`）與啟動驗證：值必須是布林；staging 與 production 開啟即失敗；開啟時 RPG 用途必須已設定、清單非空、主要與備援模型都在清單內。驗證：`tests/test_config.py` 涵蓋每個情境（含預設關閉、開發環境可開、清單缺一個模型）
- [ ] 2.3 `.env.example` 與 README 的環境變數表新增八個名稱（不含實際值）。驗證：`pytest tests/test_env_example.py tests/test_readme.py` 通過

## 3. 組裝與內容上限

- [ ] 3.1 `app/__init__.py`、`app/runtime.py`、`app/services/registry.py`：以用途存放供應商（客服與摘要不變），`create_app` 新增注入參數 `rpg_llm_provider`，新增 `get_rpg_llm_service()`（未設定為 None）。驗證：`tests/test_llm_providers_by_purpose.py`：只設定 Gemini 時客服與摘要照常、RPG 用途為 None；兩把金鑰互不混用；RPG 用途的呼叫在帳本記錄 OpenAI 相容端點的供應商名稱與實際模型；RPG 供應商持續失敗不影響客服的備援與斷路
- [ ] 3.2 `app/services/access_policy.py`：建構子新增 `adult_ceiling`（預設 2，既有行為不變），`content_ceiling()` 依它回傳，`registry` 依設定傳入 2 或 3；更新檔頭與常數的說明。驗證：`tests/test_access_policy.py` 新增年齡（15、25、沒有生日）乘開關（關、開）乘管理員的組合，未成年與沒有生日永遠是 0，既有測試不改即通過
- [ ] 3.3 確認架構規則仍成立：業務程式碼（services、controllers 等）不含供應商網域，供應商網域只在 `app/llm/`。驗證：`pytest tests/architecture` 通過

## 4. 探測指令

- [ ] 4.1 `app/cli.py` 新增 `flask llm-probe`：只在 development 與 testing 執行，呼叫 RPG 用途的供應商一次，印出回覆、模型、延遲、token 與結束原因；`--structured` 以固定結構描述呼叫並回報是否通過驗證與原因分類；不寫帳本與資料庫、不顯示金鑰、不印供應商錯誤本文。驗證：`tests/test_cli_llm_probe.py`（注入假供應商）涵蓋 spec 的五個情境，並斷言資料表筆數不變、輸出不含金鑰

## 5. 驗收與文件

- [ ] 5.1 人工驗收（**每次真實呼叫都需使用者當下同意**，只用無害的合成文字）：以 `flask llm-probe` 對目前的免費端點呼叫一般文字與 `--structured` 各數次，記錄是否可用、延遲、繁體中文品質、JSON 穩定度、429 的表現；另確認 spec 的兩個待驗證的事實（內容審核的回應碼、零保存偏好的欄位名稱與免費端點的反應）。驗證：結果記入 README，需要時據此補一個 spec 情境
- [ ] 5.2 全部測試與規格驗證通過。驗證：`pytest`（含 `-m sqlserver`）、`node --test "tests/js/**/*.test.js"`、`openspec validate openai-compatible-provider --strict`
- [ ] 5.3 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`模型呼叫層-llm-layer.md`（第二個轉接器、依用途指定供應商、結構化輸出的退化、零保存偏好、環境變數）、`帳號與安全-auth-security.md`（內容上限的圖與規則改為依開關，第 5 節）、`營運與部署-operations.md`（新環境變數、`flask llm-probe`、staging 與 production 的限制）、`系統架構-architecture.md`（`create_app` 新增的注入參數 `rpg_llm_provider`）、`路線圖-roadmap.md`（這個 change 的狀態，並把「自家審查層」與「成年驗證」記為任何露骨內容入口出現之前的前置條件），README 的環境變數表與驗收紀錄；並讓 `tests/test_design_docs.py` 通過

## Workflow follow-up

- 取得「許可涵蓋的供應商、模型與日期」後，寫入 `docs/帳號與安全-auth-security.md` 並填入 `SEXUAL_CONTENT_LEVEL3_MODELS`（程式不依賴這些文字，不阻擋實作）。
- 實作完成並由使用者檢視後，再執行封存。
