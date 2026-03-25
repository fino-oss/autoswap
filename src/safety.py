"""
safety.py — Swap Security Module for AutoSwap (p14)

Prevents the real errors observed during p13 operations (2026-03-25):
  - amountOutMinimum=0 → sandwich attack (ETH→USDC swap returned "0 gained" in logs
    despite tx success — because the fallback set amountOutMin=0 silently)
  - Wrong Uniswap fee tier (used 3000 instead of 500 → tx revert on Base)
  - Approval on wrong contract (approving router instead of actual spender)

Core rule: NO swap may proceed with amountOutMinimum = 0.
If quoting fails, REFUSE the swap rather than risk a sandwich.

Usage:
    from safety import (
        calc_min_output,
        validate_route,
        check_approval,
        estimate_slippage,
        detect_sandwich_risk,
        SafetyError,
        SandwichRiskLevel,
    )

    # 1. Calculate safe minimum output
    min_out = calc_min_output(quoted_usdc=5_000_000, slippage_bps=200)
    # → 4_900_000  (2% slippage on 5 USDC, never below 50% of quoted)

    # 2. Validate a swap route before execution
    route = {
        "token_in":  "0x4200000000000000000000000000000000000006",  # WETH
        "token_out": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        "fee":       500,
        "chain":     "base",
        "router":    "0x2626664c2603336E57B271c5C0b26F421741e481",
    }
    errors = validate_route(route)  # [] = OK, list of strings = issues

    # 3. Check ERC-20 approval before swap
    ok = check_approval(
        token="0x833589...",     # USDC on Base
        spender="0x2626664...",  # SwapRouter02
        amount=5_000_000,        # raw units
        wallet="0xYourWallet",
        chain="base",
    )

    # 4. Estimate expected slippage for an amount
    slippage_pct = estimate_slippage(
        token_pair=("WETH", "USDC"),
        amount=0.003,   # ETH
        chain="base",
    )

    # 5. Detect sandwich risk in tx params
    risk = detect_sandwich_risk({
        "amountOutMinimum": 0,
        "amountOutQuoted":  4_900_000,
        "token_in":  "WETH",
        "token_out": "USDC",
    })
    # → SandwichRisk(level=CRITICAL, reason="amountOutMinimum is 0 — sandwich attack possible")
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from web3 import Web3

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Absolute minimum slippage floor: 0.1% (to handle tx inclusion delay on L2)
SLIPPAGE_MIN_BPS: int = 10

# Maximum allowed slippage cap (safety ceiling): 5%
SLIPPAGE_MAX_BPS: int = 500

# Minimum amountOutMinimum as a fraction of quoted output (50% = 5000 bps)
# If our calc somehow produces less than 50% of quoted, hard-reject.
MIN_OUTPUT_FLOOR_BPS: int = 5000  # 50%

# Sandwich risk threshold: if amountOutMin < X% of quoted → CRITICAL
SANDWICH_CRITICAL_THRESHOLD_BPS: int = 200    # <2% of quoted = critical
SANDWICH_HIGH_THRESHOLD_BPS: int = 500        # <5% of quoted = high
SANDWICH_MEDIUM_THRESHOLD_BPS: int = 1000     # <10% of quoted = medium

# Uniswap V3 valid fee tiers (in bps * 100, i.e. fee = 100 means 0.01%)
UNISWAP_VALID_FEE_TIERS = {100, 500, 3000, 10000}

# Known token registries per chain — addresses MUST be checksummed
TOKEN_REGISTRY: Dict[str, Dict[str, str]] = {
    "base": {
        "WETH":  "0x4200000000000000000000000000000000000006",
        "USDC":  "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDbC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",  # bridged USDC (deprecated)
        "DAI":   "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
        "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
        "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    },
    "polygon": {
        "WMATIC": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        "USDC":   "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # Native USDC
        "USDCe":  "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # Bridged USDC.e
        "USDT":   "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "WETH":   "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
        "MYST":   "0x1379E8886A944d2D9d440b3d88DF536Aea08d9F3",
        "DAI":    "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
    },
    "arbitrum": {
        "WETH":  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "USDC":  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "USDCe": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
        "USDT":  "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "DAI":   "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    },
    "optimism": {
        "WETH":  "0x4200000000000000000000000000000000000006",
        "USDC":  "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "USDCe": "0x7F5c764cBc14f9669B88837ca1490cCa17c31607",
        "USDT":  "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        "OP":    "0x4200000000000000000000000000000000000042",
    },
    "ethereum": {
        "WETH":  "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "USDC":  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT":  "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "DAI":   "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        "WBTC":  "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    },
}

# Known safe spenders (router/aggregator contracts) per chain
# Approving anything OUTSIDE this list should generate a warning
KNOWN_SAFE_SPENDERS: Dict[str, Dict[str, str]] = {
    "base": {
        "Uniswap V3 SwapRouter02":   "0x2626664c2603336E57B271c5C0b26F421741e481",
        "Uniswap V3 SwapRouter (v1)": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        "Paraswap Augustus v6":       "0x6A000F20005980200259B80c5102003040001068",
        "1inch v5 Router":            "0x1111111254EEB25477B68fb85Ed929f73A960582",
        "Across SpokePool":           "0x09aea4b2242abC8bb4BB78D537A67a245A7bEC64",
    },
    "polygon": {
        "Uniswap V3 SwapRouter02":   "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "Paraswap Augustus v6":       "0x6A000F20005980200259B80c5102003040001068",
        "1inch v5 Router":            "0x1111111254EEB25477B68fb85Ed929f73A960582",
        "Across SpokePool":           "0x9295ee1d8C5b022Be115A2AD3c30C72E34e7F096",
    },
    "arbitrum": {
        "Uniswap V3 SwapRouter02":   "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "Paraswap Augustus v6":       "0x6A000F20005980200259B80c5102003040001068",
        "1inch v5 Router":            "0x1111111254EEB25477B68fb85Ed929f73A960582",
    },
    "optimism": {
        "Uniswap V3 SwapRouter02":   "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "Paraswap Augustus v6":       "0x6A000F20005980200259B80c5102003040001068",
        "1inch v5 Router":            "0x1111111254EEB25477B68fb85Ed929f73A960582",
    },
}

# Known best Uniswap fee tiers per pair (from p13 experience: 3000 failed on Base ETH/USDC)
# KEY: "{lower_symbol}/{upper_symbol}", VALUE: best fee tier
KNOWN_BEST_FEE_TIERS: Dict[str, int] = {
    "ETH/USDC":  500,    # 0.05% — highest liquidity on Base (p13: 3000 caused revert)
    "WETH/USDC": 500,
    "WETH/USDT": 500,
    "ETH/USDT":  500,
    "USDC/USDT": 100,    # 0.01% stablecoin pairs
    "DAI/USDC":  100,
    "DAI/USDT":  100,
    "WBTC/WETH": 3000,
    "WBTC/USDC": 3000,
}

# Minimal ERC-20 ABI for allowance/balanceOf
ERC20_ABI = [
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

# Chain RPC endpoints
CHAIN_RPC: Dict[str, str] = {
    "base":      "https://mainnet.base.org",
    "polygon":   "https://polygon.drpc.org",
    "arbitrum":  "https://arb1.arbitrum.io/rpc",
    "optimism":  "https://mainnet.optimism.io",
    "ethereum":  "https://eth.drpc.org",
}


# ─── Data Structures ──────────────────────────────────────────────────────────

class SafetyError(Exception):
    """
    Raised when a safety check fails and the swap MUST be blocked.
    Unlike warnings, a SafetyError means "do not proceed under any circumstances."
    """
    pass


class SandwichRiskLevel(str, Enum):
    """Risk classification for sandwich attack detection."""
    SAFE     = "safe"      # amountOutMin is reasonable
    MEDIUM   = "medium"    # amountOutMin is somewhat low (warn but allow)
    HIGH     = "high"      # amountOutMin is dangerously low (warn loudly, ask confirmation)
    CRITICAL = "critical"  # amountOutMin=0 or near-0 → BLOCK the swap


@dataclass
class SandwichRisk:
    """Result of detect_sandwich_risk()."""
    level:          SandwichRiskLevel
    reason:         str
    amount_out_min: int             # Raw amountOutMinimum provided
    amount_quoted:  Optional[int]   # Raw quoted output (if available)
    protection_pct: Optional[float] # % protection = amountOutMin/quoted * 100
    recommendation: str
    should_block:   bool            # True if swap must be refused


@dataclass
class ApprovalStatus:
    """Result of check_approval()."""
    is_sufficient:    bool        # True if allowance >= amount
    current_allowance: int        # Raw allowance
    required_amount:  int         # Raw amount needed
    token_address:    str
    spender_address:  str
    spender_name:     Optional[str]  # Human name if known safe spender
    is_known_spender: bool
    warning:          Optional[str]  # Non-None if spender is unknown


@dataclass
class RouteValidationResult:
    """Result of validate_route()."""
    is_valid:  bool
    errors:    List[str]           # Blocking issues → swap must be refused
    warnings:  List[str]          # Non-blocking issues → log and proceed
    suggested_fee_tier: Optional[int]  # If a better fee tier is known


@dataclass
class SlippageEstimate:
    """Result of estimate_slippage()."""
    estimated_bps:   int           # Estimated slippage in basis points
    estimated_pct:   float         # Same in percent
    confidence:      str           # "high" | "medium" | "low"
    basis:           str           # Explanation of how it was computed
    recommended_bps: int           # What we recommend setting as slippage_bps


# ─── 1. calc_min_output ───────────────────────────────────────────────────────

def calc_min_output(
    quoted_amount: int,
    slippage_bps: int = 200,
) -> int:
    """
    Calculate the safe minimum output amount for a swap.

    RULE: The result is NEVER 0.
    If quoted_amount is 0 or very small, raises SafetyError — refuse the swap.

    Args:
        quoted_amount:  The quoted output in raw token units (e.g., USDC raw = 6 decimals)
                        This comes from Uniswap QuoterV2 or a price aggregator.
        slippage_bps:   Maximum acceptable slippage in basis points.
                        100 bps = 1%. Default = 200 (2%).
                        Will be clamped to [SLIPPAGE_MIN_BPS, SLIPPAGE_MAX_BPS].

    Returns:
        int: Safe minimum output in the same raw units as quoted_amount.
             Always >= 1, always >= 50% of quoted_amount.

    Raises:
        SafetyError: If quoted_amount is 0 (cannot compute safe minimum → refuse swap).
        SafetyError: If slippage_bps > SLIPPAGE_MAX_BPS (too risky).

    Examples:
        >>> calc_min_output(5_000_000, slippage_bps=200)  # 5 USDC, 2% slippage
        4_900_000
        >>> calc_min_output(0, slippage_bps=200)
        SafetyError: quoted_amount is 0 — cannot compute safe minimum, refusing swap
    """
    # ── Guard: quoted_amount must be positive ────────────────────────────────
    if quoted_amount <= 0:
        raise SafetyError(
            f"quoted_amount is {quoted_amount} — cannot compute safe minimum output. "
            f"This likely means the quote failed. "
            f"REFUSING SWAP: never set amountOutMinimum=0 (p13 lesson: this caused "
            f"a sandwich attack where the ETH→USDC swap returned '0 gained' despite success)."
        )

    # ── Clamp slippage to safe range ─────────────────────────────────────────
    if slippage_bps > SLIPPAGE_MAX_BPS:
        raise SafetyError(
            f"slippage_bps={slippage_bps} exceeds maximum allowed "
            f"{SLIPPAGE_MAX_BPS} bps ({SLIPPAGE_MAX_BPS / 100:.1f}%). "
            f"This is too risky. Use a lower slippage or investigate why such "
            f"high slippage is needed."
        )

    if slippage_bps < SLIPPAGE_MIN_BPS:
        logger.warning(
            f"[safety] slippage_bps={slippage_bps} is below minimum {SLIPPAGE_MIN_BPS} bps. "
            f"Clamping to {SLIPPAGE_MIN_BPS} bps to avoid revert on tx inclusion delay."
        )
        slippage_bps = SLIPPAGE_MIN_BPS

    # ── Compute min output ───────────────────────────────────────────────────
    # min_output = quoted * (10000 - slippage_bps) / 10000
    # Use integer arithmetic to avoid float precision issues
    min_output = (quoted_amount * (10_000 - slippage_bps)) // 10_000

    # ── Apply absolute floor: never below 50% of quoted ─────────────────────
    floor_output = (quoted_amount * MIN_OUTPUT_FLOOR_BPS) // 10_000
    if min_output < floor_output:
        logger.warning(
            f"[safety] calc_min_output: computed min_output={min_output} is below "
            f"50% floor={floor_output} for quoted={quoted_amount}. "
            f"This implies slippage_bps={slippage_bps} > 50%. Clamping to 50% floor. "
            f"Consider reducing swap amount or choosing higher liquidity pool."
        )
        min_output = floor_output

    # ── Final guard: result must be at least 1 ───────────────────────────────
    if min_output <= 0:
        raise SafetyError(
            f"calc_min_output produced 0 even after floor clamping "
            f"(quoted={quoted_amount}, slippage_bps={slippage_bps}). "
            f"REFUSING SWAP to prevent sandwich attack."
        )

    logger.debug(
        f"[safety] calc_min_output: quoted={quoted_amount}, "
        f"slippage={slippage_bps}bps, min_output={min_output} "
        f"({min_output / quoted_amount * 100:.2f}% of quoted)"
    )

    return min_output


# ─── 2. validate_route ────────────────────────────────────────────────────────

def validate_route(route: Dict[str, Any]) -> RouteValidationResult:
    """
    Validate a swap route before execution.

    Checks:
      - token_in and token_out are valid checksummed addresses
      - fee tier is a valid Uniswap V3 tier (100, 500, 3000, 10000)
      - chain is supported
      - router address is in the known safe spenders list
      - token_in != token_out
      - warns if a better-known fee tier exists for this pair (p13: 3000→revert, should be 500)
      - warns if using deprecated tokens (USDbC on Base)

    Args:
        route: Dict with keys:
            - token_in:   Address of input token (checksummed or raw)
            - token_out:  Address of output token (checksummed or raw)
            - fee:        Uniswap V3 fee tier (int): 100 | 500 | 3000 | 10000
            - chain:      Chain name: "base" | "polygon" | "arbitrum" | "optimism" | "ethereum"
            - router:     (optional) Router/spender contract address
            - amount_in:  (optional) Amount in raw units (for sanity check)

    Returns:
        RouteValidationResult with errors (blocking) and warnings (non-blocking).
        If errors is non-empty, the swap MUST be refused.
    """
    errors: List[str] = []
    warnings: List[str] = []
    suggested_fee: Optional[int] = None

    # ── Check required fields ────────────────────────────────────────────────
    for required_key in ("token_in", "token_out", "fee", "chain"):
        if required_key not in route:
            errors.append(f"Missing required field: '{required_key}'")

    if errors:
        return RouteValidationResult(
            is_valid=False, errors=errors, warnings=warnings,
            suggested_fee_tier=None
        )

    chain      = route["chain"].lower()
    token_in   = route["token_in"]
    token_out  = route["token_out"]
    fee        = route["fee"]
    router     = route.get("router")

    # ── Chain validation ─────────────────────────────────────────────────────
    if chain not in CHAIN_RPC:
        errors.append(
            f"Unsupported chain: '{chain}'. "
            f"Supported: {list(CHAIN_RPC.keys())}"
        )

    # ── Address validation: checksum ─────────────────────────────────────────
    for field_name, addr in [("token_in", token_in), ("token_out", token_out)]:
        if not addr:
            errors.append(f"'{field_name}' address is empty")
            continue
        if not addr.startswith("0x") or len(addr) != 42:
            errors.append(
                f"'{field_name}' address '{addr[:20]}...' is not a valid Ethereum address "
                f"(must be 0x + 40 hex chars)"
            )
            continue
        try:
            checksummed = Web3.to_checksum_address(addr)
            if addr != checksummed and addr.lower() != checksummed.lower():
                errors.append(
                    f"'{field_name}' address '{addr}' has invalid checksum. "
                    f"Expected: '{checksummed}'"
                )
        except Exception as e:
            errors.append(f"'{field_name}' address is invalid: {e}")

    # ── token_in != token_out ────────────────────────────────────────────────
    if token_in and token_out:
        if token_in.lower() == token_out.lower():
            errors.append(
                f"token_in and token_out are the same address ({token_in}). "
                f"Cannot swap a token for itself."
            )

    # ── Fee tier validation ──────────────────────────────────────────────────
    if not isinstance(fee, int) or fee not in UNISWAP_VALID_FEE_TIERS:
        errors.append(
            f"Invalid Uniswap V3 fee tier: {fee}. "
            f"Valid tiers: {sorted(UNISWAP_VALID_FEE_TIERS)} "
            f"(100=0.01%, 500=0.05%, 3000=0.3%, 10000=1%)"
        )

    # ── Check for known better fee tier ─────────────────────────────────────
    # P13 LESSON: using fee=3000 on WETH/USDC Base caused a tx revert
    # The correct tier is 500 (0.05%) for ETH/USDC on Base
    if chain in CHAIN_RPC and token_in and token_out:
        token_in_symbol  = _resolve_symbol(token_in, chain)
        token_out_symbol = _resolve_symbol(token_out, chain)

        if token_in_symbol and token_out_symbol:
            pair_key = _canonical_pair_key(token_in_symbol, token_out_symbol)
            best_fee = KNOWN_BEST_FEE_TIERS.get(pair_key)

            if best_fee is not None and fee != best_fee and isinstance(fee, int):
                suggested_fee = best_fee
                warnings.append(
                    f"[P13 LESSON] Fee tier {fee} ({fee/10000:.2f}%) may not be optimal "
                    f"for {token_in_symbol}/{token_out_symbol} on {chain}. "
                    f"Known best fee tier: {best_fee} ({best_fee/10000:.2f}%). "
                    f"Using wrong tier caused tx revert in p13 (2026-03-25)."
                )

    # ── Deprecated token warning ─────────────────────────────────────────────
    if chain == "base" and token_out:
        usdc_e_base = "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"  # USDbC
        if token_out.lower() == usdc_e_base.lower():
            warnings.append(
                f"token_out is USDbC (0xd9aA...) — this is the deprecated bridged USDC on Base. "
                f"Consider using native USDC instead: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
            )

    # ── Router/spender validation ────────────────────────────────────────────
    if router:
        if not router.startswith("0x") or len(router) != 42:
            errors.append(
                f"router address '{router[:20]}...' is not a valid Ethereum address"
            )
        else:
            try:
                router_checksummed = Web3.to_checksum_address(router)
                safe_spenders = KNOWN_SAFE_SPENDERS.get(chain, {})
                safe_addresses_lower = {
                    addr.lower() for addr in safe_spenders.values()
                }
                if router_checksummed.lower() not in safe_addresses_lower:
                    warnings.append(
                        f"router '{router_checksummed}' is NOT in the known safe spenders list "
                        f"for chain '{chain}'. Known safe spenders: "
                        f"{list(safe_spenders.keys())}. "
                        f"Verify this is the correct contract before approving tokens to it."
                    )
            except Exception as e:
                errors.append(f"router address validation failed: {e}")

    is_valid = len(errors) == 0

    if errors:
        logger.error(f"[safety] Route validation FAILED: {errors}")
    if warnings:
        logger.warning(f"[safety] Route warnings: {warnings}")

    return RouteValidationResult(
        is_valid=is_valid,
        errors=errors,
        warnings=warnings,
        suggested_fee_tier=suggested_fee,
    )


# ─── 3. check_approval ────────────────────────────────────────────────────────

def check_approval(
    token:   str,
    spender: str,
    amount:  int,
    wallet:  str,
    chain:   str = "base",
    rpc:     Optional[str] = None,
) -> ApprovalStatus:
    """
    Check if the wallet has sufficient ERC-20 approval for `spender` to spend `amount`.

    Also validates that the spender is a known-safe contract and warns if not.

    Args:
        token:   Token contract address (checksummed or raw)
        spender: Address to check approval for (router, bridge, etc.)
        amount:  Raw token amount required
        wallet:  Wallet/owner address
        chain:   Chain name ("base", "polygon", etc.)
        rpc:     Optional RPC endpoint override

    Returns:
        ApprovalStatus with is_sufficient flag and current allowance.

    Raises:
        SafetyError: If token or spender addresses are invalid.

    Note:
        This does NOT sign or send any transaction.
        To fix insufficient approval, call ERC-20 approve() separately.
    """
    chain = chain.lower()

    # ── Validate addresses ───────────────────────────────────────────────────
    try:
        token_cs   = Web3.to_checksum_address(token)
        spender_cs = Web3.to_checksum_address(spender)
        wallet_cs  = Web3.to_checksum_address(wallet)
    except Exception as e:
        raise SafetyError(
            f"check_approval: invalid address provided — {e}. "
            f"token={token}, spender={spender}, wallet={wallet}"
        )

    # ── Identify spender ─────────────────────────────────────────────────────
    safe_spenders   = KNOWN_SAFE_SPENDERS.get(chain, {})
    spender_name    = None
    is_known        = False
    spender_warning = None

    for name, known_addr in safe_spenders.items():
        if known_addr.lower() == spender_cs.lower():
            spender_name = name
            is_known = True
            break

    if not is_known:
        spender_warning = (
            f"Spender '{spender_cs}' is NOT in the known safe contracts list for '{chain}'. "
            f"Approving an unknown contract may lead to loss of funds. "
            f"Known safe spenders: {list(safe_spenders.keys())}"
        )
        logger.warning(f"[safety] ⚠️  {spender_warning}")

    # ── Query on-chain allowance ─────────────────────────────────────────────
    rpc_url = rpc or CHAIN_RPC.get(chain)
    if not rpc_url:
        raise SafetyError(f"No RPC endpoint configured for chain '{chain}'")

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
        allowance = contract.functions.allowance(wallet_cs, spender_cs).call()
    except Exception as e:
        logger.warning(
            f"[safety] check_approval: RPC call failed for chain={chain}: {e}. "
            f"Returning is_sufficient=False to be safe (will require explicit approval)."
        )
        allowance = 0

    is_sufficient = allowance >= amount

    if is_sufficient:
        logger.debug(
            f"[safety] ✅ Allowance sufficient: {allowance} >= {amount} "
            f"for {spender_name or spender_cs} to spend {token_cs}"
        )
    else:
        logger.info(
            f"[safety] 🔴 Insufficient allowance: {allowance} < {amount} "
            f"— need to call approve({spender_cs}, {amount}) on {token_cs}"
        )

    return ApprovalStatus(
        is_sufficient=is_sufficient,
        current_allowance=allowance,
        required_amount=amount,
        token_address=token_cs,
        spender_address=spender_cs,
        spender_name=spender_name,
        is_known_spender=is_known,
        warning=spender_warning,
    )


# ─── 4. estimate_slippage ─────────────────────────────────────────────────────

def estimate_slippage(
    token_pair: Tuple[str, str],
    amount:     float,
    chain:      str = "base",
) -> SlippageEstimate:
    """
    Estimate expected slippage for a swap, based on pair liquidity knowledge.

    Uses a tiered model based on well-known token pairs and trade sizes.
    For unknown pairs or large sizes, returns a conservative estimate.

    NOTE: In Phase 1 this is a model-based estimate (no on-chain pool query).
    Phase 2 will add Uniswap V3 slot0 + liquidity queries for real-time data.

    Args:
        token_pair: Tuple of (token_in_symbol, token_out_symbol)
                    e.g., ("WETH", "USDC") or ("ETH", "USDC")
        amount:     Trade size in human units of token_in
                    e.g., 0.003 for 0.003 ETH
        chain:      Chain name ("base", "polygon", etc.)

    Returns:
        SlippageEstimate with recommended_bps to use as slippage protection.

    Recommended use:
        est = estimate_slippage(("WETH", "USDC"), 0.003, "base")
        min_out = calc_min_output(quoted_raw, est.recommended_bps)
    """
    chain = chain.lower()
    sym_in, sym_out = token_pair
    pair_key = _canonical_pair_key(sym_in, sym_out)

    # ── Tier 1: Stablecoin pairs — ultra-low slippage ────────────────────────
    STABLE_PAIRS = {"USDC/USDT", "DAI/USDC", "DAI/USDT", "USDC/USDCe",
                    "USDC/USDbC", "USDT/DAI"}
    if pair_key in STABLE_PAIRS:
        recommended = 30  # 0.3%
        return SlippageEstimate(
            estimated_bps=10,
            estimated_pct=0.1,
            confidence="high",
            basis="Stablecoin pair — very deep liquidity, minimal slippage",
            recommended_bps=recommended,
        )

    # ── Tier 2: Major pairs (ETH/USDC, WETH/USDC) — varies by size ─────────
    MAJOR_ETH_PAIRS = {"ETH/USDC", "WETH/USDC", "ETH/USDT", "WETH/USDT"}
    if pair_key in MAJOR_ETH_PAIRS:
        # Size-dependent estimation
        # At $2000/ETH: 0.003 ETH = ~$6, 0.01 ETH = ~$20, 0.1 ETH = ~$200
        if chain == "base":
            if amount <= 0.005:       # < ~$10 — negligible slippage
                est, rec = 5, 50      # 0.05% est, recommend 0.5%
                confidence = "high"
                basis = f"Small trade (<0.005 ETH) on {chain} — highest liquidity pool (fee 500)"
            elif amount <= 0.05:      # $10-$100
                est, rec = 15, 100    # 0.15% est, recommend 1%
                confidence = "high"
                basis = f"Medium trade (0.005–0.05 ETH) on {chain}"
            elif amount <= 0.5:       # $100-$1000
                est, rec = 30, 200    # 0.3% est, recommend 2%
                confidence = "medium"
                basis = f"Large trade (0.05–0.5 ETH) on {chain} — may have mild price impact"
            else:                     # >$1000
                est, rec = 100, 500   # 1% est, recommend 5%
                confidence = "low"
                basis = f"Very large trade (>0.5 ETH) on {chain} — significant price impact expected"
        else:
            # Other chains — slightly more conservative
            if amount <= 0.01:
                est, rec = 10, 100
                confidence = "medium"
            elif amount <= 0.1:
                est, rec = 30, 200
                confidence = "medium"
            else:
                est, rec = 100, 500
                confidence = "low"
            basis = f"{pair_key} on {chain}"

        return SlippageEstimate(
            estimated_bps=est,
            estimated_pct=est / 100,
            confidence=confidence,
            basis=basis,
            recommended_bps=rec,
        )

    # ── Tier 3: BTC pairs ────────────────────────────────────────────────────
    BTC_PAIRS = {"WBTC/WETH", "WBTC/USDC", "cbBTC/USDC", "cbBTC/WETH"}
    if pair_key in BTC_PAIRS:
        recommended = 200 if amount < 0.01 else 300
        return SlippageEstimate(
            estimated_bps=30,
            estimated_pct=0.3,
            confidence="medium",
            basis=f"BTC pair — moderate liquidity on {chain}",
            recommended_bps=recommended,
        )

    # ── Tier 4: Altcoin / exotic pairs (MYST, OP, etc.) ─────────────────────
    # Low-liquidity pairs — use conservative settings
    logger.warning(
        f"[safety] estimate_slippage: unknown pair '{pair_key}' on {chain}. "
        f"Using conservative 3% estimate. "
        f"Consider querying Uniswap pool directly for better accuracy."
    )
    return SlippageEstimate(
        estimated_bps=300,
        estimated_pct=3.0,
        confidence="low",
        basis=(
            f"Unknown pair '{pair_key}' — conservative estimate. "
            f"No liquidity data available for this pair. "
            f"Consider using a DEX aggregator quote for more accurate slippage."
        ),
        recommended_bps=500,  # 5% for unknown pairs
    )


# ─── 5. detect_sandwich_risk ─────────────────────────────────────────────────

def detect_sandwich_risk(tx_data: Dict[str, Any]) -> SandwichRisk:
    """
    Detect sandwich attack risk in swap transaction parameters.

    A sandwich attack occurs when a bot sees a pending swap with a low
    amountOutMinimum, front-runs it to move the price, lets the victim's
    swap execute at a worse price, then back-runs to profit.

    The primary indicator: amountOutMinimum is 0 or very close to 0.

    P13 LESSON (2026-03-25):
        The swap-eth-to-usdc.js script had a fallback that set
        amountOutMin = 0n if the quote call failed. The swap succeeded
        on-chain (tx confirmed) but returned "0 gained" in the USDC
        balance check — indicating slippage capture by a sandwich bot.

    Args:
        tx_data: Dict with any of these keys:
            - amountOutMinimum:  int — the amountOutMin param in the swap call (REQUIRED)
            - amountOutQuoted:   int — the quoted amount (optional, enables ratio check)
            - token_in:          str — symbol or address of input token (optional, for context)
            - token_out:         str — symbol or address of output token (optional, for context)
            - slippage_bps:      int — intended slippage setting (optional, for validation)

    Returns:
        SandwichRisk with level (SAFE/MEDIUM/HIGH/CRITICAL) and recommendation.
        If level == CRITICAL: should_block = True → swap MUST be refused.

    Raises:
        SafetyError: If amountOutMinimum key is missing from tx_data.
    """
    if "amountOutMinimum" not in tx_data:
        raise SafetyError(
            "detect_sandwich_risk: 'amountOutMinimum' key is missing from tx_data. "
            "Cannot assess sandwich risk without this value. "
            "REFUSING: treat missing amountOutMinimum as a blocking safety error."
        )

    amount_out_min = tx_data["amountOutMinimum"]
    amount_quoted  = tx_data.get("amountOutQuoted")
    token_in       = tx_data.get("token_in", "unknown")
    token_out      = tx_data.get("token_out", "unknown")

    # ── CRITICAL: amountOutMinimum = 0 ──────────────────────────────────────
    if amount_out_min == 0:
        reason = (
            f"amountOutMinimum is 0 for {token_in}→{token_out} swap. "
            f"This means you accept ANY output amount, including 0. "
            f"P13 LESSON: this exact configuration caused a swap to return '0 gained' "
            f"in USDC balance despite the tx confirming successfully "
            f"(sandwich bot captured the entire output on 2026-03-25)."
        )
        return SandwichRisk(
            level=SandwichRiskLevel.CRITICAL,
            reason=reason,
            amount_out_min=0,
            amount_quoted=amount_quoted,
            protection_pct=0.0,
            recommendation=(
                "BLOCK this swap. Get a fresh quote and call calc_min_output(quoted, slippage_bps=200). "
                "If quote is unavailable, REFUSE the swap entirely rather than set amountOutMin=0."
            ),
            should_block=True,
        )

    # ── If no quoted amount, we can only check if min is positive ────────────
    if amount_quoted is None or amount_quoted <= 0:
        if amount_out_min > 0:
            return SandwichRisk(
                level=SandwichRiskLevel.SAFE,
                reason=(
                    f"amountOutMinimum={amount_out_min} is positive. "
                    f"No quoted amount provided for ratio check — assuming safe."
                ),
                amount_out_min=amount_out_min,
                amount_quoted=None,
                protection_pct=None,
                recommendation=(
                    "Provide amountOutQuoted for more accurate risk assessment. "
                    "Ensure slippage_bps is set appropriately (recommend 100–300 bps)."
                ),
                should_block=False,
            )
        else:
            # Negative minimum (shouldn't happen, but be defensive)
            return SandwichRisk(
                level=SandwichRiskLevel.CRITICAL,
                reason=f"amountOutMinimum={amount_out_min} is negative — invalid parameter.",
                amount_out_min=amount_out_min,
                amount_quoted=None,
                protection_pct=None,
                recommendation="Fix amountOutMinimum. Use calc_min_output() to compute correctly.",
                should_block=True,
            )

    # ── Compute protection ratio ─────────────────────────────────────────────
    # protection_bps = amountOutMin / amountOutQuoted * 10000
    protection_bps = int((amount_out_min / amount_quoted) * 10_000)
    protection_pct = protection_bps / 100.0

    logger.debug(
        f"[safety] detect_sandwich_risk: min={amount_out_min}, "
        f"quoted={amount_quoted}, protection={protection_pct:.2f}%"
    )

    # ── CRITICAL: protection < 2% ────────────────────────────────────────────
    if protection_bps < SANDWICH_CRITICAL_THRESHOLD_BPS:
        return SandwichRisk(
            level=SandwichRiskLevel.CRITICAL,
            reason=(
                f"amountOutMinimum is only {protection_pct:.1f}% of quoted amount "
                f"({amount_out_min} vs quoted {amount_quoted}) for {token_in}→{token_out}. "
                f"This provides virtually no sandwich protection — a bot could extract "
                f"~{100 - protection_pct:.1f}% of your expected output."
            ),
            amount_out_min=amount_out_min,
            amount_quoted=amount_quoted,
            protection_pct=protection_pct,
            recommendation=(
                f"BLOCK this swap. "
                f"Minimum acceptable protection is {SANDWICH_CRITICAL_THRESHOLD_BPS / 100:.0f}% of quoted. "
                f"Use: calc_min_output({amount_quoted}, slippage_bps=200) = "
                f"{calc_min_output(amount_quoted, 200)} to get a safe minimum."
            ),
            should_block=True,
        )

    # ── HIGH: protection < 5% ────────────────────────────────────────────────
    if protection_bps < SANDWICH_HIGH_THRESHOLD_BPS:
        return SandwichRisk(
            level=SandwichRiskLevel.HIGH,
            reason=(
                f"amountOutMinimum is {protection_pct:.1f}% of quoted — dangerously low. "
                f"A sandwich bot could extract up to {100 - protection_pct:.1f}% of your output."
            ),
            amount_out_min=amount_out_min,
            amount_quoted=amount_quoted,
            protection_pct=protection_pct,
            recommendation=(
                f"Strongly recommend increasing amountOutMinimum. "
                f"Suggested: {calc_min_output(amount_quoted, 200)} (2% slippage). "
                f"Proceed only if you understand the sandwich risk."
            ),
            should_block=False,
        )

    # ── MEDIUM: protection < 10% ─────────────────────────────────────────────
    if protection_bps < SANDWICH_MEDIUM_THRESHOLD_BPS:
        return SandwichRisk(
            level=SandwichRiskLevel.MEDIUM,
            reason=(
                f"amountOutMinimum is {protection_pct:.1f}% of quoted — below 10% protection. "
                f"Moderate sandwich risk, especially for larger trades or volatile markets."
            ),
            amount_out_min=amount_out_min,
            amount_quoted=amount_quoted,
            protection_pct=protection_pct,
            recommendation=(
                f"Consider using {calc_min_output(amount_quoted, 200)} for 2% slippage protection "
                f"(vs current {amount_out_min}). Acceptable for small trades in calm markets."
            ),
            should_block=False,
        )

    # ── SAFE: protection >= 10% ──────────────────────────────────────────────
    return SandwichRisk(
        level=SandwichRiskLevel.SAFE,
        reason=(
            f"amountOutMinimum is {protection_pct:.1f}% of quoted — adequate sandwich protection."
        ),
        amount_out_min=amount_out_min,
        amount_quoted=amount_quoted,
        protection_pct=protection_pct,
        recommendation="No action required. Slippage protection is sufficient.",
        should_block=False,
    )


# ─── Helper functions ─────────────────────────────────────────────────────────

def _resolve_symbol(address: str, chain: str) -> Optional[str]:
    """
    Resolve a token address to its symbol using TOKEN_REGISTRY.
    Returns None if address not found in registry.
    """
    registry = TOKEN_REGISTRY.get(chain, {})
    address_lower = address.lower()
    for symbol, addr in registry.items():
        if addr.lower() == address_lower:
            return symbol
    return None


def _canonical_pair_key(sym_a: str, sym_b: str) -> str:
    """
    Produce a canonical pair key (alphabetically sorted).
    e.g., ("USDC", "WETH") → "USDC/WETH", ("WETH", "USDC") → "USDC/WETH"

    Exception: WETH and ETH are treated as equivalent.
    """
    # Normalize ETH ↔ WETH
    normalize = {"WETH": "ETH", "weth": "eth", "Weth": "Eth"}
    sym_a = normalize.get(sym_a, sym_a).upper()
    sym_b = normalize.get(sym_b, sym_b).upper()
    tokens = sorted([sym_a, sym_b])
    return f"{tokens[0]}/{tokens[1]}"


# ─── Module-level convenience: pre-swap safety check ─────────────────────────

def pre_swap_check(
    route:          Dict[str, Any],
    quoted_amount:  int,
    slippage_bps:   int = 200,
    wallet:         Optional[str] = None,
) -> Dict[str, Any]:
    """
    Convenience function: run all safety checks before a swap.

    Runs:
      1. validate_route()     — blocking if errors
      2. calc_min_output()    — computes safe amountOutMinimum
      3. detect_sandwich_risk() — verifies the computed min is safe
      4. check_approval()     — only if wallet + token addresses provided

    Args:
        route:          Route dict (see validate_route for schema)
        quoted_amount:  Quoted output from price aggregator (raw units)
        slippage_bps:   Slippage in basis points (default 200 = 2%)
        wallet:         Wallet address for approval check (optional)

    Returns:
        Dict with keys:
          - safe:           bool — True if all checks pass
          - min_output:     int  — computed amountOutMinimum
          - route_result:   RouteValidationResult
          - sandwich_risk:  SandwichRisk
          - approval:       ApprovalStatus | None
          - errors:         list[str] — all blocking issues
          - warnings:       list[str] — all non-blocking issues

    Raises:
        SafetyError: If quoted_amount is 0 (no safe minimum possible).
    """
    all_errors: List[str] = []
    all_warnings: List[str] = []

    # ── Step 1: Validate route ───────────────────────────────────────────────
    route_result = validate_route(route)
    all_errors.extend(route_result.errors)
    all_warnings.extend(route_result.warnings)

    # ── Step 2: Compute min output ───────────────────────────────────────────
    # SafetyError propagates up if quoted_amount == 0
    min_output = calc_min_output(quoted_amount, slippage_bps)

    # ── Step 3: Detect sandwich risk ─────────────────────────────────────────
    sandwich = detect_sandwich_risk({
        "amountOutMinimum": min_output,
        "amountOutQuoted":  quoted_amount,
        "token_in":  route.get("token_in", "unknown"),
        "token_out": route.get("token_out", "unknown"),
    })

    if sandwich.should_block:
        all_errors.append(f"Sandwich risk [{sandwich.level}]: {sandwich.reason}")

    # ── Step 4: Check approval (optional) ────────────────────────────────────
    approval: Optional[ApprovalStatus] = None

    if wallet and route.get("token_in") and route.get("router"):
        chain = route.get("chain", "base")
        approval = check_approval(
            token=route["token_in"],
            spender=route["router"],
            amount=int(route.get("amount_in", 0)),
            wallet=wallet,
            chain=chain,
        )
        if not approval.is_sufficient and route.get("amount_in", 0) > 0:
            all_errors.append(
                f"Insufficient approval: need {approval.required_amount} but "
                f"current allowance is {approval.current_allowance} for "
                f"{approval.spender_name or approval.spender_address}"
            )
        if approval.warning:
            all_warnings.append(approval.warning)

    safe = len(all_errors) == 0

    if safe:
        logger.info(
            f"[safety] ✅ pre_swap_check passed — min_output={min_output}, "
            f"sandwich_risk={sandwich.level}"
        )
    else:
        logger.error(
            f"[safety] ❌ pre_swap_check FAILED — {len(all_errors)} blocking error(s): "
            f"{all_errors}"
        )

    return {
        "safe":          safe,
        "min_output":    min_output,
        "route_result":  route_result,
        "sandwich_risk": sandwich,
        "approval":      approval,
        "errors":        all_errors,
        "warnings":      all_warnings,
    }
