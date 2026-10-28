# 內容政策與合規研究：供應商條款、台灣法規與待法務確認事項

- **文件版本**：v1.0
- **日期**：2026-10-28
- **性質**：研究整理，**不是法律意見**。與 [RPG.md](RPG.md) 的 R-15（內容分級與安全）、R-16（外部資料與 MCP）配套；設計決定記在 RPG.md，本文件只收納**依據**。
- **讀者**：專案負責人，以及之後要審閱本文件的法務或律師。
- **揭露**：本文件由 Anthropic 的模型（Claude）整理。表中也包含 Anthropic 自己的條款，查證與呈現方式與其他供應商相同。

---

## 第 1 章　如何閱讀本文件

| 項目 | 說明 |
|---|---|
| 取得日期 | 全部於 2026-10-28 取得。條款會變動，**使用前請重讀原文** |
| 取得方式 | 以網頁讀取工具取得。該工具會先摘要網頁再回傳，所以英文引文**可能有細微出入**，一律以各節連結的原文為準 |
| 未能取得原文 | **OpenAI 使用政策**（403）、**xAI 可接受使用政策**（403）、**Google Cloud 可接受使用政策**（頁面被截斷）。這三項只有二手資料，已在文中標示，不得當成原文引用 |
| 排序 | 依字母順序，**不依寬鬆程度**。本文件不替任何一方挑選「最不受限」的供應商 |
| 「沒提」不等於「允許」 | 條款未明文禁止，只代表沒有明文規定。請法務不要用「沒禁止就是可以」來解讀本文件 |
| 範圍 | 只涵蓋文字內容（本專案是純文字 RPG）。圖片、影片與語音的規定未研究 |

**已確認的內容範圍**（出自 RPG.md R-15.1）：保留黑暗內容（非性）、成熟內容、成年角色之間合意的露骨內容（預設關閉、玩家主動啟用、須通過年齡確認）；排除涉及未成年的性內容、性暴力與非自願的性情節、以真人名義的性內容。

---

## 第 2 章　Google（Gemini 與 Gemma）

本專案的規範（AGENTS.md 第 5 節）要求全系統一律使用 Gemini，因此本章最重要。

### 2.1　生成式 AI 禁止使用政策

原文：[policies.google.com/terms/generative-ai/use-policy](https://policies.google.com/terms/generative-ai/use-policy)　版本：Last Modified **2024-12-17**

與本專案相關的條文（英文引自該頁，經工具摘要）：

| 條號 | 條文 |
|---|---|
| 1.1 | "Relates to child sexual abuse or exploitation." |
| 1.2 | "Facilitates violent extremism or terrorism." |
| 1.3 | "Facilitates non-consensual intimate imagery." |
| 1.4 | "Facilitates self-harm." |
| 2.3 | "Circumvention of abuse protections or safety filters -- for example, manipulating the model to contravene our policies." |
| 3.1 | "Hatred or hate speech." |
| 3.2 | "Harassment, bullying, intimidation, abuse, or the insulting of others." |
| 3.3 | "Violence or the incitement of violence." |
| 3.4 | "Sexually explicit content -- for example, content created for the purpose of pornography or sexual gratification." |

第 1 節與第 3 節的引言為「generating or distributing content that facilitates」，也就是第 3 節各項前面都有「facilitates」。

**例外條款**：「We may make exceptions to these policies based on educational, documentary, scientific, or artistic considerations」，工具回報該句後面接著「or where harms are outweighed by substantial benefits to the public」。這是 **Google 可以裁量**的例外，不是使用者的權利。

**政策沒有的東西**：

- 沒有任何關於**虛構、諷刺或角色扮演**的句子。
- 沒有寫明違規的處置方式（停用、終止等）。

**解讀注意事項**（以下是整理者的讀法，**Google 並未說明**，請以書面詢問為準）：

- 第 3.3 項寫的是「facilitates violence」，字面上，小說裡描寫戰鬥與復仇比較像「描寫」而不是「促成」；但政策沒有明文排除虛構。
- 第 3.2 項可能涵蓋反派 NPC 對玩家的威脅與辱罵；第 3.1 項可能涵蓋角色說出的歧視言論。
- 第 3.4 項**沒有任何關於角色年齡的限定**。「成年角色」不會讓這一項失效。
- 第 2.3 項表示：用提示詞技巧去規避過濾，本身就是違規。

### 2.2　Gemini API 附加條款

原文：[ai.google.dev/gemini-api/terms](https://ai.google.dev/gemini-api/terms)　版本：Effective **2026-03-23**，Last updated 2026-04-28

| 主題 | 條文（英文引自該頁，經工具摘要） |
|---|---|
| **年齡** | "You must be 18 years of age or older to use the APIs." |
| **面向未成年的應用** | "You also will not use the Services as part of a website, application, or other service (collectively, "API Clients") that is directed towards or is likely to be accessed by individuals under the age of 18." |
| **安全設定** | "You are responsible for determining the necessary and appropriate safety settings and factuality tools for your use case." 緊接著："Applications with less restrictive safety settings may be subject to Google's review and approval." |
| **不得繞過** | 條款說明安全功能會擋有害內容，且不得嘗試繞過 |
| **禁止使用政策** | "you must comply with our Prohibited Use Policy"（即 2.1） |
| **資料：免費（Unpaid）服務** | Google 使用你提交的內容與回應來提供與改進服務；"human reviewers may read, annotate, and process your API input and output." |
| **資料：付費（Paid）服務** | Google 不使用提示與回應來改進產品；提示與回應仍會被記錄，"solely for detecting and preventing violations of the Prohibited Use Policy"（僅用於偵測與防止違反禁止使用政策） |
| **成人／露骨內容** | 附加條款本身**沒有**針對成人或露骨內容的具體規定，而是引用 2.1 |
| **違規處置** | 工具回報該頁未載明停用、終止或審查的具體後果 |

### 2.3　安全設定說明

原文：[ai.google.dev/gemini-api/docs/safety-settings](https://ai.google.dev/gemini-api/docs/safety-settings)　版本：Last updated **2026-10-15**

| 類別 | 定義（英文引自該頁，經工具摘要） |
|---|---|
| Harassment | "Abusive, threatening, or content intended to bully, torment, or ridicule." |
| Hate speech | "Content that promotes violence or incites hatred against individuals or groups based on certain attributes." |
| Sexually explicit | "Content that contains sexually explicit material." |
| Dangerous | "Content that promotes, facilitates, or enables dangerous activities." |
| Jailbreak | "Prompts designed to bypass safety filters." |

其他要點：

- 該頁以**影片遊戲對話**為例，說明你「may deem it acceptable to allow more content that's rated as」危險內容。Google 自己承認遊戲對話是調整門檻的正當場景（可引用於詢問信）。
- 過濾依**機率**而不是嚴重程度：「It blocks content based on that probability, not on the severity of the harm.」
- 有些類別永遠被擋且不可調整："These types of harm are always blocked and cannot be adjusted."，該頁的例子是危害兒童安全的內容。
- 該頁沒有提到「公共議題誠信」（civic integrity）類別（舊版說明有，新版說明有出入）。
- **Vertex AI 的說明**另指出，`BLOCK_NONE` 是**受限欄位，不是所有使用者都能用**（[說明](https://docs.cloud.google.com/vertex-ai/docs/generative-ai/multimodal/configure-safety-attributes)）。這是 Vertex 文件，本專案用的是 Gemini API 金鑰，細節可能不同。
- **二手說法，尚未由官方證實**：第三方說明指出，兒童安全等內建保護位於可調過濾器之下，無法關閉，命中時會顯示成結束原因 `PROHIBITED_CONTENT`（[說明](https://neurals.ca/tech/gemini/safety-settings/)）；開發者論壇有回報「已設定 BLOCK_NONE 仍被擋」（[討論](https://discuss.ai.google.dev/t/getting-blocked-by-safety-settings-on-un-reports/39224)）。

### 2.4　Gemma 禁止使用政策（開放權重模型）

原文：[ai.google.dev/gemma/prohibited_use_policy](https://ai.google.dev/gemma/prohibited_use_policy)　版本：Last modified 2024-02-21；頁面註明該文件已棄用並指向 gemma.ai，因此**可能已被取代**

- 禁止："Generate sexually explicit content, including content created for the purposes of pornography or sexual gratification"（舉例包含性聊天機器人）。
- 例外："Note that this does not include content created for scientific, educational, documentary, or artistic purposes."（這是明文例外，與 2.1 的裁量性例外不同。）
- 未成年：「Promoting or generating content related to child sexual abuse or exploitation」。
- 工具回報該頁沒有處置方式。

### 2.5　濫用監控與處置

原文：[ai.google.dev/gemini-api/docs/usage-policies](https://ai.google.dev/gemini-api/docs/usage-policies)（Gemini API 條款頁指向的「Abuse monitoring」頁面）。以下摘要來自工具回報，**請以原文為準**：

| 主題 | 內容 |
|---|---|
| 自動偵測 | 系統掃描使用情形是否違規，涵蓋「hate speech, harassment, sexually explicit content, and dangerous content」 |
| 資料保存 | Google 保存提示、上下文與輸出 **55 天**，用於偵測與防止違規 |
| 人工審查 | 持續出現可疑活動的專案「may be flagged for manual review by authorized Google personnel」 |
| 主動聯絡 | Google 可能來信，討論你的使用情境並協助符合規範 |
| 暫時性限制 | 可能調整速率限制或改變回應請求的模型；可能「temporarily pause your access to the Gemini API」 |
| 帳號關閉 | 嚴重違規可能「permanently close your access to the Gemini API **and other Google services**」，也就是不只 Gemini，可能波及其他 Google 服務 |
| 申訴 | 該頁沒有列出申訴管道；只說若 Google 就暫停或關閉與你聯絡，會附上申訴連結（"we will also provide a link where you can appeal"） |

**沒有官方的政策審核信箱**：Gemini API 附加條款、上述濫用監控頁，以及搜尋結果中，都沒有列出任何可以詢問政策、申請安全設定審核或申訴的電子信箱或表單。條款只說「Applications with less restrictive safety settings may be subject to Google's review and approval」，但沒有說明如何申請。

### 2.6　對本專案的影響

1. **年齡閘門是整個專案的合規問題**（2.2 的年齡條款），與是否提供成熟內容無關。客服與 RPG 都受影響；現行註冊流程沒有年齡確認。
2. **調鬆安全設定本身可能被 Google 審查**（2.2），連黑暗題材也一樣。
3. **露骨內容是否可行，取決於 Google 對藝術例外的裁量**（2.1），且該例外的後面還有「substantial benefits to the public」一項。本文件無法估計機率。
4. **免費層不得用於敏感內容**：免費服務的輸入與輸出可能被人工審閱（2.2）。
5. **不得用提示詞技巧規避過濾**（2.1 的 2.3 項、2.2）。

---

## 第 3 章　其他模型供應商（依字母順序）

| 供應商（文件） | 對成人性內容的明文 | 例外或年齡機制 | 版本日期 | 原文狀態 |
|---|---|---|---|---|
| [Anthropic 使用政策](https://www.anthropic.com/legal/aup) | 列為禁止：描繪性交與性行為、與性癖好或性幻想相關的內容、亂倫與獸交、「Engage in erotic chats」 | 該節未見創作例外。未成年條款明寫「包含虛構情境與角色扮演」也不行 | 頁面標示生效日 **2026-11-12**，晚於本文件日期，**現行版本請查版本歷史** | 已讀原文 |
| [Cohere 使用政策](https://cohere.com/usage-policy) | 沒有一般性的成人內容條款，只規範涉及未成年的性露骨內容 | 應用若會接觸未成年，須有年齡驗證與內容審核 | Last updated 2024-11-21 | 已讀原文 |
| [Meta Llama 4 可接受使用政策](https://dev.meta.ai/llama/llama4/use-policy/) | 沒有成人性內容條款。列有「Sexual solicitation」、「Human trafficking, exploitation, and sexual violence」、「The illegal distribution of information or materials to minors, including obscene materials」 | 提到「failure to employ legally required age-gating」 | 頁面未標日期 | 已讀原文 |
| [Mistral 使用政策](https://legal.mistral.ai/terms/usage-policy) | 沒有一般性的成人內容條款。禁止「generate intimate images of any person」（列在未經同意的私密影像下），對 CSAM 零容忍 | 無 | Effective 2026-06-11 | 已讀原文 |
| [OpenAI 使用政策](https://openai.com/policies/usage-policies/) | **原文讀不到。**二手資料：禁止「性暴力與未經同意的私密內容」，對未成年有多項禁止 | 2025-10 宣布將對已驗證成人開放情色內容；[2026-03 報導延後](https://techcrunch.com/2026/03/07/openai-delays-chatgpts-adult-mode-again/)；之後有報導稱已暫停，**沒有官方確認** | 不明（另有一份標示 Effective 2025-10-29 的[PDF 副本](https://docs.databricks.com/aws/ja/assets/files/usage-policies-openai-923e062e72487537e2a8df04bcec7f6d.pdf)，可能與現行頁面不同） | **未取得原文** |
| [xAI 可接受使用政策](https://x.ai/legal/acceptable-use-policy) | **原文讀不到。**二手資料：禁止以真人形象製作色情內容、禁止性化兒童。API 是否允許成人露骨文字，**無法確認** | 不明 | 不明 | **未取得原文** |

補充：

- **處置方式**：Anthropic 與 Mistral 明寫可能警告、限流、暫停或終止帳號；Cohere 明寫可限制、暫停或終止。Anthropic 與 Mistral 對 CSAM 另有通報執法機關的規定。Meta 與 Gemma 的頁面沒有寫處置。
- **OpenAI**：有社群貼文主張現行使用政策沒有全面禁止成年人之間合意的虛構性內容（[討論](https://community.openai.com/t/you-said-you-wanted-it-you-now-have-age-verification-you-now-have-a-usage-policy-that-allows-it-so-when-can-verified-adults-create-adult-fiction/1399668)），這只是社群的主張，不是官方陳述。
- **xAI**：有網頁聲稱 API 預設「沒有成人內容限制」，來源是外洩的系統提示詞（[頁面](https://www.mintlify.com/asgeirtj/system_prompts_leaks/xai/grok-api)），**不是官方政策，請勿採信**。另有[報導](https://www.bloomberg.com/news/articles/2026-01-02/grok-posts-sexual-images-of-minors-after-lapses-in-safeguards)指出 2026-01 該公司的服務曾出現涉及未成年的安全疏漏。
- 開放權重模型的**授權條款**（例如 Apache 2.0 類）通常沒有用途限制，但每個模型都要個別確認，本文件未逐一研究。
- **Mistral 補充**（2026-10-28 取得）：
  - 資料與訓練：[help center](https://help.mistral.ai/en/articles/347617-do-you-use-my-user-data-to-train-your-artificial-intelligence-models) 說明 Mistral Studio 的 **Free mode** 可能使用輸入與輸出訓練模型（可隨時退出）；Pay-as-you-go 客戶可自行控制並退出。保存期限與人工審閱該頁**未載明**。
  - 內容審核：[Guardrailing 文件](https://docs.mistral.ai/capabilities/guardrailing/)說明審核是**可選的獨立服務**（`mistral-moderation-2603`，共 14 類，含 Sexual），自訂護欄只做輸入審核、觸發時回 403；該頁**沒有說明模型預設是否過濾**，並寫「不同產業可能需要不同程度的護欄」，也就是審核強度由開發者決定。
  - 模型別名：`mistral-small-latest` 是別名。[模型總覽](https://docs.mistral.ai/getting-started/models/models_overview/)列出 Mistral Small 4（v26.03，Apache 2.0），但**沒有寫明別名目前指向哪個版本**；同頁的 Small 3.2 已公告 2026-04-30 棄用、2026-07-31 停用。
  - 速率限制：官方文件我讀不到。以下是**實測這個帳號（免費 mode）的回應標頭**：`ministral-3b-2512` 每分鐘 750 次、`ministral-8b-2512` 188 次、`codestral-2508` 125 次、`ministral-14b-2512` 30 次，每分鐘 token 上限 62.5 萬至 93.75 萬；`mistral-small-2603`（即 `mistral-small-latest`）與 `mistral-medium-latest` 的上限是 **0**，無法呼叫。第三方整理的「每秒 1 次」與此不符，請以帳號後台的 Limits 頁面為準。

---

## 第 4 章　基礎設施

| 供應商 | 結果 |
|---|---|
| Google Cloud（[條款](https://cloud.google.com/terms/aup)） | **原文讀不到**（頁面被截斷）。早先搜尋節錄指出：禁止非自願的露骨影像與兒童性剝削，沒有看到針對合法成人內容的明文；無法確認是否為現行版本（[2025-10-13 版](https://cloud.google.com/archive/terms/aup-20251013)、[2025-07-01 版](https://cloud.google.com/archive/terms/aup-20250701)）。**本專案的部署平台是 Google Cloud（AGENTS.md 第 9 節），必須讀完整原文** |
| AWS（[條款](https://aws.amazon.com/aup/)） | 已讀。合法成人內容**未提及**；禁止促進兒童性剝削；可「remove or disable access to any content or resource that violates this Policy」。頁面日期 2021-07-01。AWS 不是本專案的部署平台，僅供對照 |

---

## 第 5 章　台灣法規

資料庫截止日：民國 115 年 10 月 16 日（2026-10-16）。以下**不是窮盡的清單**。

| 法規 | 內容 | 取得狀態 |
|---|---|---|
| [刑法第 235 條](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=C0000001&flno=235) | 第 1 項：「散布、播送或販賣猥褻之**文字**、圖畫、聲音、影像或其他物品，或公然陳列，或以他法供人觀覽、聽聞者，處二年以下有期徒刑、拘役或科或併科九萬元以下罰金。」第 2 項：意圖散布、播送、販賣而製造、持有者亦同。第 3 項：沒收。**條文明寫「文字」**，純文字小說不會自動排除 | 已讀原文（逐字） |
| 釋字第 617 號 | 二手摘要（[來源](https://www.legis-pedia.com/dictionary/1000376)）：認定第 235 條不違憲，但限縮範圍。含暴力、性虐待或人獸性交等、且無藝術、醫學、教育價值的內容（硬蕊），原則上不得散布；其他客觀上足以刺激性慾、一般人難以接受的物品（軟蕊），在未採取適當**隔絕措施**時傳布才受規範。其判斷標準承襲釋字第 407 號 | **二手摘要**，須法務確認 |
| [兒童及少年福利與權益保障法第 46 條](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=D0050001&flno=46) | 摘要（工具未提供逐字）：網路平台業者須依內容防護機構或業者公會的自律規範採取明確可行的保護措施；接獲通知有害兒少內容、或未採取適當措施時，須限制兒少接觸或移除 | 摘要 |
| [個人資料保護法第 6 條](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=6) | 摘要（工具未提供逐字）：病歷、醫療、基因、**性生活**、健康檢查、犯罪前科等個資原則上不得蒐集、處理或利用，有書面同意等例外。頁面註明 2025-11-11 的修正**尚未生效**，**請法務對照現行有效版本與修正版** | 摘要 |

**未能查證**：

- 兒童及少年性剝削防制條例對純文字與虛構內容的適用。
- 網路內容分級的具體規定與主管機關指引。
- 玩家若來自台灣以外，英國、歐盟、美國部分州的年齡驗證與線上安全法是否適用。

---

## 第 6 章　待法務確認的問題

1. AI 即時生成、且只有單一使用者看得到的露骨文字，是否算刑法第 235 條的「散布」、「公然陳列」或「供人觀覽」？要用什麼「隔絕措施」（年齡驗證的強度）才算足夠？
2. 本服務是否屬於兒少法第 46 條的「網路平台業者」？具體要採取什麼措施？
3. 玩家的性相關對話紀錄是否屬於個資法第 6 條的特種個資？若是，蒐集、處理、保存需要什麼同意與安全措施？能不能在**不保存全文**的前提下提供服務？
4. 年齡確認只要玩家自行聲明就夠嗎？還是必須實際驗證？驗證所蒐集的資料有何限制？
5. Gemini API 禁止用於「可能被未滿 18 歲者使用」的應用：年齡閘門要做到什麼程度才算符合？
6. 能不能**以書面向 Google 確認**：成年角色之間的露骨創作，是否可能適用「藝術考量」例外？「較寬鬆安全設定」的審查與核准流程是什麼？違規後果是否包含停用整個專案的 Gemini 帳號？
7. 若使用第二家供應商，其合約、資料處理、跨境傳輸的條款是否可接受？
8. 服務條款與隱私權政策需要補哪些條款（內容規範、年齡、檢舉處理、AI 處理角色描述、去識別後管理員審閱）？
9. 接獲檢舉或主管機關通知時的處理流程與資料保存義務？
10. 玩家若來自台灣以外，是否適用其他地區的年齡驗證或線上安全法？
11. 若改走自架模型或第二後端，營運者對輸出內容的責任範圍，與使用託管供應商有何不同？
12. 雲端（Google Cloud）、網域、金流與寄信服務對成人內容的條款，是否容許？（本文件第 4 章未能取得 Google Cloud 原文，其餘未研究。）

第 1 至 4 題與 RPG.md R-15.3 的「第二版發行條件」直接相關。

---

## 第 7 章　給 Google 的詢問信草稿

**可寄出的正式版本是 [google.pdf](google.pdf)**（2 頁，含 6 個問題與安全措施說明；寄件人欄位已填入：Zhengmin Huang、個人工作室、專案 R.A.C、免費層，以及回覆用的聯絡信箱）。下方文字是早期草稿的備份，**以 PDF 為準**。

收件人是 Google。**官方文件沒有列出政策審核的信箱**（見 2.5），整理者不會編造一個。已知可嘗試的管道，**都無法保證會得到有約束力的書面答覆**，請務必留存書面紀錄：

| 管道 | 說明 |
|---|---|
| [Google Cloud「Contact Sales」](https://cloud.google.com/contact) | 官方的業務聯絡入口，通常會指派客戶經理。該頁內容工具讀不到，無法確認表單的細節 |
| Google Cloud 支援案件 | 於 Cloud Console 開立；未驗證需要哪種支援方案 |
| [Google AI 開發者論壇](https://discuss.ai.google.dev/) | 公開、不具約束力。論壇上有使用者回報申訴表單限 500 字、且有人數日沒有回應 |

信中**必須如實描述範圍**，不要淡化：用含糊說法換到的核准沒有保障，事後被發現描述不實，反而更容易導致帳號被停用。**主動詢問本身也有風險**：它會讓 Google 注意到這個專案的內容計畫，是否寄出由專案負責人決定。

> **Subject:** Policy clarification request — adult-only interactive fiction built on the Gemini API
>
> We are building a Traditional Chinese, text-based open-world RPG that uses the Gemini API to generate narrative. The product is pre-launch; before any public launch, access will be restricted to adults (18+) through age confirmation at registration (not yet implemented). Before launch we would like written clarification on the following, so that we comply with the Gemini API Additional Terms and the Generative AI Prohibited Use Policy.
>
> **Scope of content.** Fictional narrative that includes (a) dark themes such as combat, death, villains, threats and morally grey characters; and (b) in an opt-in, age-gated tier, sexually explicit text between consenting, fictional adult characters. The product will not generate sexual content involving minors or characters who appear to be minors, sexual violence or non-consensual sexual content, or sexual content about real people.
>
> **Questions.**
> 1. Does fictional violence, hostile or threatening non-player characters, villains' insulting dialogue, and in-universe prejudice between fictional species or factions (e.g., humans disdaining beast-folk in a fantasy world) fall within items 3.1–3.3 when depicted in clearly fictional narrative? We note the safety-settings documentation lists video-game dialogue as a legitimate reason to adjust thresholds.
> 2. Could explicit consensual adult fiction fall under the "artistic considerations" exception to item 3.4, or is it prohibited regardless of context? Is there a way to obtain a written determination?
> 3. What is the review and approval process for "applications with less restrictive safety settings," and what information do you need from us?
> 4. Does an age-verified, adult-only application satisfy the Age Requirements for "API Clients" (not "directed towards or likely to be accessed by individuals under the age of 18")? What level of age verification do you expect?
> 5. If a violation is found, what are the consequences, and is there a notice or cure period before the account or project is suspended?
>
> We will keep the explicit tier disabled until we receive written guidance.

---

## 第 8 章　未涵蓋的範圍

- Microsoft（Azure OpenAI）、阿里雲與 DeepSeek 等供應商。
- 轉接與託管開放模型的平台（OpenRouter、Together 這類）。
- 各個開放模型各自的授權條款。
- 金流、網域、寄信服務、應用程式商店的條款。
- 各國法規（第 5 章末所列）。
- 圖片、影片與語音內容。

---

## 附錄 A　備案與其他技術發現

這些是設計過程中查到的技術事實，與條款無直接關係，但影響 RPG.md R-16 的選擇。

**Gemini 內建的 Google 搜尋（grounding）**——作為外部資料的備案：

- 價格說法互相矛盾：舊版價格頁是免費層每日約 500 次、付費層每日 1,500 次免費後每千次 35 美元（[論壇說明](https://discuss.ai.google.dev/t/gemini-api-grounding-billing-for-requests-exceeding-daily-free-tier/102927)）；較新的[官方價格頁](https://ai.google.dev/gemini-api/docs/pricing?authuser=3)版本寫成每月 5,000 次免費、之後每千次 14 美元，並註明按每個實際搜尋查詢計費。使用前必須讀即時頁面。
- 有顯示義務與額外資料收集：[Gemini API 附加條款](https://ai.google.dev/terms?hl=ru)定義了「Grounded Results」與必須顯示的「Search Suggestions」，並提到使用時會額外收集資料（本整理未取得該節全文）。
- 與結構化輸出（JSON schema）並用：**沒有官方文件證實**。只有[第三方範例](https://zilliz.com/ai-faq/does-gemini-3-support-structured-output-and-functioncalling-schemas)顯示內建工具可搭配 schema；另有[使用者回報](https://discuss.ai.google.dev/t/gemini-responds-with-structured-json-like-output-only-when-function-calling-is-enabled/112993)啟用函式呼叫後回應變成類 JSON，尚未證實。
- 無法限定來源為白名單，內容安全的控制較弱。

**官方 SDK 的 MCP 支援**：`google-genai` 可以把 MCP 連線直接當工具傳入，由 SDK 自動呼叫（[說明](https://www.mintlify.com/googleapis/python-genai/guides/mcp-support)）。標示為實驗性功能；[第三方整合文件](https://gofastmcp.com/integrations/gemini)指出只支援工具，不支援資源與提示。這是「模型主導」的自動迴圈，與 RPG.md R-16 的 Service 主導設計不同，因此需要自行以 `mcp` 套件撰寫用戶端。

---

## 附錄 B　來源與取得日期

全部於 2026-10-28 取得。

| 類別 | 連結 |
|---|---|
| Google | [禁止使用政策](https://policies.google.com/terms/generative-ai/use-policy)、[Gemini API 附加條款](https://ai.google.dev/gemini-api/terms)、[安全設定](https://ai.google.dev/gemini-api/docs/safety-settings)、[濫用監控](https://ai.google.dev/gemini-api/docs/usage-policies)、[Gemma 禁止使用政策](https://ai.google.dev/gemma/prohibited_use_policy)、[Google Cloud 可接受使用政策](https://cloud.google.com/terms/aup)（未能取得） |
| 其他供應商 | [Anthropic](https://www.anthropic.com/legal/aup)、[Cohere](https://cohere.com/usage-policy)、[Meta Llama 4](https://dev.meta.ai/llama/llama4/use-policy/)、[Mistral](https://legal.mistral.ai/terms/usage-policy)、[OpenAI](https://openai.com/policies/usage-policies/)（未能取得）、[xAI](https://x.ai/legal/acceptable-use-policy)（未能取得）、[AWS](https://aws.amazon.com/aup/) |
| 台灣法規 | [刑法第 235 條](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=C0000001&flno=235)、[個資法第 6 條](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=6)、[兒少福權法第 46 條](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=D0050001&flno=46)、釋字第 617 號的[二手摘要](https://www.legis-pedia.com/dictionary/1000376) |
