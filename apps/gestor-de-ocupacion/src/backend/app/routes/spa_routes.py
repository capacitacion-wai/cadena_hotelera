from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

SIN_CACHE = {"Cache-Control": "no-cache"}
router = APIRouter(include_in_schema=False)
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
FRONTEND_INDEX = Path(__file__).resolve().parents[3] / "frontend" / "index.html"


class _AssetsInmutables(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def montar_spa(app: FastAPI) -> None:
    if STATIC_DIR.is_dir() and (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", _AssetsInmutables(directory=str(STATIC_DIR / "assets")), name="assets")
    app.include_router(router)


@router.get("/{path:path}")
async def servir_pantalla(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="No existe esa operación.")
    if STATIC_DIR.is_dir():
        root = STATIC_DIR.resolve()
        file_path = (root / path).resolve()
        if file_path.is_relative_to(root) and file_path.is_file():
            return FileResponse(str(file_path), headers=SIN_CACHE)
        return FileResponse(str(root / "index.html"), headers=SIN_CACHE)
    return FileResponse(str(FRONTEND_INDEX), headers=SIN_CACHE)
