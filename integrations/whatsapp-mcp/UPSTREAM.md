# Vendored: lharries/whatsapp-mcp

Source: https://github.com/lharries/whatsapp-mcp (MIT License, see LICENSE), commit `7d6a06d`.

Local changes in `whatsapp-bridge/main.go` for Terminator:

- **Security:** the REST API listens on `127.0.0.1` only. Upstream used `:8080` (all interfaces, no auth), which let
  anyone on the local network send messages as the account owner and read any file through `media_path`.
- `store/` is created `0700`. It holds the WhatsApp session keys and must stay private and out of git.
- The current QR code is written to `store/qr.txt` (0600) so Terminator can show it in Settings → WhatsApp.
- When the QR codes expire, the bridge exits immediately so the service restarts with a fresh code. Upstream waited
  3 more minutes with no valid code.
- Updated `go.mau.fi/whatsmeow` to a current version, because WhatsApp rejects the March 2025 client ("client outdated"),
  and added the `context.Context` arguments its API now requires.

Build: `cd whatsapp-bridge && CGO_ENABLED=1 go build -o whatsapp-bridge .` (Go 1.24+)
