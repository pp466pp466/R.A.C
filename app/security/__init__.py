"""HTTP 安全輔助：登入 cookie、CSRF、簽章的短效 cookie、安全轉址。

這一層可以使用 Flask，但不得直接碰資料庫（不得匯入 app.db、app.models、app.repositories、sqlalchemy）；
需要資料時呼叫 Service。見 tests/architecture/test_layering.py。
"""
