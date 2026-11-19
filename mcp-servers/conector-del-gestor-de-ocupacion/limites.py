"""What goes out the door, capped. Applied in the ROUTING, not in each tool.

Why here and not in each tool: a tool that forgets the cap does not fail — it works
and wipes out the model's context window and the month's bill. A limit that
depends on someone remembering is not a limit. Placed in the routing, no response
can dodge it, not even the ones written tomorrow.

What it caps, and why each thing:

* **The size of a value.** A row with a 10 MB `TEXT` fits in "50 rows" and fits in no
  conversation. It is truncated and says it was truncated — a text cut
  silently is worse than one cut.
* **The number of items.** Long lists are truncated saying how many there were: the model
  needs to know there are more, not see them all.
* **The total size.** The last safety belt, in case a thousand small values add up to one
  big one.
"""
from __future__ import annotations

import json
import os
from typing import Any

# They can be raised in the deployment configuration, but the defaults are designed to
# fit comfortably in a conversation.
MAX_TEXTO = int(os.environ.get("MCP_MAX_TEXTO", "2000"))          # characters per value
MAX_ELEMENTOS = int(os.environ.get("MCP_MAX_ELEMENTOS", "200"))   # items per list
MAX_TOTAL = int(os.environ.get("MCP_MAX_TOTAL", "120000"))        # characters of the response

_AVISO = "…[truncated, {quedan} more characters]"


def recortar(valor: Any) -> Any:
    """Returns the same data, capped and SAYING where it was truncated."""
    if isinstance(valor, str):
        if len(valor) <= MAX_TEXTO:
            return valor
        return valor[:MAX_TEXTO] + _AVISO.format(quedan=len(valor) - MAX_TEXTO)
    if isinstance(valor, dict):
        return {k: recortar(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        recortados = [recortar(v) for v in valor[:MAX_ELEMENTOS]]
        if len(valor) > MAX_ELEMENTOS:
            recortados.append(f"…[{len(valor) - MAX_ELEMENTOS} more items, not shown]")
        return recortados
    return valor


def serializar(resultado: Any) -> str:
    """The text returned to the model, also capped IN TOTAL.

    The last safety belt: a thousand values of two thousand characters pass the caps above and
    still add up to two megabytes."""
    texto = json.dumps(recortar(resultado), ensure_ascii=False, default=str)
    if len(texto) <= MAX_TOTAL:
        return texto
    return texto[:MAX_TOTAL] + f"…[response truncated: {len(texto) - MAX_TOTAL} more characters]"
