# AutoSwap 🔄

**Cross-chain swaps in one line — built for AI agents.**

[![PyPI version](https://badge.fury.io/py/autoswap.svg)](https://badge.fury.io/py/autoswap)
[![npm version](https://badge.fury.io/js/autoswap.svg)](https://badge.fury.io/js/autoswap)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## The problem

Doing a cross-chain swap today (e.g. ETH on Base → MYST on Polygon) requires:
1. Finding the best route across DEXes
2. Handling native gas on the destination chain (cold start problem)
3. Protecting against slippage and sandwich attacks
4. Managing bridge failures, provider rate limits, tx reverts

**No existing tool solves this in a single call.** AutoSwap does.

---

## Quick Start

### Python (1 line)

```python
from autoswap import swap

result = swap(
    from_token="ETH",
    from_chain="base",
    to_token="MYST",
    to_chain="polygon",
    amount=0.003,
    slippage_max=2.0,
    dry_run=True,  # simulate first — always
)

print(result.route_taken)
# → "ETH→USDC (base) | bridge USDC base→polygon | USDC→MYST (polygon)"

print(f"~{result.amount_out:.4f} MYST")
# → "~47.3281 MYST"
```

### JavaScript (1 line)

```js
const { swap } = require('autoswap');

const result = await swap({
  fromToken: 'ETH',
  fromChain: 'base',
  toToken: 'MYST',
  toChain: 'polygon',
  amount: 0.003,
  slippageMax: 2.0,
  dryRun: true,
});

console.log(result.routeTaken);
// → "ETH→USDC (base) | bridge USDC base→polygon | USDC→MYST (polygon)"
console.log(`~${result.amountOut} MYST`);
```

### CLI

```bash
# Simulate a cross-chain swap
autoswap --from ETH --from-chain base --to MYST --to-chain polygon --amount 0.003 --dry-run

# Get a price quote (fastest, no wallet needed)
autoswap quote --from ETH --from-chain base --to USDC --to-chain base --amount 0.001

# Live swap (reads ETH_PRIVATE_KEY from environment)
autoswap --from USDC --from-chain base --to USDC --to-chain polygon --amount 10.0
```

---

## Installation

### Python

```bash
pip install autoswap
```

### JavaScript / Node.js

```bash
npm install autoswap
```

### MCP Tool (Claude / Cursor / Cline)

Add to your MCP config (`.cursor/mcp.json` or Claude Desktop config):

```json
{
  "mcpServers": {
    "autoswap": {
      "command": "python3",
      "args": ["-m", "autoswap.mcp_handler"],
      "env": {
        "ETH_PRIVATE_KEY": "your_private_key_here"
      }
    }
  }
}
```

Then Claude/Cursor can call `autoswap.swap(...)` directly.

---

## Supported Chains

| Chain     | Chain ID | Native Token | Status |
|-----------|----------|-------------|--------|
| Base      | 8453     | ETH         | ✅ Supported |
| Polygon   | 137      | POL (MATIC) | ✅ Supported |
| Arbitrum  | 42161    | ETH         | ✅ Supported |
| Optimism  | 10       | ETH         | ✅ Supported |
| Ethereum  | 1        | ETH         | 🔜 Coming soon |

### Supported Bridge Routes

All combinations of: Base ↔ Polygon ↔ Arbitrum ↔ Optimism

Bridgeable tokens: **USDC** (primary), **WETH**

---

## Supported Tokens

| Token    | Base | Polygon | Arbitrum | Optimism |
|----------|------|---------|----------|----------|
| ETH/WETH | ✅   | ✅      | ✅       | ✅       |
| USDC     | ✅   | ✅      | ✅       | ✅       |
| DAI      | ✅   | ✅      | ✅       | ✅       |
| USDT     | ❌   | ✅      | ✅       | ✅       |
| MYST     | ❌   | ✅      | ❌       | ❌       |
| POL/MATIC| ❌   | ✅      | ❌       | ❌       |

---

## Full API Reference

### Python: `swap()`

```python
from autoswap import swap, SwapResult

result: SwapResult = swap(
    from_token="ETH",       # Input token symbol (case-insensitive)
    from_chain="base",      # Source chain
    to_token="MYST",        # Output token symbol
    to_chain="polygon",     # Destination chain
    amount=0.003,           # Amount in human units (NOT wei)
    wallet_key=None,        # 0x... private key, or reads ETH_PRIVATE_KEY from env
    slippage_max=2.0,       # Max slippage % (default: 2.0)
    dry_run=False,          # True = simulate only (default: False)
)
```

**SwapResult fields:**

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether the swap succeeded |
| `dry_run` | bool | Whether this was a simulation |
| `route_taken` | str | Human-readable route (e.g. "ETH→USDC (base) \| bridge \| USDC→MYST (polygon)") |
| `route_type` | str | `"same_chain"` \| `"direct_bridge"` \| `"swap_bridge_swap"` |
| `from_token` | str | Input token symbol |
| `from_chain` | str | Source chain |
| `to_token` | str | Output token symbol |
| `to_chain` | str | Destination chain |
| `amount_in` | float | Amount sent |
| `amount_out` | float | Estimated (dry-run) or actual (live) output |
| `tx_hashes` | list[str] | Transaction hashes (empty in dry-run) |
| `fees` | dict | Fee breakdown: `{bridge_fee, gas_step1, ...}` |
| `steps` | list[StepResult] | Step-by-step breakdown |
| `error` | str \| None | Error message if failed |

### JavaScript: `swap(options)`

```js
const { swap, quote } = require('autoswap');

// Swap
const result = await swap({
  fromToken: 'ETH',         // Input token
  fromChain: 'base',        // Source chain
  toToken: 'MYST',          // Output token
  toChain: 'polygon',       // Destination chain
  amount: 0.003,            // Amount in human units
  slippageMax: 2.0,         // Max slippage % (default: 2.0)
  walletKey: '0x...',       // Optional; reads ETH_PRIVATE_KEY from env
  dryRun: true,             // Simulate (default: false)
  timeoutMs: 60000,         // Timeout in ms (default: 60000)
});

// Quote only (fast, no wallet needed)
const q = await quote({
  fromToken: 'ETH', fromChain: 'base',
  toToken: 'USDC', toChain: 'base',
  amount: 0.001,
});
console.log(`Expected: ${q.expectedOutput} USDC`);
console.log(`Min out: ${q.minOutput} USDC`);
```

---

## Security

AutoSwap is built with security as a first principle:

### Slippage Protection
- **Never sets `amountOutMin=0`** — the most common source of MEV exploits
- Slippage is calculated against real liquidity, not a fixed percentage of gas
- Default slippage: **2%** (good for most pairs). Adjust down for stable/stable swaps
- Hard maximum: **50%** — any route with >50% slippage is rejected

### Sandwich Attack Detection
AutoSwap detects and blocks suspicious trades:
```python
# This will raise SafetyError if sandwich risk is detected
result = swap("ETH", "base", "MYST", "polygon", 0.003)
# → SafetyError: "Sandwich risk CRITICAL: amountOutMin ≈ 0 on ETH→USDC"
```

Risk levels:
- `LOW` — normal, no action
- `MEDIUM` — logs a warning
- `HIGH` — logs a warning, consider reducing amount or increasing slippage
- `CRITICAL` — blocks the swap entirely

### Gas Safety (Cold Start Problem)
AutoSwap automatically detects when you have no native gas on the destination chain and resolves it via a gasless relay. This prevents a common failure mode where a bridged token arrives but can't be used because there's no ETH/POL for gas.

### Private Key Handling
- **Never hardcode private keys** — use environment variables
- AutoSwap reads `ETH_PRIVATE_KEY` from your environment
- Compatible with [agent-vault](https://github.com/fino-oss/agent-vault) for encrypted key storage
- In dry-run mode, **no key is needed** — safe to use in CI/CD

### Environment Variable Setup
```bash
export ETH_PRIVATE_KEY="0x..."   # Python/CLI
```
```js
// Node.js — set before importing autoswap
process.env.ETH_PRIVATE_KEY = '0x...';
```

---

## How AutoSwap Works

### Routing Algorithm
1. **Query Paraswap** — aggregator covering 50+ DEXes, best price in most cases
2. **Query Uniswap V3** — direct on-chain quote as fallback/comparison
3. **Pick the best** — highest `expectedOutput` wins
4. **Validate** — safety checks before building transaction

### Cross-Chain Bridge (Across Protocol)
- **~2-5 second fills** via exclusive relayers
- USDC is the default bridge token (deepest liquidity)
- Automatic fallback if primary bridge route fails

### Route Types

| Scenario | Route |
|----------|-------|
| ETH → USDC (same chain) | `swap via Paraswap` |
| USDC Base → USDC Polygon | `direct bridge via Across` |
| ETH Base → MYST Polygon | `ETH→USDC (Base) \| bridge \| USDC→MYST (Polygon)` |
| ETH Base → USDC Polygon | `ETH→USDC (Base) \| bridge USDC` |

---

## Comparison with Competitors

| Feature | AutoSwap | Relay.link | Li.Fi | 1inch Fusion |
|---------|----------|------------|-------|-------------|
| **Single function call** | ✅ | ❌ (manual steps) | ❌ (SDK setup) | ❌ (API key required) |
| **Cold start gas fix** | ✅ **automatic** | ❌ manual | ❌ manual | ❌ not addressed |
| **Sandwich protection** | ✅ built-in | ⚠️ partial | ⚠️ partial | ✅ |
| **amountOutMin=0 guard** | ✅ always | ⚠️ user's responsibility | ⚠️ user's responsibility | ✅ |
| **AI agent / MCP ready** | ✅ native | ❌ | ❌ | ❌ |
| **Dry-run simulation** | ✅ | ❌ | ⚠️ |  ❌ |
| **Open source** | ✅ MIT | ❌ | ✅ GPL | ❌ |
| **No API key needed** | ✅ | ❌ | ❌ | ❌ |
| **Slippage auto-calc** | ✅ | ⚠️ fixed | ⚠️ fixed | ✅ |
| **Retry + fallback** | ✅ | ❌ | ✅ | ❌ |

**TL;DR:** AutoSwap is the only swap SDK designed for AI agents from the ground up. It handles every failure mode automatically — including the cold start gas problem that derails other tools.

---

## MCP Tool Integration

AutoSwap exposes itself as an MCP (Model Context Protocol) tool, letting Claude, Cursor, and Cline execute cross-chain swaps directly.

### Available MCP Tools

1. **`autoswap.swap`** — Full swap execution (live or dry-run)
2. **`autoswap.swap_quote`** — Price quote only (fast, no wallet needed)

### Example (Claude calling AutoSwap via MCP)

```
User: "Swap 0.003 ETH on Base to MYST on Polygon, show me the route first"

Claude: [calls autoswap.swap with dry_run=true]
→ Route: ETH→USDC (base) | bridge USDC base→polygon | USDC→MYST (polygon)
→ Expected output: ~47.3 MYST
→ Steps: 3 (swap, bridge, swap)
→ Bridge fee: 0.08 USDC (~$0.08)
→ Estimated time: ~5 seconds

"Looks good. Execute it."

Claude: [calls autoswap.swap with dry_run=false]
→ TX 1: 0xabc... (ETH→USDC, confirmed)
→ TX 2: 0xdef... (bridge deposit, confirmed)
→ TX 3: 0x123... (USDC→MYST, confirmed)
→ Received: 47.1 MYST
```

### MCP Config (mcp-tool.json)

See [`mcp-tool.json`](./mcp-tool.json) for the full tool descriptor. This file is designed to be listed in MCP tool directories.

---

## Examples

### Dry-run then live (Python)

```python
from autoswap import swap

# Step 1: Always simulate first
result = swap("ETH", "base", "MYST", "polygon", 0.003, dry_run=True)
result.print_summary()

# Check the route makes sense
print(f"Route: {result.route_taken}")
print(f"Expected output: {result.amount_out:.4f} MYST")
print(f"Steps: {len(result.steps)}")
print(f"Bridge fee: {result.fees.get('bridge_fee', 0):.4f} USDC")

# Step 2: Execute live
if result.success and result.amount_out > 40:  # safety check
    live_result = swap("ETH", "base", "MYST", "polygon", 0.003, dry_run=False)
    if live_result.success:
        print(f"✅ Received {live_result.amount_out:.4f} MYST")
        print(f"TX hashes: {live_result.tx_hashes}")
```

### Same-chain swap

```python
from autoswap import swap

# ETH → USDC on Base
result = swap("ETH", "base", "USDC", "base", 0.001, dry_run=True)
print(f"~{result.amount_out:.2f} USDC")
```

### Direct bridge

```python
from autoswap import swap

# USDC Base → USDC Polygon (no swaps, just bridge)
result = swap("USDC", "base", "USDC", "polygon", 10.0, dry_run=True)
print(f"Route type: {result.route_type}")  # "direct_bridge"
print(f"Bridge fee: {result.fees.get('bridge_fee', 0):.4f} USDC")
```

### JSON output for AI agents (CLI)

```bash
autoswap --from ETH --from-chain base --to MYST --to-chain polygon \
         --amount 0.003 --dry-run --json | jq '.routeTaken'
```

### Error handling (Python)

```python
from autoswap import swap, SwapError
from autoswap import SafetyError

try:
    result = swap("ETH", "base", "MYST", "polygon", 0.003)
except SafetyError as e:
    print(f"Safety blocked: {e}")  # sandwich attack detected
except SwapError as e:
    print(f"Swap failed: {e}")     # route not found, etc.
```

---

## Development

### Setup

```bash
git clone https://github.com/fino-oss/autoswap
cd autoswap
pip install -e ".[dev]"
```

### Run tests

```bash
pytest tests/ -v
```

Tests run in **dry-run mode** — no real transactions.

### Project structure

```
autoswap/
├── autoswap/          ← Python package (pip installable)
│   ├── __init__.py    ← Public API: swap(), SwapResult
│   ├── cli.py         ← CLI entry point
│   └── mcp_handler.py ← MCP subprocess handler
├── src/               ← Core implementation
│   ├── swap.py        ← Orchestrator
│   ├── router.py      ← DEX routing (Paraswap + Uniswap V3)
│   ├── bridge.py      ← Cross-chain bridge (Across Protocol)
│   ├── gas.py         ← Native gas resolver
│   └── safety.py      ← Slippage + sandwich protection
├── npm/               ← JavaScript wrapper (npm package)
│   ├── package.json
│   ├── src/index.js   ← JS API: swap(), quote()
│   └── bin/autoswap.js ← CLI
├── tests/             ← Dry-run tests
├── mcp-tool.json      ← MCP tool descriptor
├── pyproject.toml     ← PyPI config
└── README.md
```

---

## Roadmap

- [x] Phase 1: Python SDK (routing + bridge + gas + safety)
- [x] Phase 2: MCP tool + npm package + PyPI publish
- [x] Phase 3: Hosted API (`POST https://api.autoswap.dev/swap`) ✅
- [ ] Phase 4: Arbitrum + Optimism full support
- [ ] Phase 5: Additional tokens (AAVE, LINK, UNI, ...)
- [ ] Phase 6: Solana cross-chain support

---

## Contributing

PRs welcome. Run tests before submitting:

```bash
pytest tests/ -v --tb=short
```

---

## License

MIT — see [LICENSE](./LICENSE).

Built from real pain. Swap 0.003 ETH to MYST, 5 bugs, Sam manually sending POL.
Now it's one line.
