"""負載測試（見 load-testing 規格與 docs/營運與部署-operations.md 的「負載測試」）。

- `provider.py`：可設定延遲與錯誤率的假模型供應商（真的睡眠，不消耗任何額度）；
- `fake_app.py`：以假供應商取代真實模型的應用程式入口，**只在 development 與 testing 載入**；
- `guard.py`：位址守門（預設只對本機；其他位址需確認參數；正式環境一律拒絕）；
- `actions.py`：一位虛擬會員的行為（不依賴 Locust，可用 Flask 測試客戶端驅動）；
- `locustfile.py`：Locust 的薄包裝；
- `evaluate.py`：讀取 Locust 的 CSV，依建議的通過標準判定。

這個目錄不在容器映像內（Dockerfile 不複製、.dockerignore 排除），Locust 也只在 requirements-dev.txt。
"""
