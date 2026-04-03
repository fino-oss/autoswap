"""
router.py — AutoSwap cross-DEX Router (Phase 1)

Finds the best price across Paraswap and Uniswap V3 for a given swap.
Supports Base (8453) and Polygon (137) in Phase 1.

Usage:
    from router import Router, RouteResult

    router = Router()
    result = router.get_best_route(
        from_token="ETH",
        to_token="USDC",
        amount=0.001,          # in human units (ETH, not wei)
        chain="base",
        slippage_bps=100,      # 1%
        user_address="0x..."   # optional, needed for tx_data
    )
    print(result.expected_output, result.min_output, result.tx_data)
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import requests
from web3 import Web3

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# Native token placeholder (Paraswap convention)
NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# Chain config
CHAINS: Dict[str, Dict] = {
    "base": {
        "chain_id": 8453,
        "rpc": "https://mainnet.base.org",
        "name": "Base",
    },
    "polygon": {
        "chain_id": 137,
        "rpc": "https://polygon.drpc.org",
        "name": "Polygon",
    },
}

# Token registry: {chain: {symbol_upper: {address, decimals}}}
TOKENS: Dict[str, Dict] = {
    "base": {
        "ETH": {
            "address": NATIVE_TOKEN,
            "decimals": 18,
            "weth": "0x4200000000000000000000000000000000000006",
        },
        "WETH": {
            "address": "0x4200000000000000000000000000000000000006",
            "decimals": 18,
        },
        "USDC": {
            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "decimals": 6,
        },
        "DAI": {
            "address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
            "decimals": 18,
        },
        "CBETH": {
            "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
            "decimals": 18,
        },
    },
    "polygon": {
        "POL": {
            "address": NATIVE_TOKEN,
            "decimals": 18,
            "weth": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",  # WMATIC
        },
        "MATIC": {  # alias for POL
            "address": NATIVE_TOKEN,
            "decimals": 18,
            "weth": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        },
        "WMATIC": {
            "address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
            "decimals": 18,
        },
        "WPOL": {  # alias
            "address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
            "decimals": 18,
        },
        "USDC": {
            "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "decimals": 6,
        },
        "USDC_E": {  # bridged USDC
            "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "decimals": 6,
        },
        "USDT": {
            "address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
            "decimals": 6,
        },
        "WETH": {
            "address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
            "decimals": 18,
        },
        "MYST": {
            "address": "0x1379E8886A944d2D9d440b3d88DF536Aea08d9F3",
            "decimals": 18,
        },
        "DAI": {
            "address": "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
            "decimals": 18,
        },
    },
}

# Uniswap V3 contract addresses by chain
# NOTE: Base QuoterV2 at 0x3d4e44... is NOT deployed (0 bytes on-chain as of 2026-03).
# We use slot0-based price estimation for Base as the UniV3 fallback.
UNISWAP_V3: Dict[str, Dict] = {
    "base": {
        # QuoterV2 not deployed on Base mainnet — use slot0 estimator instead
        "quoter_v2":   None,
        "quoter_v1":   "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6",  # 2109 bytes, V1 interface
        "factory":     "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "swap_router": "0x2626664c2603336E57B271c5C0b26F421741e481",  # SwapRouter02
        "use_slot0":   True,  # fallback to slot0-based estimation
    },
    "polygon": {
        "quoter_v2":   "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",  # confirmed working
        "factory":     "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "swap_router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  # SwapRouter02
        "use_slot0":   False,
    },
}

# Uniswap V3 fee tiers to try (in order of priority)
FEE_TIERS = [500, 3000, 10000, 100]

# ABIs
QUOTER_V2_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn",             "type": "address"},
                    {"name": "tokenOut",            "type": "address"},
                    {"name": "amountIn",            "type": "uint256"},
                    {"name": "fee",                 "type": "uint24"},
                    {"name": "sqrtPriceLimitX96",   "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut",                   "type": "uint256"},
            {"name": "sqrtPriceX96After",           "type": "uint160"},
            {"name": "initializedTicksCrossed",     "type": "uint32"},
            {"name": "gasEstimate",                 "type": "uint256"},
        ],
    }
]

SWAP_ROUTER_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn",             "type": "address"},
                    {"name": "tokenOut",            "type": "address"},
                    {"name": "fee",                 "type": "uint24"},
                    {"name": "recipient",           "type": "address"},
                    {"name": "amountIn",            "type": "uint256"},
                    {"name": "amountOutMinimum",    "type": "uint256"},
                    {"name": "sqrtPriceLimitX96",   "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    }
]

FACTORY_ABI = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee",    "type": "uint24"},
        ],
        "outputs": [{"name": "pool", "type": "address"}],
    }
]

POOL_SLOT0_ABI = [
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96",               "type": "uint160"},
            {"name": "tick",                        "type": "int24"},
            {"name": "observationIndex",            "type": "uint16"},
            {"name": "observationCardinality",      "type": "uint16"},
            {"name": "observationCardinalityNext",  "type": "uint16"},
            {"name": "feeProtocol",                 "type": "uint8"},
            {"name": "unlocked",                    "type": "bool"},
        ],
    },
    {
        "name": "token0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "token1",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "liquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}],
    },
]

ERC20_DECIMALS_ABI = [
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    }
]

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }
]

# Paraswap base URL
PARASWAP_URL = "https://apiv5.paraswap.io"
PARASWAP_TIMEOUT = 10  # seconds


# ─── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RouteResult:
    """Result of get_best_route() — always has a valid min_output > 0."""

    # Which DEX won
    dex: str                        # "paraswap" | "uniswap_v3" | "uniswap_v3_multihop"
    route: str                      # Human-readable description

    # Tokens
    from_token: str                 # symbol
    to_token: str                   # symbol
    chain: str                      # "base" | "polygon"

    # Amounts (all in human units, NOT raw)
    from_amount: float              # Input amount
    expected_output: float          # Best quoted output
    min_output: float               # = expected_output * (1 - slippage)  [NEVER 0]

    # Raw amounts (base units, as int)
    from_amount_raw: int
    expected_output_raw: int
    min_output_raw: int             # NEVER 0

    # Slippage used
    slippage_bps: int               # e.g. 100 = 1%

    # Transaction data (ready to sign+send, or None in dry-run / no user_address)
    tx_data: Optional[Dict[str, Any]] = None

    # Paraswap-specific: price route for building the tx
    paraswap_price_route: Optional[Dict] = None

    # Uniswap V3 fee tier used (if applicable)
    uniswap_fee_tier: Optional[int] = None

    # Extra metadata
    meta: Dict[str, Any] = field(default_factory=dict)


class RouterError(Exception):
    """Raised when no valid route is found."""
    pass


# ─── Router ───────────────────────────────────────────────────────────────────

class Router:
    """
    Cross-DEX router for Phase 1 (Base + Polygon).

    Strategy:
      1. Try Paraswap (aggregator — best price in most cases)
      2. If Paraswap fails → fallback to Uniswap V3 QuoterV2 (on-chain)
      3. Compare both and return the best result

    Guarantee: min_output is ALWAYS > 0.
    """

    def __init__(self, timeout: int = PARASWAP_TIMEOUT):
        self.timeout = timeout
        self._web3_cache: Dict[str, Web3] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_best_route(
        self,
        from_token: str,
        to_token: str,
        amount: float,
        chain: str,
        slippage_bps: int = 100,
        user_address: Optional[str] = None,
    ) -> RouteResult:
        """
        Find the best swap route across Paraswap and Uniswap V3.

        Args:
            from_token: Token symbol or address (e.g. "ETH", "USDC")
            to_token:   Token symbol or address (e.g. "USDC", "MYST")
            amount:     Amount in human units (e.g. 0.001 for 0.001 ETH)
            chain:      "base" or "polygon"
            slippage_bps: Max slippage in basis points (default 100 = 1%)
            user_address: Wallet address (needed to build tx_data)

        Returns:
            RouteResult with best price, min_output > 0, and tx_data

        Raises:
            RouterError: if no route found on any DEX
            ValueError: if token/chain not supported
        """
        chain = chain.lower()
        from_token = from_token.upper()
        to_token   = to_token.upper()

        if chain not in CHAINS:
            raise ValueError(f"Chain '{chain}' not supported. Use: {list(CHAINS.keys())}")

        # Resolve token info
        src_info  = self._resolve_token(from_token, chain)
        dest_info = self._resolve_token(to_token, chain)

        logger.info(
            f"[Router] {from_token}→{to_token} on {chain} | "
            f"amount={amount} | slippage={slippage_bps}bps"
        )

        # Convert amount to raw (base units)
        src_amount_raw = int(amount * (10 ** src_info["decimals"]))

        results = []

        # ── 1. Try Paraswap ──────────────────────────────────────────────────
        try:
            ps_result = self._quote_paraswap(
                src_info=src_info,
                dest_info=dest_info,
                src_amount_raw=src_amount_raw,
                from_token=from_token,
                to_token=to_token,
                amount=amount,
                chain=chain,
                slippage_bps=slippage_bps,
                user_address=user_address,
            )
            results.append(ps_result)
            logger.info(
                f"[Paraswap] expected={ps_result.expected_output:.6f} {to_token} | "
                f"min={ps_result.min_output:.6f}"
            )
        except Exception as e:
            logger.warning(f"[Paraswap] failed: {e}")

        # ── 2. Try Uniswap V3 (fallback) ────────────────────────────────────
        try:
            uni_result = self._quote_uniswap_v3(
                src_info=src_info,
                dest_info=dest_info,
                src_amount_raw=src_amount_raw,
                from_token=from_token,
                to_token=to_token,
                amount=amount,
                chain=chain,
                slippage_bps=slippage_bps,
                user_address=user_address,
            )
            results.append(uni_result)
            logger.info(
                f"[Uniswap V3] expected={uni_result.expected_output:.6f} {to_token} | "
                f"min={uni_result.min_output:.6f} | fee={uni_result.uniswap_fee_tier}"
            )
        except Exception as e:
            logger.warning(f"[Uniswap V3] failed: {e}")

        if not results:
            raise RouterError(
                f"No route found for {from_token}→{to_token} on {chain}. "
                f"Both Paraswap and Uniswap V3 failed."
            )

        # ── 3. Pick best (highest expected_output) ──────────────────────────
        best = max(results, key=lambda r: r.expected_output)
        winner_label = "🥇 Paraswap" if best.dex == "paraswap" else "🥈 Uniswap V3"
        logger.info(
            f"[Router] Best route: {winner_label} | "
            f"output={best.expected_output:.6f} {to_token}"
        )

        # Sanity check: min_output must NEVER be 0
        assert best.min_output > 0, "FATAL: min_output is 0 — this must never happen"
        assert best.min_output_raw > 0, "FATAL: min_output_raw is 0 — this must never happen"

        return best

    # ── Paraswap ──────────────────────────────────────────────────────────────

    def _quote_paraswap(
        self,
        src_info: Dict,
        dest_info: Dict,
        src_amount_raw: int,
        from_token: str,
        to_token: str,
        amount: float,
        chain: str,
        slippage_bps: int,
        user_address: Optional[str],
    ) -> RouteResult:
        """Query Paraswap /prices and optionally /transactions."""

        chain_id = CHAINS[chain]["chain_id"]
        src_addr  = src_info["address"]
        dest_addr = dest_info["address"]

        # Step 1: GET /prices
        params = {
            "srcToken":    src_addr,
            "destToken":   dest_addr,
            "amount":      str(src_amount_raw),
            "srcDecimals": str(src_info["decimals"]),
            "destDecimals":str(dest_info["decimals"]),
            "network":     str(chain_id),
            "side":        "SELL",
            "partner":     "autoswap",
        }

        resp = requests.get(
            f"{PARASWAP_URL}/prices",
            params=params,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RouterError(f"Paraswap prices error: {data['error']}")

        price_route = data.get("priceRoute")
        if not price_route:
            raise RouterError("Paraswap returned no priceRoute")

        dest_amount_raw = int(price_route["destAmount"])
        if dest_amount_raw == 0:
            raise RouterError("Paraswap returned destAmount=0")

        dest_amount = dest_amount_raw / (10 ** dest_info["decimals"])

        # Calculate min_output with slippage
        slippage_factor = 10000 - slippage_bps
        min_output_raw  = (dest_amount_raw * slippage_factor) // 10000
        min_output      = min_output_raw / (10 ** dest_info["decimals"])

        # Guarantee: min_output must be > 0
        if min_output_raw <= 0:
            raise RouterError(
                f"Paraswap min_output_raw would be 0 or negative after slippage "
                f"(dest_amount_raw={dest_amount_raw}, slippage_bps={slippage_bps})"
            )

        route_desc = self._build_paraswap_route_desc(price_route, from_token, to_token)

        # Step 2 (optional): POST /transactions/{chainId} if user_address given
        tx_data = None
        if user_address:
            try:
                tx_data = self._build_paraswap_tx(
                    price_route=price_route,
                    src_amount_raw=src_amount_raw,
                    min_output_raw=min_output_raw,
                    src_addr=src_addr,
                    dest_addr=dest_addr,
                    chain_id=chain_id,
                    user_address=user_address,
                    slippage_bps=slippage_bps,
                    src_info=src_info,
                )
            except Exception as e:
                logger.warning(f"[Paraswap] tx build failed (quote still valid): {e}")

        return RouteResult(
            dex="paraswap",
            route=route_desc,
            from_token=from_token,
            to_token=to_token,
            chain=chain,
            from_amount=amount,
            expected_output=dest_amount,
            min_output=min_output,
            from_amount_raw=src_amount_raw,
            expected_output_raw=dest_amount_raw,
            min_output_raw=min_output_raw,
            slippage_bps=slippage_bps,
            tx_data=tx_data,
            paraswap_price_route=price_route,
            meta={
                "gas_cost_usd": price_route.get("gasCostUSD"),
                "side": price_route.get("side"),
                "percent_change": price_route.get("hmac"),
            },
        )

    def _build_paraswap_route_desc(self, price_route: Dict, from_token: str, to_token: str) -> str:
        """Build a human-readable route description from Paraswap priceRoute."""
        best_route = price_route.get("bestRoute", [])
        if not best_route:
            return f"Paraswap: {from_token} → {to_token}"

        hops = []
        for leg in best_route:
            swaps = leg.get("swaps", [])
            for swap_info in swaps:
                swapExchanges = swap_info.get("swapExchanges", [])
                for ex in swapExchanges:
                    hops.append(ex.get("exchange", "?"))

        if hops:
            return f"Paraswap [{' → '.join(hops)}]: {from_token} → {to_token}"
        return f"Paraswap: {from_token} → {to_token}"

    def _build_paraswap_tx(
        self,
        price_route: Dict,
        src_amount_raw: int,
        min_output_raw: int,
        src_addr: str,
        dest_addr: str,
        chain_id: int,
        user_address: str,
        slippage_bps: int,
        src_info: Dict,
    ) -> Dict:
        """POST /transactions/{chainId} to get signed-ready tx from Paraswap."""

        payload = {
            "srcToken":   src_addr,
            "destToken":  dest_addr,
            "srcAmount":  str(src_amount_raw),
            "destAmount": str(min_output_raw),  # minimum acceptable
            "priceRoute": price_route,
            "userAddress": user_address,
            "slippage": slippage_bps,
            "partner": "autoswap",
        }

        resp = requests.post(
            f"{PARASWAP_URL}/transactions/{chain_id}",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        tx = resp.json()

        if "error" in tx:
            raise RouterError(f"Paraswap tx error: {tx['error']}")

        # Add value field for native token swaps
        if src_addr.lower() == NATIVE_TOKEN.lower():
            tx["value"] = hex(src_amount_raw)

        return tx

    # ── Uniswap V3 ────────────────────────────────────────────────────────────

    def _quote_uniswap_v3(
        self,
        src_info: Dict,
        dest_info: Dict,
        src_amount_raw: int,
        from_token: str,
        to_token: str,
        amount: float,
        chain: str,
        slippage_bps: int,
        user_address: Optional[str],
    ) -> RouteResult:
        """
        Query Uniswap V3 for a quote.
        Uses QuoterV2 if available, otherwise falls back to slot0 price estimation.
        """
        if chain not in UNISWAP_V3:
            raise RouterError(f"Uniswap V3 not configured for chain '{chain}'")

        # Check if we should use slot0 (QuoterV2 not available on this chain)
        if UNISWAP_V3[chain].get("use_slot0", False) or not UNISWAP_V3[chain].get("quoter_v2"):
            logger.debug(f"[UniV3] Using slot0 estimator for {chain} (QuoterV2 not available)")
            return self._quote_uniswap_v3_slot0(
                src_info=src_info,
                dest_info=dest_info,
                src_amount_raw=src_amount_raw,
                from_token=from_token,
                to_token=to_token,
                amount=amount,
                chain=chain,
                slippage_bps=slippage_bps,
                user_address=user_address,
            )

        w3 = self._get_web3(chain)
        quoter_addr = UNISWAP_V3[chain]["quoter_v2"]
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(quoter_addr),
            abi=QUOTER_V2_ABI,
        )

        # Uniswap V3 uses WETH, not native ETH — resolve the actual ERC-20 address
        token_in  = self._token_to_erc20(src_info, chain)
        token_out = self._token_to_erc20(dest_info, chain)

        best_amount_out = 0
        best_fee = None

        for fee in FEE_TIERS:
            try:
                params = {
                    "tokenIn":           Web3.to_checksum_address(token_in),
                    "tokenOut":          Web3.to_checksum_address(token_out),
                    "amountIn":          src_amount_raw,
                    "fee":               fee,
                    "sqrtPriceLimitX96": 0,
                }
                result = quoter.functions.quoteExactInputSingle(params).call()
                amount_out = result[0]

                if amount_out > best_amount_out:
                    best_amount_out = amount_out
                    best_fee = fee
                    logger.debug(
                        f"[UniV3] fee={fee} → {amount_out / (10**dest_info['decimals']):.6f} {to_token}"
                    )
            except Exception as e:
                logger.debug(f"[UniV3] fee={fee} no pool or error: {e}")
                continue

        if best_amount_out == 0:
            raise RouterError(
                f"Uniswap V3: no liquidity found for {from_token}→{to_token} on {chain} "
                f"(tried fee tiers: {FEE_TIERS})"
            )

        dest_amount = best_amount_out / (10 ** dest_info["decimals"])

        # Calculate min_output — NEVER 0
        slippage_factor = 10000 - slippage_bps
        min_output_raw  = (best_amount_out * slippage_factor) // 10000
        min_output      = min_output_raw / (10 ** dest_info["decimals"])

        if min_output_raw <= 0:
            raise RouterError(
                f"Uniswap V3 min_output_raw would be 0 after slippage "
                f"(quoted={best_amount_out}, slippage_bps={slippage_bps})"
            )

        route_desc = (
            f"Uniswap V3 [{best_fee/10000:.3f}% pool]: {from_token} → {to_token}"
        )

        # Build tx_data if user_address is provided
        tx_data = None
        if user_address:
            try:
                tx_data = self._build_uniswap_v3_tx(
                    src_info=src_info,
                    dest_info=dest_info,
                    token_in=token_in,
                    token_out=token_out,
                    src_amount_raw=src_amount_raw,
                    min_output_raw=min_output_raw,
                    fee=best_fee,
                    chain=chain,
                    user_address=user_address,
                )
            except Exception as e:
                logger.warning(f"[UniV3] tx build failed (quote still valid): {e}")

        return RouteResult(
            dex="uniswap_v3",
            route=route_desc,
            from_token=from_token,
            to_token=to_token,
            chain=chain,
            from_amount=amount,
            expected_output=dest_amount,
            min_output=min_output,
            from_amount_raw=src_amount_raw,
            expected_output_raw=best_amount_out,
            min_output_raw=min_output_raw,
            slippage_bps=slippage_bps,
            tx_data=tx_data,
            uniswap_fee_tier=best_fee,
            meta={"fee_tiers_tried": FEE_TIERS},
        )

    def _quote_uniswap_v3_slot0(
        self,
        src_info: Dict,
        dest_info: Dict,
        src_amount_raw: int,
        from_token: str,
        to_token: str,
        amount: float,
        chain: str,
        slippage_bps: int,
        user_address: Optional[str],
    ) -> RouteResult:
        """
        Estimate Uniswap V3 output using slot0() price from pool.
        Used as fallback when QuoterV2 is not available (e.g., Base mainnet).

        Accuracy: good for small amounts (< 0.1% of pool liquidity).
        For larger amounts, Paraswap is more accurate due to price impact.
        """
        if chain not in UNISWAP_V3:
            raise RouterError(f"Uniswap V3 not configured for chain '{chain}'")

        factory_addr = UNISWAP_V3[chain].get("factory")
        if not factory_addr:
            raise RouterError(f"Factory address not configured for {chain}")

        w3 = self._get_web3(chain)
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(factory_addr),
            abi=FACTORY_ABI,
        )

        token_in  = self._token_to_erc20(src_info, chain)
        token_out = self._token_to_erc20(dest_info, chain)

        best_amount_out = 0
        best_fee = None

        for fee in FEE_TIERS:
            try:
                pool_addr = factory.functions.getPool(
                    Web3.to_checksum_address(token_in),
                    Web3.to_checksum_address(token_out),
                    fee,
                ).call()

                # Zero address means no pool
                if pool_addr == "0x0000000000000000000000000000000000000000":
                    continue

                pool = w3.eth.contract(
                    address=Web3.to_checksum_address(pool_addr),
                    abi=POOL_SLOT0_ABI,
                )

                # Get current price and liquidity
                slot0 = pool.functions.slot0().call()
                sqrtPriceX96 = slot0[0]
                liquidity = pool.functions.liquidity().call()

                if sqrtPriceX96 == 0 or liquidity == 0:
                    continue

                # Determine which token is token0 in the pool
                pool_token0 = pool.functions.token0().call()
                is_token0_input = pool_token0.lower() == token_in.lower()

                # Calculate price from sqrtPriceX96
                # price = (sqrtPriceX96 / 2^96)^2 = amount_of_token1_raw / amount_of_token0_raw
                Q96 = 2 ** 96
                if is_token0_input:
                    # Selling token0, getting token1
                    # price = sqrtP^2/Q96^2 (token1_raw per token0_raw)
                    price_raw = (sqrtPriceX96 ** 2) / (Q96 ** 2)
                    # Adjust for fee
                    amount_out_raw = src_amount_raw * price_raw * (1 - fee / 1_000_000)
                else:
                    # Selling token1, getting token0
                    # Inverse price = Q96^2 / sqrtP^2
                    price_raw = (Q96 ** 2) / (sqrtPriceX96 ** 2)
                    amount_out_raw = src_amount_raw * price_raw * (1 - fee / 1_000_000)

                amount_out_int = int(amount_out_raw)
                if amount_out_int > best_amount_out:
                    best_amount_out = amount_out_int
                    best_fee = fee

                    logger.debug(
                        f"[UniV3 slot0] fee={fee} pool={pool_addr[:10]}... "
                        f"→ {amount_out_int / (10**dest_info['decimals']):.6f} {to_token}"
                    )

            except Exception as e:
                logger.debug(f"[UniV3 slot0] fee={fee} error: {e}")
                continue

        if best_amount_out == 0:
            raise RouterError(
                f"Uniswap V3 slot0: no pool found for {from_token}→{to_token} on {chain}"
            )

        dest_amount = best_amount_out / (10 ** dest_info["decimals"])

        slippage_factor = 10000 - slippage_bps
        min_output_raw  = (best_amount_out * slippage_factor) // 10000
        min_output      = min_output_raw / (10 ** dest_info["decimals"])

        if min_output_raw <= 0:
            raise RouterError("Uniswap V3 slot0 min_output_raw would be 0 after slippage")

        route_desc = (
            f"Uniswap V3 slot0 [{best_fee/10000:.3f}% pool] (price estimate): "
            f"{from_token} → {to_token}"
        )

        tx_data = None
        if user_address:
            try:
                tx_data = self._build_uniswap_v3_tx(
                    src_info=src_info,
                    dest_info=dest_info,
                    token_in=token_in,
                    token_out=token_out,
                    src_amount_raw=src_amount_raw,
                    min_output_raw=min_output_raw,
                    fee=best_fee,
                    chain=chain,
                    user_address=user_address,
                )
            except Exception as e:
                logger.warning(f"[UniV3 slot0] tx build failed: {e}")

        return RouteResult(
            dex="uniswap_v3",
            route=route_desc,
            from_token=from_token,
            to_token=to_token,
            chain=chain,
            from_amount=amount,
            expected_output=dest_amount,
            min_output=min_output,
            from_amount_raw=src_amount_raw,
            expected_output_raw=best_amount_out,
            min_output_raw=min_output_raw,
            slippage_bps=slippage_bps,
            tx_data=tx_data,
            uniswap_fee_tier=best_fee,
            meta={"method": "slot0_price_estimate", "fee_tiers_tried": FEE_TIERS},
        )

    def _build_uniswap_v3_tx(
        self,
        src_info: Dict,
        dest_info: Dict,
        token_in: str,
        token_out: str,
        src_amount_raw: int,
        min_output_raw: int,
        fee: int,
        chain: str,
        user_address: str,
    ) -> Dict:
        """Build Uniswap V3 SwapRouter02.exactInputSingle tx data."""

        w3 = self._get_web3(chain)
        router_addr = UNISWAP_V3[chain]["swap_router"]
        router_contract = w3.eth.contract(
            address=Web3.to_checksum_address(router_addr),
            abi=SWAP_ROUTER_ABI,
        )

        is_native_in = src_info["address"].lower() == NATIVE_TOKEN.lower()

        params = {
            "tokenIn":           Web3.to_checksum_address(token_in),
            "tokenOut":          Web3.to_checksum_address(token_out),
            "fee":               fee,
            "recipient":         Web3.to_checksum_address(user_address),
            "amountIn":          src_amount_raw,
            "amountOutMinimum":  min_output_raw,  # NEVER 0
            "sqrtPriceLimitX96": 0,
        }

        # Encode the calldata
        calldata = router_contract.encodeABI(
            fn_name="exactInputSingle",
            args=[params],
        )

        tx = {
            "to":    router_addr,
            "data":  calldata,
            "value": hex(src_amount_raw) if is_native_in else "0x0",
            "chainId": CHAINS[chain]["chain_id"],
        }

        return tx

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_token(self, symbol_or_address: str, chain: str) -> Dict:
        """Resolve a token symbol or address to {address, decimals}."""
        chain_tokens = TOKENS.get(chain, {})

        # Try by symbol (already uppercased)
        if symbol_or_address in chain_tokens:
            return chain_tokens[symbol_or_address]

        # Try as address (0x...)
        if symbol_or_address.startswith("0x"):
            # Try to find matching address in registry
            for token_info in chain_tokens.values():
                if token_info["address"].lower() == symbol_or_address.lower():
                    return token_info
            # Unknown address — assume 18 decimals as fallback
            logger.warning(
                f"Token {symbol_or_address} not in registry for {chain}, "
                f"assuming 18 decimals"
            )
            return {"address": symbol_or_address, "decimals": 18}

        raise ValueError(
            f"Token '{symbol_or_address}' not found on {chain}. "
            f"Known tokens: {list(chain_tokens.keys())}"
        )

    def _token_to_erc20(self, token_info: Dict, chain: str) -> str:
        """
        Return ERC-20 address for a token.
        For native tokens (ETH/POL), returns the WETH/WMATIC address
        because Uniswap V3 trades WETH, not native ETH.
        """
        address = token_info["address"]
        if address.lower() == NATIVE_TOKEN.lower():
            weth_addr = token_info.get("weth")
            if not weth_addr:
                raise RouterError(
                    f"No WETH/WMATIC address configured for native token on {chain}"
                )
            return weth_addr
        return address

    def _get_web3(self, chain: str) -> Web3:
        """Get or create a Web3 instance for the given chain."""
        if chain not in self._web3_cache:
            rpc = CHAINS[chain]["rpc"]
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": self.timeout}))
            self._web3_cache[chain] = w3
        return self._web3_cache[chain]


# ─── Module-level convenience function ────────────────────────────────────────

_default_router = None


def get_best_route(
    from_token: str,
    to_token: str,
    amount: float,
    chain: str,
    slippage_bps: int = 100,
    user_address: Optional[str] = None,
) -> RouteResult:
    """
    Convenience function — same as Router().get_best_route(...).

    Example:
        from router import get_best_route
        result = get_best_route("ETH", "USDC", 0.001, "base")
        print(f"Expected: {result.expected_output} USDC")
        print(f"Min out:  {result.min_output} USDC")
        print(f"Via:      {result.route}")
    """
    global _default_router
    if _default_router is None:
        _default_router = Router()
    return _default_router.get_best_route(
        from_token=from_token,
        to_token=to_token,
        amount=amount,
        chain=chain,
        slippage_bps=slippage_bps,
        user_address=user_address,
    )
