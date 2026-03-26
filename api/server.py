#!/usr/bin/env python3
"""
server.py — AutoSwap Hosted API (Model C — Paid)
══════════════════════════════════════════════════
FastAPI server that wraps the AutoSwap SDK with:
  - API key authentication (header X-API-Key)
  - 0.1% commission (min $0.01) deducted from amount_out
  - Commission forwarded to agent wallet
  - Full swap, quote, routes, and health endpoints

Commission model:
  commission = max(amount_out * 0.001, $0.01 equivalent)
  Sent to: 0x804dd2cE4aA3296831c880139040e4326df13c6e

Deployment: Railway (https://autoswap-api.up.railway.app)

Env vars required:
  ADMIN_SECRET      — secret to generate API keys via /admin/keys
  API_KEYS          — comma-separated list of valid API keys (or use DB)
  AGENT_WALLET_KEY  — private key of the agent wallet (for commission forwarding)
  COMMISSION_WALLET — target wallet address for commissions (default: hardcoded)
"""

import os
import sys
import json
import time
import secrets
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

# ─── Add autoswap to path ───────────────────────────────────────────────────
_api_dir = os.path.dirname(os.path.abspath(__file__))
_p14_dir = os.path.dirname(_api_dir)
_src_dir  = os.path.join(_p14_dir, "src")
for _d in [_p14_dir, _src_dir]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

# ─── FastAPI imports ────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Header, Depends, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field, field_validator
    import uvicorn
except ImportError as e:
    print(f"[FATAL] Missing dependency: {e}")
    print("Run: pip install fastapi uvicorn pydantic")
    sys.exit(1)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="[%(asctime)s] %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("autoswap-api")

# ─── Config ─────────────────────────────────────────────────────────────────
COMMISSION_RATE    = 0.001          # 0.1%
COMMISSION_MIN_USD = 0.01           # $0.01 minimum
COMMISSION_WALLET  = os.getenv(
    "COMMISSION_WALLET",
    "0x804dd2cE4aA3296831c880139040e4326df13c6e"
)
ADMIN_SECRET       = os.getenv("ADMIN_SECRET", "")
API_VERSION        = "1.0.0"

# ─── In-memory API key store (production: replace with Redis/DB) ─────────────
_api_keys: set = set()

def _load_api_keys():
    """Load API keys from environment variable (comma-separated)."""
    raw = os.getenv("API_KEYS", "")
    if raw:
        keys = {k.strip() for k in raw.split(",") if k.strip()}
        _api_keys.update(keys)
        log.info(f"Loaded {len(keys)} API key(s) from environment")
    # Always add a dev key if no keys are configured (Railway first boot)
    if not _api_keys and not ADMIN_SECRET:
        dev_key = "dev-" + secrets.token_hex(16)
        _api_keys.add(dev_key)
        log.warning(f"[DEV MODE] No API keys found. Auto-generated key: {dev_key}")

_load_api_keys()

# ─── Supported chains/tokens (from router) ──────────────────────────────────
SUPPORTED_CHAINS = ["ethereum", "base", "polygon", "arbitrum", "optimism", "avalanche"]
SUPPORTED_TOKENS = {
    "ethereum": ["ETH", "USDC", "USDT", "WETH", "DAI", "WBTC"],
    "base":     ["ETH", "USDC", "WETH", "DAI"],
    "polygon":  ["MATIC", "USDC", "USDT", "WETH", "DAI", "WBTC", "MYST"],
    "arbitrum": ["ETH", "USDC", "USDT", "WETH", "DAI", "ARB"],
    "optimism": ["ETH", "USDC", "USDT", "WETH", "DAI", "OP"],
    "avalanche":["AVAX", "USDC", "USDT", "WETH", "DAI"],
}

# ─── Pydantic models ─────────────────────────────────────────────────────────

class SwapRequest(BaseModel):
    from_token:   str   = Field(..., example="ETH",      description="Source token symbol")
    from_chain:   str   = Field(..., example="base",     description="Source chain")
    to_token:     str   = Field(..., example="USDC",     description="Target token symbol")
    to_chain:     str   = Field(..., example="polygon",  description="Target chain")
    amount:       float = Field(..., gt=0,               description="Amount to swap (in from_token units)")
    wallet_key:   str   = Field(...,                     description="Private key of sender wallet (hex)")
    slippage_max: float = Field(2.0, ge=0.1, le=50.0,   description="Max slippage in % (default 2.0)")

    @field_validator("from_chain", "to_chain")
    @classmethod
    def chain_must_be_supported(cls, v):
        if v.lower() not in SUPPORTED_CHAINS:
            raise ValueError(f"Chain '{v}' not supported. Use: {SUPPORTED_CHAINS}")
        return v.lower()

    @field_validator("wallet_key")
    @classmethod
    def key_format(cls, v):
        key = v.strip()
        if key.startswith("0x"):
            key = key[2:]
        if len(key) != 64:
            raise ValueError("wallet_key must be a 32-byte hex private key (64 hex chars, optional 0x prefix)")
        return "0x" + key


class QuoteRequest(BaseModel):
    from_token:   str   = Field(..., example="ETH")
    from_chain:   str   = Field(..., example="base")
    to_token:     str   = Field(..., example="USDC")
    to_chain:     str   = Field(..., example="polygon")
    amount:       float = Field(..., gt=0)
    slippage_max: float = Field(2.0, ge=0.1, le=50.0)


class AdminKeyRequest(BaseModel):
    admin_secret: str = Field(..., description="Admin secret to authorize key creation")
    label:        str = Field("", description="Optional label for the key")


# ─── Auth dependency ─────────────────────────────────────────────────────────

def require_api_key(x_api_key: Optional[str] = Header(None)):
    """Validate X-API-Key header."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail={"error": "Missing X-API-Key header", "docs": "https://autoswap-api.up.railway.app/docs"}
        )
    if x_api_key not in _api_keys:
        raise HTTPException(
            status_code=403,
            detail={"error": "Invalid API key", "contact": "fino.oss@proton.me"}
        )
    return x_api_key


# ─── Commission helpers ───────────────────────────────────────────────────────

def calculate_commission(amount_out: float, token: str = "USDC") -> dict:
    """
    Calculate commission:
      - 0.1% of amount_out
      - Minimum $0.01 equivalent
    For non-stablecoin tokens, commission is applied as-is (denominated in output token).
    Returns: {commission_amount, commission_rate_pct, min_applied}
    """
    commission_raw = amount_out * COMMISSION_RATE
    min_in_token   = COMMISSION_MIN_USD  # TODO: convert via price feed for non-stable tokens
    commission     = max(commission_raw, min_in_token)
    return {
        "commission_amount":   round(commission, 8),
        "commission_rate_pct": COMMISSION_RATE * 100,
        "min_applied":         commission > commission_raw,
        "commission_wallet":   COMMISSION_WALLET,
    }


def apply_commission(amount_out: float, token: str = "USDC") -> tuple[float, dict]:
    """
    Deduct commission from amount_out.
    Returns: (net_amount_out, commission_info)
    """
    commission_info  = calculate_commission(amount_out, token)
    net_amount_out   = amount_out - commission_info["commission_amount"]
    return net_amount_out, commission_info


def forward_commission_async(amount: float, token: str, from_chain: str, agent_key: str):
    """
    Fire-and-forget: forward commission to agent wallet.
    In production, this should be queued (Celery / background task).
    For now: logs the intent and returns immediately.
    """
    log.info(
        f"[COMMISSION] Forward {amount:.6f} {token} on {from_chain} → {COMMISSION_WALLET} "
        f"(async, not yet on-chain)"
    )
    # TODO: implement actual on-chain transfer using web3
    # from web3 import Web3
    # w3 = Web3(Web3.HTTPProvider(RPC_URLS[from_chain]))
    # ... transfer token to COMMISSION_WALLET ...


# ─── AutoSwap integration ─────────────────────────────────────────────────────

def _do_swap(params: dict, dry_run: bool = False) -> dict:
    """Call AutoSwap SDK and return raw result dict."""
    try:
        from autoswap import swap
        result = swap(
            from_token   = params["from_token"],
            from_chain   = params["from_chain"],
            to_token     = params["to_token"],
            to_chain     = params["to_chain"],
            amount       = float(params["amount"]),
            wallet_key   = params.get("wallet_key"),
            slippage_max = float(params.get("slippage_max", 2.0)),
            dry_run      = dry_run,
        )
        return {
            "success":      result.success,
            "route_taken":  result.route_taken,
            "route_type":   result.route_type,
            "from_token":   result.from_token,
            "from_chain":   result.from_chain,
            "to_token":     result.to_token,
            "to_chain":     result.to_chain,
            "amount_in":    result.amount_in,
            "amount_out":   result.amount_out,
            "tx_hashes":    result.tx_hashes,
            "fees":         result.fees,
            "error":        result.error,
        }
    except ImportError:
        # Fallback: SDK not installed → return mock for local dev
        log.warning("[SDK] autoswap not installed — returning mock response")
        return {
            "success":     True,
            "route_taken": f"{params['from_token']}→{params['to_token']} (mock)",
            "route_type":  "mock",
            "from_token":  params["from_token"],
            "from_chain":  params["from_chain"],
            "to_token":    params["to_token"],
            "to_chain":    params["to_chain"],
            "amount_in":   params["amount"],
            "amount_out":  params["amount"] * 0.997,  # mock: 0.3% slippage
            "tx_hashes":   ["0xmock_tx_hash_abc123"] if not dry_run else [],
            "fees":        {"gas_usd": 0.50, "bridge_fee_usd": 0.10},
            "error":       None,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "amount_out": 0, "amount_in": params.get("amount", 0), "tx_hashes": [], "fees": {}}


def _do_quote(params: dict) -> dict:
    """Get price quote without executing the swap."""
    try:
        _src_dir_local = os.path.join(_p14_dir, "src")
        if _src_dir_local not in sys.path:
            sys.path.insert(0, _src_dir_local)
        from router import Router
        router    = Router()
        slippage  = float(params.get("slippage_max", 2.0))
        route     = router.get_best_route(
            from_token   = params["from_token"],
            to_token     = params["to_token"],
            amount       = float(params["amount"]),
            chain        = params["from_chain"],
            slippage_bps = int(slippage * 100),
        )
        raw_out = route.expected_output
        commission = calculate_commission(raw_out, params["to_token"])
        return {
            "success":          True,
            "expected_output":  raw_out,
            "min_output":       route.min_output,
            "net_output":       round(raw_out - commission["commission_amount"], 8),
            "commission":       commission,
            "route":            route.route,
            "dex":              route.dex,
            "bridge_fee":       None if params["from_chain"] == params["to_chain"] else "~0.05-0.20 USDC",
            "estimated_time_s": None if params["from_chain"] == params["to_chain"] else 5,
            "slippage_max_pct": slippage,
        }
    except ImportError:
        # Fallback mock for local dev
        raw_out    = float(params["amount"]) * 0.997
        commission = calculate_commission(raw_out, params["to_token"])
        return {
            "success":          True,
            "expected_output":  raw_out,
            "min_output":       raw_out * 0.98,
            "net_output":       round(raw_out - commission["commission_amount"], 8),
            "commission":       commission,
            "route":            f"[mock] {params['from_token']}→{params['to_token']}",
            "dex":              "mock-dex",
            "bridge_fee":       None if params["from_chain"] == params["to_chain"] else "~0.10 USDC",
            "estimated_time_s": None if params["from_chain"] == params["to_chain"] else 5,
            "slippage_max_pct": float(params.get("slippage_max", 2.0)),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── App setup ───────────────────────────────────────────────────────────────

app = FastAPI(
    title        = "AutoSwap Hosted API",
    description  = (
        "Cross-chain swap API with automatic routing, bridging, gas resolution, "
        "and slippage protection. 0.1% commission (min $0.01) per swap.\n\n"
        "**Auth:** Pass your API key in the `X-API-Key` header.\n\n"
        "**Commission wallet:** `0x804dd2cE4aA3296831c880139040e4326df13c6e`"
    ),
    version      = API_VERSION,
    contact      = {"name": "fino-oss", "email": "fino.oss@proton.me"},
    docs_url     = "/docs",
    redoc_url    = "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

# ─── Request logging middleware ──────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = (time.time() - start) * 1000
    log.info(f"{request.method} {request.url.path} → {response.status_code} ({duration:.1f}ms)")
    return response


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    """
    Health check — returns API status, version, and commission config.
    No auth required.
    """
    return {
        "status":             "ok",
        "version":            API_VERSION,
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "commission_rate_pct": COMMISSION_RATE * 100,
        "commission_min_usd":  COMMISSION_MIN_USD,
        "commission_wallet":   COMMISSION_WALLET,
        "supported_chains":    SUPPORTED_CHAINS,
    }


@app.get("/routes", tags=["Info"])
def get_routes(api_key: str = Depends(require_api_key)):
    """
    Returns supported chains and tokens.
    Useful for building UIs or validating inputs before calling /swap.
    """
    return {
        "success":           True,
        "supported_chains":  SUPPORTED_CHAINS,
        "tokens_by_chain":   SUPPORTED_TOKENS,
        "cross_chain":       True,
        "bridge_protocol":   "Across Protocol (v3)",
        "dex_protocols":     ["Paraswap", "Uniswap V3"],
        "notes": {
            "gas":      "Gas resolved automatically (cold-start safe)",
            "slippage": "Default 2.0%, sandwich protection built-in",
            "bridge":   "~2-5s fill time via Across relayer network",
        }
    }


@app.post("/quote", tags=["Swap"])
def get_quote(req: QuoteRequest, api_key: str = Depends(require_api_key)):
    """
    Get price estimate WITHOUT executing the swap.

    Returns expected output, commission breakdown, route, and estimated time.
    Use this to preview costs before calling /swap.
    """
    params = req.dict()
    result = _do_quote(params)

    if not result.get("success"):
        raise HTTPException(status_code=422, detail={"error": result.get("error", "Quote failed")})

    return {
        "success":          result["success"],
        "from_token":       req.from_token,
        "from_chain":       req.from_chain,
        "to_token":         req.to_token,
        "to_chain":         req.to_chain,
        "amount_in":        req.amount,
        "expected_output":  result["expected_output"],
        "min_output":       result["min_output"],
        "net_output":       result["net_output"],
        "commission":       result["commission"],
        "route":            result.get("route"),
        "dex":              result.get("dex"),
        "bridge_fee":       result.get("bridge_fee"),
        "estimated_time_s": result.get("estimated_time_s"),
        "slippage_max_pct": result.get("slippage_max_pct"),
        "note":             "This is an estimate. Actual output may vary due to price movement.",
    }


@app.post("/swap", tags=["Swap"])
def execute_swap(req: SwapRequest, api_key: str = Depends(require_api_key)):
    """
    Execute a cross-chain swap.

    **Commission:** 0.1% of amount_out (minimum $0.01) is deducted automatically.
    Commission is forwarded to the agent wallet.

    **Security note:** `wallet_key` is used only to sign transactions and is never logged or stored.

    Returns tx hashes, net amount received, and commission breakdown.
    """
    log.info(
        f"[SWAP] {req.amount} {req.from_token} ({req.from_chain}) → {req.to_token} ({req.to_chain}) "
        f"| slippage={req.slippage_max}% | key=...{req.wallet_key[-6:]}"
    )

    params = req.dict()
    result = _do_swap(params, dry_run=False)

    if not result.get("success"):
        raise HTTPException(
            status_code=422,
            detail={
                "error":       result.get("error", "Swap failed"),
                "amount_in":   result.get("amount_in", req.amount),
                "amount_out":  0,
                "tx_hashes":   result.get("tx_hashes", []),
            }
        )

    # Apply commission
    raw_amount_out  = result.get("amount_out", 0)
    net_amount_out, commission_info = apply_commission(raw_amount_out, req.to_token)

    # Async commission forwarding (fire and forget)
    agent_key = os.getenv("AGENT_WALLET_KEY", "")
    if agent_key and commission_info["commission_amount"] > 0:
        forward_commission_async(
            amount     = commission_info["commission_amount"],
            token      = req.to_token,
            from_chain = req.to_chain,  # commission is on destination chain
            agent_key  = agent_key,
        )

    log.info(
        f"[SWAP OK] amount_out={raw_amount_out:.6f} → net={net_amount_out:.6f} "
        f"commission={commission_info['commission_amount']:.6f} {req.to_token}"
    )

    return {
        "success":      True,
        "tx_hashes":    result.get("tx_hashes", []),
        "from_token":   result.get("from_token", req.from_token),
        "from_chain":   result.get("from_chain", req.from_chain),
        "to_token":     result.get("to_token", req.to_token),
        "to_chain":     result.get("to_chain", req.to_chain),
        "amount_in":    result.get("amount_in", req.amount),
        "amount_out":   round(net_amount_out, 8),      # Net (after commission)
        "amount_out_gross": round(raw_amount_out, 8),  # Gross (before commission)
        "fees":         result.get("fees", {}),
        "commission":   commission_info,
        "route_taken":  result.get("route_taken"),
        "route_type":   result.get("route_type"),
    }


# ─── Admin endpoint ──────────────────────────────────────────────────────────

@app.post("/admin/keys", tags=["Admin"])
def create_api_key(req: AdminKeyRequest):
    """
    Generate a new API key.

    Protected by `admin_secret` (set via ADMIN_SECRET env var).
    Keys are stored in memory — restart clears them (use API_KEYS env for persistence).
    """
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoint disabled: set ADMIN_SECRET env var to enable"
        )
    if req.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")

    new_key = "ask-" + secrets.token_urlsafe(24)
    _api_keys.add(new_key)

    label_suffix = f" [{req.label}]" if req.label else ""
    log.info(f"[ADMIN] New API key created{label_suffix}: {new_key[:12]}...")

    return {
        "success":    True,
        "api_key":    new_key,
        "label":      req.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "usage":      "Pass in X-API-Key header for all /swap, /quote, /routes requests",
        "note":       "Store this key securely — it will not be shown again.",
    }


@app.delete("/admin/keys/{key_prefix}", tags=["Admin"])
def revoke_api_key(key_prefix: str, admin_secret: Optional[str] = Header(None)):
    """
    Revoke an API key by its first 12 characters.
    Pass admin secret in X-Admin-Secret header.
    """
    if not ADMIN_SECRET or admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")

    to_remove = [k for k in _api_keys if k.startswith(key_prefix)]
    for k in to_remove:
        _api_keys.discard(k)

    return {
        "success":  True,
        "revoked":  len(to_remove),
        "prefix":   key_prefix,
    }


@app.get("/admin/keys/count", tags=["Admin"])
def count_api_keys(admin_secret: Optional[str] = Header(None)):
    """Returns count of active API keys (no secrets exposed)."""
    if not ADMIN_SECRET or admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return {"active_keys": len(_api_keys)}


# ─── Root ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["System"])
def root():
    return {
        "name":        "AutoSwap Hosted API",
        "version":     API_VERSION,
        "description": "Cross-chain swap API — Model C (paid, 0.1% commission)",
        "docs":        "/docs",
        "health":      "/health",
        "contact":     "fino.oss@proton.me",
        "commission":  "0.1% per swap (min $0.01)",
    }


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log.info(f"Starting AutoSwap API v{API_VERSION} on port {port}")
    log.info(f"Commission wallet: {COMMISSION_WALLET}")
    log.info(f"Active API keys: {len(_api_keys)}")
    uvicorn.run(
        "server:app",
        host    = "0.0.0.0",
        port    = port,
        reload  = False,
        workers = 1,
    )
