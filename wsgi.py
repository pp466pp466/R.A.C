"""gunicorn 與 `flask run` 的進入點。"""
from app import create_app

app = create_app()
