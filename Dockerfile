# --- build the React frontend ---
FROM node:24-slim AS web
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Python runtime ---
FROM python:3.12-slim
# bubblewrap powers the code sandbox. Inside Docker it needs user namespaces; see README › Docker.
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap procps && rm -rf /var/lib/apt/lists/*
RUN useradd -m app
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN python -m venv /app/backend/.venv && /app/backend/.venv/bin/pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /src/frontend/dist /app/frontend/dist
RUN mkdir -p /app/backend/data && chown -R app /app/backend/data
USER app
ENV APP_ENV=production DATA_DIR=/app/backend/data
EXPOSE 8000
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
