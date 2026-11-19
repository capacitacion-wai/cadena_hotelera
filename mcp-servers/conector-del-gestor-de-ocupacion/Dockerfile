# Imagen del servidor MCP. Pequeña a propósito: se despliega uno por conector y algunos
# viven en el namespace compartido, donde la memoria es de todos.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Sin root: si alguien encuentra un fallo en el conector, que no encuentre además la caja.
RUN useradd -m -u 1000 mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
