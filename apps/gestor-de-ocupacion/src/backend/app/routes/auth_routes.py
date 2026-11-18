# INVARIANT FILE — copied byte for byte by `assets/copiar_base.sh` (wai-app-builder, skill 7.0.0); do not edit
# it: `verify_app.sh` fails if it differs from the skill's. Why it is like this: `patterns/authentication.md`.
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from app.config import WAI_BACKEND_URL

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginRequest):
    """The login is the platform's: the app proxies it and holds no user database."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{WAI_BACKEND_URL}/backend/auth/token",
            data={"username": body.email, "password": body.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return resp.json()


@router.post("/handoff")
async def handoff(body: dict, request: Request):
    """Arriving from an employee with `?wai=<pass>`: the SERVER exchanges the single-use pass for a session, and the
    destination is its own host, never one the browser proposes."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{WAI_BACKEND_URL}/backend/auth/app-handoff/exchange",
            json={"codigo": str(body.get("codigo") or ""), "destino": request.headers.get("host", "")},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid pass")
    return resp.json()      # {access_token, token_type}
