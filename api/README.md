# AutoSwap Hosted API — Model C (Paid)

FastAPI server wrapping the AutoSwap SDK with:
- **0.1% commission** per swap (minimum $0.01)
- **API key auth** (`X-API-Key` header)
- Cross-chain swaps, quotes, and route discovery

## Endpoints

| Method | Path              | Auth     | Description                        |
|--------|-------------------|----------|------------------------------------|
| GET    | `/health`         | None     | Health check + commission config   |
| GET    | `/`               | None     | API info                           |
| GET    | `/routes`         | API key  | Supported chains & tokens          |
| POST   | `/quote`          | API key  | Price estimate (no execution)      |
| POST   | `/swap`           | API key  | Execute swap with commission       |
| POST   | `/admin/keys`     | Admin    | Generate new API key               |
| DELETE | `/admin/keys/:id` | Admin    | Revoke API key                     |

Full Swagger UI at `/docs`, ReDoc at `/redoc`.

## Commission Model

```
commission = max(amount_out × 0.1%, $0.01)
Sent to: 0x804dd2cE4aA3296831c880139040e4326df13c6e
```

Commission is deducted from `amount_out` automatically. The response includes both
`amount_out` (net, after commission) and `amount_out_gross` (before commission).

## Local Development

```bash
cd api/
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set ADMIN_SECRET

python server.py
# → http://localhost:8080/docs
```

### Generate an API key (local)

```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"admin_secret": "change-me-to-a-strong-secret", "label": "test"}'
```

### Call /swap

```bash
curl -X POST http://localhost:8080/swap \
  -H "X-API-Key: ask-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "from_token": "ETH",
    "from_chain": "base",
    "to_token": "USDC",
    "to_chain": "polygon",
    "amount": 0.01,
    "wallet_key": "0xYOUR_PRIVATE_KEY",
    "slippage_max": 2.0
  }'
```

### Call /quote

```bash
curl -X POST http://localhost:8080/quote \
  -H "X-API-Key: ask-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "from_token": "ETH",
    "from_chain": "base",
    "to_token": "USDC",
    "to_chain": "polygon",
    "amount": 0.01,
    "slippage_max": 2.0
  }'
```

## Deploy to Railway

### 1. Create project
```bash
railway login
cd /Users/sam/Desktop/samDev/p14/api
railway init  # or link existing project
```

### 2. Set env vars
```bash
railway variables set ADMIN_SECRET="$(openssl rand -hex 32)"
railway variables set AGENT_WALLET_KEY="0xYOUR_KEY"
# Optional: pre-seed API keys
railway variables set API_KEYS="ask-$(python -c "import secrets; print(secrets.token_urlsafe(24))")"
```

### 3. Deploy
```bash
railway up
# Target URL: https://autoswap-api.up.railway.app
```

### 4. Generate first production API key
```bash
export ADMIN=$(railway variables get ADMIN_SECRET)
curl -X POST https://autoswap-api.up.railway.app/admin/keys \
  -H "Content-Type: application/json" \
  -d "{\"admin_secret\": \"$ADMIN\", \"label\": \"first-customer\"}"
```

## Security Notes

- `wallet_key` is used only to sign transactions in-process — never logged, never stored
- `AGENT_WALLET_KEY` must be set via Railway env vars, never committed to git
- Rate limiting: add nginx/Cloudflare in front for production scale
- API keys: in-memory only (restart clears them) — use `API_KEYS` env var for persistence
- For production: replace in-memory key store with Redis or PostgreSQL

## Architecture

```
Client
  │  X-API-Key: ask-xxx
  ▼
FastAPI server (Railway)
  │
  ├── /quote  → Router.get_best_route() → price estimate + commission preview
  ├── /swap   → autoswap.swap() → execute → deduct commission → return net result
  │             └── async: forward commission to 0x804d...
  └── /admin  → key management (ADMIN_SECRET protected)
```
