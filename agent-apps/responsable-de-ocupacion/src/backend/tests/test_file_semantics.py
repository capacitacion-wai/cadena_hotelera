"""v7.96.0 — files in memory carry SEMANTICS, not just a channel label.

Post-mortem: the Memoria panel showed every chat deliverable as an opaque
«Generado por chat» — nothing telling the employee WHAT a file holds or WHEN
to use it, so memory search couldn't recall the right artifact. The floor is
now ``build_file_semantics``: origin + the request that produced the file +
the answer sentence naming it + a content sniff, embedded for dense recall.
``index_file`` UPSERTS so an agent-written description replaces the heuristic
one instead of colliding with ``(user_email, workspace_path)`` uniqueness.
"""
from __future__ import annotations

import pytest

from app.memory_utils import (
    _mention_snippet,
    _sniff_file_content,
    auto_index_generated_files,
    build_file_semantics,
)


# ── content sniff ────────────────────────────────────────────────────────────

def test_sniff_csv_columns_and_rows(tmp_path) -> None:
    (tmp_path / "inv.csv").write_text(
        "sku,nombre,stock,precio_eur\nN-1,Nopal fresco,120,3.5\nN-2,Nopal seco,40,7.9\n"
    )
    out = _sniff_file_content(str(tmp_path), "inv.csv")
    assert "CSV" in out and "2 filas" in out
    assert "sku" in out and "precio_eur" in out


def test_sniff_md_first_heading(tmp_path) -> None:
    (tmp_path / "plan.md").write_text("\n# Plan de negocio Nopal México\n\ncuerpo\n")
    out = _sniff_file_content(str(tmp_path), "plan.md")
    assert "Plan de negocio Nopal México" in out


def test_sniff_binary_ext_returns_empty(tmp_path) -> None:
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4 garbage")
    assert _sniff_file_content(str(tmp_path), "x.pdf") == ""


# ── mention snippet ──────────────────────────────────────────────────────────

def test_mention_snippet_extracts_sentence() -> None:
    text = (
        "He terminado el análisis.\n"
        "He generado el plan adaptado a EUR en `exports/plan_eur.pdf` con las "
        "proyecciones a 3 años.\nAlgo más."
    )
    snip = _mention_snippet(text, "plan_eur.pdf")
    assert "plan adaptado a EUR" in snip and "proyecciones" in snip


def test_mention_snippet_absent_file() -> None:
    assert _mention_snippet("no hay ficheros aquí", "x.csv") == ""


# ── composition ──────────────────────────────────────────────────────────────

def test_build_semantics_composes_all_parts(tmp_path) -> None:
    (tmp_path / "inv.csv").write_text("sku,stock\nA,1\n")
    desc = build_file_semantics(
        "inv.csv", root=str(tmp_path), origin="Generado por chat",
        response_text="Aquí tienes el inventario inicial en inv.csv listo para importar.",
        user_message="prepara el inventario inicial del plan de nopal en EUR",
    )
    assert desc.startswith("Generado por chat")
    assert "inventario inicial del plan de nopal" in desc   # the request anchor
    assert "listo para importar" in desc                    # the answer mention
    assert "Columnas" in desc                               # the sniff


def test_build_semantics_skips_trivial_request(tmp_path) -> None:
    desc = build_file_semantics(
        "a.bin", root=str(tmp_path), origin="Generado por chat",
        user_message="hola",
    )
    assert desc == "Generado por chat"


# ── auto-index integration: the stored row is semantic + embedded ───────────

@pytest.mark.asyncio
async def test_auto_index_stores_semantics_and_embedding(tmp_path, monkeypatch) -> None:
    import app.workspace_fs as wfs
    from sqlalchemy import select
    from app.app_database import app_session
    from app.app_models.memory import UserFile
    from app.app_models.runtime import AgentRun

    sub = "sessions/users/tester"
    exports = tmp_path / sub / "exports"
    exports.mkdir(parents=True)
    (exports / "ventas.csv").write_text("mes,total_eur\nenero,1000\n")
    monkeypatch.setattr(wfs, "workspace_root", lambda: str(tmp_path))

    async with app_session() as s:
        s.add(AgentRun(id=501, idempotency_key="fsem-run-501", prompt="", mode="chat"))
        await s.commit()

    arts = await auto_index_generated_files(
        501, "boss@acme.com",
        f"He dejado el resumen de ventas en {sub}/exports/ventas.csv como pediste.",
        workspace_subdir=sub, since_ts=0.0,
        user_message="dame las ventas mensuales en EUR",
    )
    assert any(a["filename"] == "ventas.csv" for a in arts)
    async with app_session() as s:
        row = (await s.execute(select(UserFile).where(
            UserFile.user_email == "boss@acme.com",
            UserFile.filename == "ventas.csv",
        ))).scalar_one()
    assert "ventas mensuales en EUR" in (row.description or "")
    assert "Columnas" in (row.description or "")
    assert row.embedding is not None and row.embed_model


# ── chat workspace upload-to-folder: self-scoped + memory-synced (v7.97.0) ──

@pytest.mark.asyncio
async def test_chat_upload_to_stores_and_indexes(tmp_path, monkeypatch) -> None:
    from io import BytesIO
    from types import SimpleNamespace
    from fastapi import UploadFile
    from sqlalchemy import select
    from app.agent_sessions import interactive_scope
    from app.app_database import app_session
    from app.app_models.memory import UserFile
    from app.routes.workspace_routes import chat_upload_to

    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path))
    email = "boss@acme.com"
    sub = interactive_scope(email).workspace_subdir
    up = UploadFile(file=BytesIO(b"sku,stock\nA,5\nB,2\n"), filename="stock.csv")
    out = await chat_upload_to(
        path=f"{sub}/facturas", files=[up], user=SimpleNamespace(email=email),
    )
    assert out["uploaded"] == [f"{sub}/facturas/stock.csv"]
    assert out["memory_indexed"] == 1
    async with app_session() as s:
        row = (await s.execute(select(UserFile).where(
            UserFile.user_email == email, UserFile.filename == "stock.csv",
        ))).scalar_one()
    assert "Subido al workspace" in (row.description or "")
    assert "Columnas: sku, stock" in (row.description or "")
    assert row.embedding is not None and row.embed_model


@pytest.mark.asyncio
async def test_chat_upload_to_rejects_foreign_scope(tmp_path, monkeypatch) -> None:
    from io import BytesIO
    from types import SimpleNamespace
    from fastapi import HTTPException, UploadFile
    from app.routes.workspace_routes import chat_upload_to

    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(tmp_path))
    up = UploadFile(file=BytesIO(b"x"), filename="x.txt")
    with pytest.raises(HTTPException) as exc:
        await chat_upload_to(
            path="sessions/users/otro-usuario/exports",
            files=[up],
            user=SimpleNamespace(email="boss@acme.com"),
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "workspace.outside_scope"
