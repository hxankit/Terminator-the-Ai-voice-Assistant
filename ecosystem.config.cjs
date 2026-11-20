// PM2: build the frontend first (npm --prefix frontend run build); FastAPI then serves it.
// Keep ONE instance: stop signals and the WebSocket hub are in-process (see README › Scaling).
module.exports = {
  apps: [{
    name: 'terminator',
    cwd: __dirname + '/backend',
    script: '.venv/bin/uvicorn',
    args: 'app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1',
    interpreter: 'none',
    instances: 1,
    autorestart: true,
    max_memory_restart: '1G',
    env: { APP_ENV: 'production' },
  }],
}
