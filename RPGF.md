# AutoSwap — Public Good Documentation (RPGF)

## What is AutoSwap?

AutoSwap is an open-source SDK that makes cross-chain DeFi accessible to AI agents and developers. It wraps routing, bridging, gas resolution, and slippage protection into a single function call.

```python
from autoswap import swap
result = swap("ETH", "base", "MYST", "polygon", 0.003, dry_run=True)
```

## Why AutoSwap is a Public Good

### 1. Open Source (MIT License)
- Free to use, modify, and redistribute
- No API key required — works with public DEX APIs (Paraswap, Uniswap V3) and Across Protocol
- No vendor lock-in

### 2. Solves a Real Infrastructure Problem
The "cold start gas problem" — when tokens arrive cross-chain but are unusable because there's no native gas — is a well-known but unsolved failure mode. AutoSwap resolves it automatically.

The sandwich attack vector from `amountOutMin=0` affects millions of on-chain transactions. AutoSwap eliminates it by design.

### 3. Enables Permissionless DeFi for AI Agents
AI agents (Claude, GPT-4, Cursor, etc.) currently cannot execute DeFi operations without complex manual setup. AutoSwap's MCP integration makes this possible for the first time with zero configuration overhead.

### 4. Deployed on Optimism Ecosystem Chains
AutoSwap supports:
- **Base** (Coinbase L2, built on OP Stack) — ✅ Full support
- **Optimism** — ✅ Full support  
- **Arbitrum** — ✅ Full support
- **Polygon** — ✅ Full support

## Impact Metrics (to be updated)

| Metric | Target (30 days) | Current |
|--------|-----------------|---------|
| npm downloads/month | 100+ | (tracking) |
| PyPI downloads/month | 100+ | (tracking) |
| GitHub stars | 50+ | (tracking) |
| MCP tool uses | 10/day | (tracking) |
| Dev.to views | 1,000+ | (tracking) |

## Grant Applications

### Optimism RetroPGF
- **Relevant round**: Retro Funding (monitor https://app.optimism.io/retropgf)
- **Category**: Developer tooling
- **Justification**: Reduces friction for DeFi access on Base/Optimism; open-source; no API key needed

### Base Grants
- **URL**: https://paragraph.xyz/@base/grants
- **Category**: Developer ecosystem tooling
- **Justification**: Base-first tool; enables AI agents to use DeFi on Base

### Gitcoin Grants (Fallback)
- **URL**: https://grants.gitcoin.co
- **Category**: Open Source Software / DeFi tooling

## GitHub Repository
https://github.com/fino-oss/autoswap

## Author
fino-oss — fino.oss@proton.me
