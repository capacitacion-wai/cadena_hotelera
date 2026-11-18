import os

WAI_BACKEND_URL = os.environ.get(
    "WAI_BACKEND_URL",
    "http://demo-wai-backend.demo.svc.cluster.local:5002",
)
WAI_INTROSPECCION_URL = os.environ.get(
    "WAI_INTROSPECCION_URL",
    f"{WAI_BACKEND_URL}/backend/auth/introspeccion",
)
PROJECT_ID = int(os.environ.get("PROJECT_ID", "43"))
DB_SCHEMA = os.environ.get("DB_SCHEMA", "demo_wai")
APP_NAME = os.environ.get("APP_NAME", "gestor-de-ocupacion")
APP_DB_PATH = os.environ.get("APP_DB_PATH", "/app-data/app.sqlite")
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL",
    "sqlite+aiosqlite:////app-data/app.sqlite",
)
