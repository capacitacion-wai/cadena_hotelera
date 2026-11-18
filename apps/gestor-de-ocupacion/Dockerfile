# Stage 1: Build frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
# Copy package.json and package-lock.json (the trailing * makes the COPY
# tolerate the lockfile being absent — the agent should still ship it).
COPY src/frontend/package.json src/frontend/package-lock.json* ./
# Use `npm ci` when the lockfile is present (fast, deterministic). Fall back
# to `npm install` when it is missing — otherwise `npm ci` exits with the
# cryptic "Usage: npm ci" banner and the real cause (missing lockfile) hides
# in /root/.npm/_logs. The agent should still materialise package-lock.json
# locally (cd src/frontend && npm install) before delivery so the build runs
# the fast, deterministic branch.
RUN if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund ; \
    else \
      echo "WARNING: package-lock.json missing, falling back to 'npm install' (non-reproducible — generate the lockfile locally before the next build)." ; \
      npm install --no-audit --no-fund ; \
    fi
COPY src/frontend/ ./
RUN npm run build

# Stage 2: Python backend + compiled frontend
FROM python:3.12-slim
WORKDIR /app
COPY src/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src/backend/ ./
COPY --from=frontend-build /app/frontend/dist ./static
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
