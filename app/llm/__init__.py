"""模型呼叫層：供應商中立的型別與介面（`types.py`），以及各供應商的轉接器（目前只有 `gemini.py`）。

Service 與端點只匯入 `app.llm.types`；供應商專屬的請求格式、安全設定與錯誤對應只存在於轉接器內
（見 llm-provider 規格與 tests/architecture）。這一層不依賴 services、repositories、controllers、models 與 flask。
"""
