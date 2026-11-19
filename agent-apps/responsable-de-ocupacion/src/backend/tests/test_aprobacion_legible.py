"""An approval says what will be sent, in one known language (14.15.0).

  · the approver sees each tool that will run and its exact arguments (the reply text, the
    comment it answers), unwrapped from a Composio router call, without the agent's notes;
  · the approval texts are asked for in the employee's working language, whatever language
    the chat was in — and the title is no longer a Spanish template.
"""
from __future__ import annotations

import json

from app.mcp_policy import approval_message
from app.routes.approvals_routes import lo_que_se_envia


def test_una_llamada_del_enrutador_se_desenvuelve():
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
        "arguments": {"thought": "reply politely", "current_step": "REPLYING",
                      "tools": [{"tool_slug": "INSTAGRAM_REPLY_TO_COMMENT",
                                 "arguments": {"comment_id": "1789", "message": "It depends on the flow."}}]}}})
    assert lo_que_se_envia(body) == [{"tool": "INSTAGRAM_REPLY_TO_COMMENT",
                                      "arguments": {"comment_id": "1789", "message": "It depends on the flow."}}]


def test_una_llamada_directa_y_lo_ilegible():
    body = json.dumps({"method": "tools/call", "params": {"name": "HUBSPOT_CREATE_COMPANY",
                                                          "arguments": {"name": "Red Quay"}}})
    assert lo_que_se_envia(body) == [{"tool": "HUBSPOT_CREATE_COMPANY", "arguments": {"name": "Red Quay"}}]
    assert lo_que_se_envia("not json") == [] and lo_que_se_envia(None) == []


def test_el_idioma_de_la_aprobacion_es_el_del_empleado():
    en = approval_message("composio-instagram", ["INSTAGRAM_REPLY_TO_COMMENT"], "abc", idioma="en")
    es = approval_message("composio-instagram", ["INSTAGRAM_REPLY_TO_COMMENT"], "abc", idioma="es")
    assert "in ENGLISH" in en and "whatever language this conversation" in en
    assert "in SPANISH" in es
    assert "Ejecutar" not in en and "Ejecutar" not in es, "no Spanish title template"
    assert '"request_ref": "abc"' in en
