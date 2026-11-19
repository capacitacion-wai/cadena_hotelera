from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.api_errors import api_error
from app.app_database import app_session
from app.database import project_db_ro
from app.config import EXPOSED_PROJECT_TABLES, EXPOSED_LOCAL_TABLES
from app.auth import require_admin, require_project_access, TokenPayload
from app.pagination import paginated

# SECURITY: the employee's ADMINISTRATORS ONLY. This router dumps WHOLE tables: the default
# local list includes `agent_runs` (the prompt and the reply of EVERY turn of EVERY person)
# and `tool_call_log`. With `require_project_access` any member could page through everyone
# else's conversations —it was the shortcut that bypassed the `require_admin` with which
# `/api/runs` protects exactly the same thing. The SPA does not use it for anything member.
router = APIRouter(prefix="/api/data", tags=["data"],
                   dependencies=[Depends(require_admin)])

# Upper bound for a single page of raw table rows. Without a ceiling a caller
# (or a prompt-injected agent hitting this surface) could request an unbounded
# page: on the local SQLite store ``LIMIT -1`` means "ALL rows" and a huge
# ``LIMIT`` streams the whole table — a DoS / bulk-exfiltration amplifier even
# within the whitelist. FastAPI's Query bounds reject out-of-range values (422)
# BEFORE the SQL is built, so ``limit`` stays in [0, MAX] and ``offset`` >= 0.
_MAX_PAGE = 500


@router.get("/tables")
async def list_tables(
    user: TokenPayload = Depends(require_project_access),
):
    return {
        "project": list(EXPOSED_PROJECT_TABLES),
        "local":   list(EXPOSED_LOCAL_TABLES),
    }


@router.get("/project/{table}")
async def project_table(
    table: str,
    limit: int = Query(50, ge=0, le=_MAX_PAGE),
    offset: int = Query(0, ge=0),
    user: TokenPayload = Depends(require_project_access),
):
    if table not in EXPOSED_PROJECT_TABLES:
        raise api_error(404, "not_found", f"Table '{table}' is not exposed.")
    async with project_db_ro() as ro:
        total = (await ro.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar()
        rows = (await ro.execute(text(
            f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset})).mappings().all()
    return paginated(total or 0, limit, offset, [dict(r) for r in rows])


@router.get("/local/{table}")
async def local_table(
    table: str,
    limit: int = Query(50, ge=0, le=_MAX_PAGE),
    offset: int = Query(0, ge=0),
    user: TokenPayload = Depends(require_project_access),
):
    if table not in EXPOSED_LOCAL_TABLES:
        raise api_error(404, "not_found", f"Table '{table}' is not exposed.")
    async with app_session() as s:
        total = (await s.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar()
        rows = (await s.execute(text(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset})).mappings().all()
    return paginated(total or 0, limit, offset, [dict(r) for r in rows])
