"""Files that COME INTO and GO OUT of a connector, without breaking on the way.

It exists because of a measurement, not for completeness. Before this, a tool returning a
200 KB PDF as base64 went through the routing's text cap and came out **truncated to 2,000
characters**: 0.7 % of the file, and not even valid base64. The call answered 200, the
log said nothing and the model received garbage that looked like data. It is the worst outcome in
this house — it works and it lies —, and that is why files cannot travel as just another text.

Two rules, and both are the opposite of what `limites.py` does with text:

* **A file is NOT truncated: it either fits or fails.** A cut text is still readable and
  warns it was cut; a cut file is a corrupt file nobody can tell from
  a good one until they try to open it, far from here.
* **The cap is checked in BOTH directions.** The upload too: without that, whoever calls
  can push whatever they want down the pipe and the pod's memory is the limit.

How to use it, in both directions:

    # a file goes out
    async def factura_en_pdf(numero: str, cabeceras: dict) -> ficheros.Fichero:
        return await app_wai.descargar(cabeceras, f"/api/facturas/{numero}/pdf")

    # a file comes in
    async def adjuntar_justificante(gasto_id: str, fichero_b64: str, nombre: str,
                                    cabeceras: dict):
        f = ficheros.desde_base64(nombre, fichero_b64)
        return await app_wai.subir(cabeceras, f"/api/gastos/{gasto_id}/adjunto", f)
"""
from __future__ import annotations

import base64
import mimetypes
import os
from dataclasses import dataclass

# Per-file cap. It can be raised in the deployment configuration, but not for convenience:
# what goes through here ends up going through a model's conversation, and megabytes are paid
# twice — in the pipe and in the bill —. If something truly big needs moving, what
# moves is a reference, not the file.
MAX_MB = float(os.environ.get("MCP_MAX_FICHERO_MB", "8"))
MAX_BYTES = int(MAX_MB * 1024 * 1024)


class FicheroDemasiadoGrande(Exception):
    """Over the cap. It is NOT truncated: a half file is a corrupt file."""


class FicheroNoValido(Exception):
    """What arrived is not base64, or is empty."""


@dataclass
class Fichero:
    """A real file: name, bytes and type. No loose strings.

    Being its own type and not a `dict` is what lets the routing recognize it and take it
    off the text path. With a dict it would have to guess from the key names,
    and guessing is how the truncated PDF happened."""
    nombre: str
    contenido: bytes
    tipo_mime: str = ""

    def __post_init__(self) -> None:
        if not self.tipo_mime:
            self.tipo_mime = (mimetypes.guess_type(self.nombre)[0]
                              or "application/octet-stream")
        if len(self.contenido) > MAX_BYTES:
            raise FicheroDemasiadoGrande(
                f"'{self.nombre}' is {len(self.contenido) / 1024 / 1024:.1f} MB and this "
                f"connector's cap is {MAX_MB:g} MB. It is not truncated on purpose: a cut "
                f"file cannot be told from a good one until someone tries to open it. "
                f"Raise `MCP_MAX_FICHERO_MB` in its configuration if it really has to fit, "
                f"or return a reference instead of the file."
            )
        if not self.contenido:
            raise FicheroNoValido(f"'{self.nombre}' is empty: there is no file to send.")

    @property
    def base64(self) -> str:
        return base64.b64encode(self.contenido).decode("ascii")

    def contenido_mcp(self) -> list[dict]:
        """The blocks returned to the model: the file AND what it is.

        Both always go. The binary block is what an MCP client that knows how can use;
        the text one is what makes the model KNOW it received something and able to say its
        name and size — without it, a client that ignores the binary leaves the model with an
        empty response and no way of telling. It is not a copy of the file: it is its card."""
        if self.tipo_mime.startswith("image/"):
            binario = {"type": "image", "data": self.base64, "mimeType": self.tipo_mime}
        else:
            # `resource` with `blob` is the shape the protocol gives to what is neither image nor
            # audio. The `uri` is not resolved over the network: it identifies the file within the
            # response, which is what the protocol asks it for.
            binario = {"type": "resource", "resource": {
                "uri": f"wai-mcp://fichero/{self.nombre}",
                "mimeType": self.tipo_mime,
                "blob": self.base64,
            }}
        ficha = (f'{{"fichero": "{self.nombre}", "tipo": "{self.tipo_mime}", '
                 f'"bytes": {len(self.contenido)}}}')
        return [binario, {"type": "text", "text": ficha}]


def desde_base64(nombre: str, contenido_b64: str, tipo_mime: str = "") -> Fichero:
    """A file that COMES IN, validated before touching it.

    The size is checked BEFORE decoding — on the string, which already tells well enough how much
    it will take — so as not to reserve in memory what is going to be rejected. And `validate=True`, since
    without it base64 swallows the characters it does not understand and returns arbitrary bytes:
    the file would be stored corrupt without a single error."""
    if not (contenido_b64 or "").strip():
        raise FicheroNoValido(f"'{nombre}' arrived with no content.")
    if len(contenido_b64) * 3 // 4 > MAX_BYTES:
        raise FicheroDemasiadoGrande(
            f"'{nombre}' exceeds this connector's {MAX_MB:g} MB cap.")
    try:
        crudo = base64.b64decode(contenido_b64, validate=True)
    except Exception as e:  # noqa: BLE001
        raise FicheroNoValido(
            f"'{nombre}' is not valid base64 ({e}). If you truncated it so it "
            f"would fit, send it whole or not at all: half a file cannot be opened."
        ) from e
    return Fichero(nombre=nombre, contenido=crudo, tipo_mime=tipo_mime)
