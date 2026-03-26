"""
gas.py — Cold Start Gas Resolver for AutoSwap (p14)

Solves the "cold start gas problem" experienced on 2026-03-25:
having tokens (USDC) on a chain but 0 native gas to execute transactions.

Usage:
    from gas import GasResolver, GasResolveResult, GasStrategy

    resolver = GasResolver()
    result = resolver.resolve(
        wallet="0x...",
        chain="polygon",
        source_chain="base",    # chain with USDC to spend
        dry_run=True,
    )

    if result.strategy == GasStrategy.SKIP:
        print("Already have enough gas — nothing to do")
    elif result.strategy == GasStrategy.RELAY_LINK:
        print(f"Relay.link quote ready: need to confirm {result.gas_needed} gas")
        print(f"Cost: {result.usdc_cost:.4f} USDC from {result.source_chain}")
    elif result.strategy == GasStrategy.GELATO_RELAY:
        print("Using Gelato gasless relay for execution")
    elif result.strategy == GasStrategy.MANUAL:
        raise RuntimeError(result.error)

Key lessons from p13:
- Relay.link returns status "fallback" when liquidity is insufficient → always verify status
- Always check GET /requests?user=ADDR after submitting to confirm execution
- Relay.link works best for amounts > 1 USDC equivalent

Architecture:
    resolve() →
        1. check_native_balance()   — RPC call to check current gas balance
        2. If enough → GasStrategy.SKIP
        3. If not → try_relay_link() — POST /quote + verify status != "fallback"
        4. If Relay.link fails → try_gelato_relay() — gasless execution
        5. If all fail → GasStrategy.MANUAL with clear error
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List

import requests
from web3 import Web3

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

RELAY_LINK_API = "https://api.relay.link"
GELATO_RELAY_API = "https://relay.gelato.network"
GELATO_TASK_STATUS_URL = "https://relay.gelato.network/tasks/status"
VAULT_PATH = "/Users/sam/.pi/agent/skills/agent-vault/vault.sh"

# Chain configuration
CHAINS: Dict[str, Dict] = {
    "polygon": {
        "chain_id": 137,
        "rpc": "https://polygon.drpc.org",
        "name": "Polygon",
        "native_symbol": "POL",
        "native_decimals": 18,
        "native_coingecko_id": "matic-network",
        # POL native token address (also used by Relay.link for destination)
        # 0x1010 is the special ERC-20 wrapper for native POL/MATIC
        "native_address": "0x0000000000000000000000000000000000001010",
    },
    "base": {
        "chain_id": 8453,
        "rpc": "https://mainnet.base.org",
        "name": "Base",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "native_coingecko_id": "ethereum",
        # ETH native — use zero address convention for Relay.link
        "native_address": "0x0000000000000000000000000000000000000000",
    },
    "arbitrum": {
        "chain_id": 42161,
        "rpc": "https://arb1.arbitrum.io/rpc",
        "name": "Arbitrum",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "native_coingecko_id": "ethereum",
        "native_address": "0x0000000000000000000000000000000000000000",
    },
    "optimism": {
        "chain_id": 10,
        "rpc": "https://mainnet.optimism.io",
        "name": "Optimism",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "native_coingecko_id": "ethereum",
        "native_address": "0x0000000000000000000000000000000000000000",
    },
}

# Minimum native gas required per chain (~enough for 3 swaps)
# Based on gas cost analysis from p13 operations
GAS_MINIMUMS: Dict[str, float] = {
    "polygon":  0.5,     # 0.5 POL  ≈ $0.25 (Polygon gas is cheap)
    "base":     0.0005,  # 0.0005 ETH ≈ $1
    "arbitrum": 0.0001,  # 0.0001 ETH ≈ $0.20
    "optimism": 0.0001,  # 0.0001 ETH ≈ $0.20
}

# Amount of native gas to request (slightly more than minimum for buffer)
GAS_REQUEST_AMOUNTS: Dict[str, float] = {
    "polygon":  1.0,     # 1 POL — enough for 5-6 swaps
    "base":     0.001,   # 0.001 ETH — enough for 3-4 swaps
    "arbitrum": 0.0002,  # 0.0002 ETH
    "optimism": 0.0002,  # 0.0002 ETH
}

# USDC token addresses by chain (source of funds for gas purchase)
USDC_ADDRESSES: Dict[str, str] = {
    "base":     "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
    "polygon":  "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # USDC on Polygon
    "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC on Arbitrum
    "optimism": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",  # USDC on Optimism
}

# Minimal ERC-20 ABI for balance check
ERC20_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


# ─── Data structures ───────────────────────────────────────────────────────────

class GasStrategy(str, Enum):
    """Resolution strategy selected by the resolver."""
    SKIP         = "skip"         # Already have enough gas
    RELAY_LINK   = "relay_link"   # Use Relay.link cross-chain swap
    GELATO_RELAY = "gelato_relay" # Use Gelato gasless relay
    MANUAL       = "manual"       # All strategies failed — manual intervention needed


@dataclass
class RelayLinkQuote:
    """Quote from Relay.link for cross-chain gas acquisition."""
    request_id: str            # ID for tracking via GET /requests
    origin_chain_id: int
    destination_chain_id: int
    origin_currency: str       # USDC address on source chain
    destination_currency: str  # Native gas token address
    origin_amount_raw: int     # USDC to spend (raw, 6 decimals)
    origin_amount: float       # USDC to spend (human)
    destination_amount_raw: int # Native gas to receive (raw, 18 decimals)
    destination_amount: float  # Native gas to receive (human)
    status: str                # "success" | "fallback" | "pending"
    expiry_ts: int             # Unix timestamp when quote expires
    steps: List[Dict]          # Transaction steps to execute
    raw_response: Dict         # Full API response for debugging


@dataclass
class GasResolveResult:
    """Result of GasResolver.resolve()."""
    # Core outcome
    strategy: GasStrategy
    chain: str
    wallet_address: str

    # Current state
    current_balance: float     # Current native gas balance (human units)
    gas_needed: float          # Minimum gas required for this chain
    has_enough: bool           # True if current_balance >= gas_needed

    # Resolution details (populated when strategy != SKIP)
    source_chain: Optional[str] = None
    usdc_cost: Optional[float] = None       # USDC spent to acquire gas
    gas_to_acquire: Optional[float] = None  # Native gas amount to receive

    # Relay.link specific
    relay_quote: Optional[RelayLinkQuote] = None

    # Gelato specific
    gelato_task_id: Optional[str] = None

    # Error (when strategy == MANUAL)
    error: Optional[str] = None

    # Dry-run mode
    dry_run: bool = True

    # Execution status
    status: str = "pending"   # "skipped" | "quoted" | "submitted" | "confirmed" | "failed"

    # Metadata
    meta: Dict[str, Any] = field(default_factory=dict)


class GasResolverError(Exception):
    """Raised when gas resolution completely fails (all strategies exhausted)."""
    pass


# ─── GasResolver ──────────────────────────────────────────────────────────────

class GasResolver:
    """
    Cold Start Gas Resolver — solves the "0 native gas" problem autonomously.

    Strategy priority:
      1. SKIP         — already have enough gas
      2. RELAY_LINK   — cross-chain USDC → native gas via Relay.link
      3. GELATO_RELAY — gasless relay if Relay.link is unavailable/fallback
      4. MANUAL       — clear error with manual instructions

    Relay.link lesson (p13 — 2026-03-25):
      After calling POST /quote, ALWAYS verify the status field.
      If status == "fallback", liquidity is insufficient and the swap
      will fail or give a very poor rate. Do NOT proceed in that case.
    """

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._web3_cache: Dict[str, Web3] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve(
        self,
        wallet: str,
        chain: str,
        source_chain: str = "base",
        usdc_budget: Optional[float] = None,  # Max USDC to spend (None = auto)
        dry_run: bool = True,
        private_key: Optional[str] = None,
        # Gelato Phase 2 — optional calldata to relay gaslessly
        gelato_target: Optional[str] = None,   # Contract to call via Gelato
        gelato_calldata: Optional[str] = None, # ABI-encoded calldata
        gelato_gas_limit: Optional[int] = None,
    ) -> GasResolveResult:
        """
        Resolve the cold start gas problem for a given wallet on a chain.

        Args:
            wallet:            Wallet address (0x...)
            chain:             Target chain that needs gas ("polygon", "base", etc.)
            source_chain:      Chain where USDC is available (default: "base")
            usdc_budget:       Max USDC to spend on gas acquisition (default: auto ~$2)
            dry_run:           If True, return quote without executing (default: True)
            private_key:       Hex private key (overrides vault lookup for live mode)
            gelato_target:     [Phase 2] Contract to relay gaslessly via Gelato
            gelato_calldata:   [Phase 2] ABI-encoded calldata for Gelato relay
            gelato_gas_limit:  [Phase 2] Optional gas limit for Gelato relay

        Returns:
            GasResolveResult with strategy and execution details

        Raises:
            GasResolverError: if all strategies fail (includes clear manual instructions)
        """
        chain        = chain.lower()
        source_chain = source_chain.lower()

        if chain not in CHAINS:
            raise GasResolverError(
                f"Unsupported chain '{chain}'. Supported: {list(CHAINS.keys())}"
            )
        if source_chain not in CHAINS:
            raise GasResolverError(
                f"Unsupported source_chain '{source_chain}'. Supported: {list(CHAINS.keys())}"
            )

        wallet = Web3.to_checksum_address(wallet)
        gas_needed = GAS_MINIMUMS.get(chain, 0.001)
        chain_info = CHAINS[chain]

        logger.info(
            f"[GasResolver] Checking {chain_info['native_symbol']} balance for "
            f"{wallet[:10]}... on {chain} | min_required={gas_needed}"
        )

        # ── Step 1: Check current native gas balance ─────────────────────────
        current_balance = self.check_native_balance(wallet, chain)
        has_enough = current_balance >= gas_needed

        logger.info(
            f"[GasResolver] Balance: {current_balance:.6f} {chain_info['native_symbol']} | "
            f"Required: {gas_needed} | Has enough: {has_enough}"
        )

        # ── Step 2: Already have gas → SKIP ─────────────────────────────────
        if has_enough:
            logger.info(
                f"[GasResolver] ✅ Already have {current_balance:.6f} "
                f"{chain_info['native_symbol']} — no action needed"
            )
            return GasResolveResult(
                strategy=GasStrategy.SKIP,
                chain=chain,
                wallet_address=wallet,
                current_balance=current_balance,
                gas_needed=gas_needed,
                has_enough=True,
                dry_run=dry_run,
                status="skipped",
                meta={"chain_name": chain_info["name"]},
            )

        # ── Step 3: Try Relay.link ───────────────────────────────────────────
        gas_to_acquire = GAS_REQUEST_AMOUNTS.get(chain, gas_needed * 2)
        logger.info(
            f"[GasResolver] Need gas — trying Relay.link | "
            f"target={gas_to_acquire} {chain_info['native_symbol']} | "
            f"source={source_chain}"
        )

        relay_result = self._try_relay_link(
            wallet=wallet,
            chain=chain,
            source_chain=source_chain,
            gas_to_acquire=gas_to_acquire,
            usdc_budget=usdc_budget,
            dry_run=dry_run,
            private_key=private_key,
            current_balance=current_balance,
            gas_needed=gas_needed,
        )

        if relay_result is not None:
            return relay_result

        # ── Step 4: Relay.link failed → try Gelato relay ────────────────────
        logger.warning(
            "[GasResolver] Relay.link unavailable/fallback — trying Gelato relay"
        )

        gelato_result = self._try_gelato_relay(
            wallet=wallet,
            chain=chain,
            current_balance=current_balance,
            gas_needed=gas_needed,
            dry_run=dry_run,
            target=gelato_target,
            calldata=gelato_calldata,
            gas_limit=gelato_gas_limit,
        )

        if gelato_result is not None:
            return gelato_result

        # ── Step 5: All strategies failed → MANUAL ──────────────────────────
        native_symbol = chain_info["native_symbol"]
        error_msg = (
            f"Need {gas_needed} {native_symbol} on {chain_info['name']} "
            f"(chain_id={chain_info['chain_id']}) to execute transactions. "
            f"Current balance: {current_balance:.6f} {native_symbol}. "
            f"Manual options:\n"
            f"  1. Send {gas_needed} {native_symbol} to {wallet}\n"
            f"  2. Use a faucet (testnet only)\n"
            f"  3. Bridge from another chain via Relay.link UI: https://relay.link\n"
            f"  4. Use CEX to send {native_symbol} directly to wallet"
        )

        logger.error(f"[GasResolver] ❌ All strategies failed: {error_msg}")

        return GasResolveResult(
            strategy=GasStrategy.MANUAL,
            chain=chain,
            wallet_address=wallet,
            current_balance=current_balance,
            gas_needed=gas_needed,
            has_enough=False,
            source_chain=source_chain,
            dry_run=dry_run,
            status="failed",
            error=error_msg,
            meta={"chain_name": chain_info["name"]},
        )

    # ── Balance Check ─────────────────────────────────────────────────────────

    def check_native_balance(self, wallet: str, chain: str) -> float:
        """
        Check native token balance (ETH, POL, etc.) for a wallet.

        Args:
            wallet: Checksummed wallet address
            chain:  Chain name ("polygon", "base", etc.)

        Returns:
            Balance in human units (e.g., 0.5 for 0.5 POL)
        """
        try:
            w3 = self._get_web3(chain)
            balance_raw = w3.eth.get_balance(wallet)
            decimals = CHAINS[chain]["native_decimals"]
            return balance_raw / (10 ** decimals)
        except Exception as e:
            logger.warning(f"[GasResolver] Balance check failed for {chain}: {e}")
            # Return 0 to trigger gas acquisition on RPC failure
            # This is the safe default (better to attempt resolution than skip)
            return 0.0

    def check_usdc_balance(self, wallet: str, chain: str) -> float:
        """
        Check USDC balance on a chain.

        Args:
            wallet: Checksummed wallet address
            chain:  Chain name

        Returns:
            USDC balance in human units (6 decimals)
        """
        usdc_address = USDC_ADDRESSES.get(chain)
        if not usdc_address:
            logger.warning(f"[GasResolver] No USDC address known for chain '{chain}'")
            return 0.0

        try:
            w3 = self._get_web3(chain)
            token = w3.eth.contract(
                address=Web3.to_checksum_address(usdc_address),
                abi=ERC20_BALANCE_ABI,
            )
            balance_raw = token.functions.balanceOf(wallet).call()
            return balance_raw / 1_000_000  # USDC has 6 decimals
        except Exception as e:
            logger.warning(f"[GasResolver] USDC balance check failed for {chain}: {e}")
            return 0.0

    # ── Relay.link Strategy ───────────────────────────────────────────────────

    def get_relay_link_quote(
        self,
        wallet: str,
        source_chain: str,
        destination_chain: str,
        gas_to_acquire: float,
        usdc_budget: Optional[float] = None,
    ) -> RelayLinkQuote:
        """
        Get a quote from Relay.link for cross-chain gas acquisition.

        POST https://api.relay.link/quote
        {
            "user": "0x...",
            "originChainId": 8453,
            "destinationChainId": 137,
            "originCurrency": "0x833589...",   ← USDC on Base
            "destinationCurrency": "0x1010...", ← POL on Polygon
            "amount": "1000000",               ← 1 USDC (raw, 6 decimals)
            "tradeType": "EXACT_OUTPUT"        ← we want exactly X POL
        }

        IMPORTANT (p13 lesson): Always check the "status" field in the response.
        If status == "fallback", liquidity is insufficient → do NOT proceed.

        Args:
            wallet:              Wallet address (0x...)
            source_chain:        Chain with USDC ("base", "polygon", etc.)
            destination_chain:   Chain that needs gas ("polygon", "base", etc.)
            gas_to_acquire:      Amount of native gas to receive (human units)
            usdc_budget:         Max USDC to spend (None = let Relay.link decide)

        Returns:
            RelayLinkQuote with steps and status

        Raises:
            GasResolverError: if the quote fails or Relay.link is unavailable
        """
        src_chain_info  = CHAINS[source_chain]
        dest_chain_info = CHAINS[destination_chain]

        origin_currency = USDC_ADDRESSES.get(source_chain)
        if not origin_currency:
            raise GasResolverError(
                f"No USDC address configured for source chain '{source_chain}'"
            )

        destination_currency = dest_chain_info["native_address"]
        dest_decimals        = dest_chain_info["native_decimals"]

        # Convert gas_to_acquire to raw units
        dest_amount_raw = int(gas_to_acquire * (10 ** dest_decimals))

        # Build quote payload
        payload: Dict[str, Any] = {
            "user":                 wallet,
            "originChainId":        src_chain_info["chain_id"],
            "destinationChainId":   dest_chain_info["chain_id"],
            "originCurrency":       origin_currency,
            "destinationCurrency":  destination_currency,
            "amount":               str(dest_amount_raw),
            "tradeType":            "EXACT_OUTPUT",  # We want exactly X gas
        }

        logger.info(
            f"[Relay.link] Requesting quote: {gas_to_acquire} "
            f"{dest_chain_info['native_symbol']} on {destination_chain} "
            f"← USDC on {source_chain}"
        )
        logger.debug(f"[Relay.link] POST /quote payload: {payload}")

        try:
            resp = requests.post(
                f"{RELAY_LINK_API}/quote",
                json=payload,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.ConnectionError as e:
            raise GasResolverError(f"Relay.link unreachable: {e}") from e
        except requests.Timeout:
            raise GasResolverError(
                f"Relay.link timeout after {self.timeout}s"
            )

        if resp.status_code == 400:
            try:
                err_data = resp.json()
            except Exception:
                err_data = {"message": resp.text[:200]}
            raise GasResolverError(
                f"Relay.link 400 bad request: {err_data.get('message', err_data)}"
            )

        if resp.status_code not in (200, 201):
            raise GasResolverError(
                f"Relay.link /quote returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as e:
            raise GasResolverError(f"Relay.link returned invalid JSON: {e}") from e

        # ── CRITICAL: Check status field (p13 lesson) ────────────────────────
        # Relay.link may return status="fallback" if liquidity is low
        # In that case: the swap will proceed but at a very poor rate
        # We treat "fallback" as a failure and move to the next strategy
        quote_status = data.get("status", "unknown")
        logger.info(f"[Relay.link] Quote status: {quote_status}")

        if quote_status == "fallback":
            raise GasResolverError(
                f"Relay.link returned status='fallback' — insufficient liquidity for "
                f"{gas_to_acquire} {dest_chain_info['native_symbol']} on {destination_chain}. "
                f"This was the failure mode observed on 2026-03-25 (p13). "
                f"Proceeding to Gelato relay fallback."
            )

        # Parse steps (transactions to execute)
        steps = data.get("steps", [])

        # Extract cost info from steps
        origin_amount_raw  = 0
        origin_amount      = 0.0
        dest_amount_actual = dest_amount_raw

        if steps:
            first_step = steps[0]
            items = first_step.get("items", [])
            if items:
                first_item = items[0]
                data_field = first_item.get("data", {})
                origin_amount_raw = int(data_field.get("value", "0") or 0)

            # Try to get origin amount from fees/details
            details = data.get("details", {})
            curr_in = details.get("currencyIn", {})
            amount_in = curr_in.get("amount", "0")
            if amount_in and amount_in != "0":
                origin_amount_raw = int(amount_in)

        if origin_amount_raw > 0:
            origin_amount = origin_amount_raw / 1_000_000  # USDC is 6 decimals

        # Check usdc_budget constraint
        if usdc_budget is not None and origin_amount > usdc_budget:
            raise GasResolverError(
                f"Relay.link quote ({origin_amount:.4f} USDC) exceeds budget "
                f"({usdc_budget:.4f} USDC)"
            )

        # Generate a pseudo request_id from response (Relay.link may not return one directly)
        request_id = data.get("requestId", data.get("id", f"relay_{int(time.time())}"))

        # Expiry
        expiry_ts = int(data.get("expirationTime", time.time() + 180))  # default 3 min

        return RelayLinkQuote(
            request_id=request_id,
            origin_chain_id=src_chain_info["chain_id"],
            destination_chain_id=dest_chain_info["chain_id"],
            origin_currency=origin_currency,
            destination_currency=destination_currency,
            origin_amount_raw=origin_amount_raw,
            origin_amount=origin_amount,
            destination_amount_raw=dest_amount_raw,
            destination_amount=gas_to_acquire,
            status=quote_status,
            expiry_ts=expiry_ts,
            steps=steps,
            raw_response=data,
        )

    def check_relay_request_status(self, wallet: str) -> Dict[str, Any]:
        """
        Check the status of recent Relay.link requests for a wallet.

        GET https://api.relay.link/requests?user=ADDR&limit=1

        Returns the latest request status dict.
        P13 lesson: check this after submitting to confirm execution,
        because status "fallback" means the swap was not optimal.
        """
        try:
            resp = requests.get(
                f"{RELAY_LINK_API}/requests",
                params={"user": wallet, "limit": 1},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            requests_list = data.get("requests", [])
            return requests_list[0] if requests_list else {}
        except Exception as e:
            logger.warning(f"[Relay.link] Failed to check request status: {e}")
            return {}

    # ── Gelato Relay Fallback ─────────────────────────────────────────────────

    def get_gelato_relay_info(self, chain: str) -> Dict[str, Any]:
        """
        Get Gelato relay information for gasless execution on a chain.

        Gelato 1Balance allows sponsoring transactions without native gas.
        The caller (sponsor) pays in USDC/stable on any chain; the user
        gets the transaction executed gaslessly on the target chain.

        Reference: https://docs.gelato.network/developer-services/relay

        Returns a dict with relay endpoint and requirements.
        """
        chain_info = CHAINS.get(chain, {})
        chain_id   = chain_info.get("chain_id", 0)

        return {
            "relay_endpoint": f"https://relay.gelato.network/relays/{chain_id}",
            "supported_chain": chain in CHAINS,
            "chain_id": chain_id,
            "chain": chain,
            "description": (
                "Gelato 1Balance Relay — sponsor gas on any chain using USDC. "
                "Requires Gelato API key and 1Balance deposit. "
                "See: https://docs.gelato.network/developer-services/relay/gelato-1balance"
            ),
            "setup_required": True,  # Needs Gelato API key + 1Balance deposit
        }

    def _load_gelato_api_key(self) -> Optional[str]:
        """
        Load GELATO_API_KEY from environment variable or vault.

        Priority:
          1. GELATO_API_KEY environment variable
          2. vault.sh read GELATO_API_KEY

        Returns None if the key is not configured anywhere.
        To configure:
            ~/.pi/agent/skills/agent-vault/vault.sh add GELATO_API_KEY "your-key"
        """
        import os
        import subprocess

        # 1. Environment variable (highest priority — CI/CD / docker)
        key = os.environ.get("GELATO_API_KEY", "").strip()
        if key:
            logger.debug("[Gelato] API key loaded from GELATO_API_KEY env var")
            return key

        # 2. Vault
        try:
            result = subprocess.run(
                [VAULT_PATH, "read", "GELATO_API_KEY"],
                capture_output=True, text=True, timeout=5,
            )
            key = result.stdout.strip()
            if key and result.returncode == 0:
                logger.debug("[Gelato] API key loaded from vault")
                return key
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"[Gelato] Vault lookup failed: {e}")

        return None

    def check_gelato_task_status(self, task_id: str) -> Dict[str, Any]:
        """
        Check the status of a Gelato relay task.

        GET https://relay.gelato.network/tasks/status/{taskId}

        Response fields:
            taskState:       "CheckPending" | "ExecPending" | "ExecSuccess" | "ExecReverted" | "Cancelled"
            transactionHash: "0x..." (once mined)
            blockNumber:     int
            creationDate:    ISO timestamp
            executionDate:   ISO timestamp (once executed)
            lastCheckMessage: str — last status message from Gelato

        Args:
            task_id: Gelato task ID returned from _execute_gelato_relay()

        Returns:
            Dict with taskState and other status fields.
            Returns {"taskState": "unknown", "error": str} on failure.
        """
        url = f"{GELATO_TASK_STATUS_URL}/{task_id}"
        try:
            resp = requests.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    f"[Gelato] Task {task_id[:16]}... state: {data.get('taskState', 'unknown')}"
                )
                return data
            else:
                logger.warning(f"[Gelato] Task status HTTP {resp.status_code}: {resp.text[:100]}")
                return {"taskState": "unknown", "httpStatus": resp.status_code}
        except Exception as e:
            logger.warning(f"[Gelato] Task status check failed: {e}")
            return {"taskState": "unknown", "error": str(e)}

    def _execute_gelato_relay(
        self,
        chain: str,
        target: str,
        calldata: str,
        gelato_api_key: str,
        gas_limit: Optional[int] = None,
        retries: int = 2,
    ) -> str:
        """
        Execute a gasless relay call via Gelato's sponsored-call API.

        This is the Phase 2 live implementation of the Gelato fallback.
        The transaction is sponsored by the Gelato 1Balance account linked
        to the API key — the user pays no native gas.

        POST https://relay.gelato.network/relays/v2/sponsored-call
        {
            "chainId": "137",
            "target":  "0x...",
            "data":    "0x...",
            "sponsorApiKey": "your-api-key",
            "gasLimit":      "300000"    (optional)
        }

        Response:
            {"taskId": "0x1234abcd..."}

        Track status with check_gelato_task_status(taskId).

        Args:
            chain:          Target chain ("polygon", "base", etc.)
            target:         Contract address to call (0x...)
            calldata:       ABI-encoded calldata (0x...)
            gelato_api_key: Gelato 1Balance API key
            gas_limit:      Optional gas limit (default: Gelato auto-estimates)
            retries:        Number of retry attempts on transient failures

        Returns:
            taskId (str) — Gelato task ID for status tracking

        Raises:
            GasResolverError: if the relay submission fails after retries
        """
        chain_info = CHAINS.get(chain)
        if not chain_info:
            raise GasResolverError(f"Unsupported chain for Gelato relay: '{chain}'")

        chain_id = chain_info["chain_id"]
        endpoint = f"{GELATO_RELAY_API}/relays/v2/sponsored-call"

        payload: Dict[str, Any] = {
            "chainId":      str(chain_id),
            "target":       Web3.to_checksum_address(target),
            "data":         calldata,
            "sponsorApiKey": gelato_api_key,
        }
        if gas_limit is not None:
            payload["gasLimit"] = str(gas_limit)

        headers = {
            "Content-Type": "application/json",
            "Accept":        "application/json",
        }

        logger.info(
            f"[Gelato] Submitting sponsored-call relay | "
            f"chain={chain} (id={chain_id}) | target={target[:16]}..."
        )
        logger.debug(f"[Gelato] POST {endpoint} | payload={payload}")

        last_error: Optional[str] = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.ConnectionError as e:
                last_error = f"Connection error: {e}"
                logger.warning(f"[Gelato] Attempt {attempt}/{retries} — {last_error}")
                if attempt < retries:
                    time.sleep(2 ** attempt)
                continue
            except requests.Timeout:
                last_error = f"Timeout after {self.timeout}s"
                logger.warning(f"[Gelato] Attempt {attempt}/{retries} — {last_error}")
                if attempt < retries:
                    time.sleep(2 ** attempt)
                continue

            # ── Handle HTTP errors ───────────────────────────────────────────
            if resp.status_code == 401:
                raise GasResolverError(
                    "Gelato relay returned HTTP 401 — invalid or expired GELATO_API_KEY. "
                    "Check your key at https://app.gelato.network and update vault: "
                    "~/.pi/agent/skills/agent-vault/vault.sh add GELATO_API_KEY <new-key>"
                )

            if resp.status_code == 400:
                try:
                    err_data = resp.json()
                except Exception:
                    err_data = {"message": resp.text[:200]}
                error_msg = err_data.get("message", str(err_data))
                raise GasResolverError(
                    f"Gelato relay 400 bad request: {error_msg}. "
                    f"Check chain_id={chain_id}, target={target}, and calldata format."
                )

            if resp.status_code == 429:
                last_error = "Rate limited (HTTP 429)"
                logger.warning(f"[Gelato] {last_error} — waiting before retry {attempt}/{retries}")
                if attempt < retries:
                    time.sleep(5)
                continue

            if resp.status_code not in (200, 201):
                last_error = f"HTTP {resp.status_code}: {resp.text[:100]}"
                logger.warning(f"[Gelato] Attempt {attempt}/{retries} — {last_error}")
                if attempt < retries:
                    time.sleep(2)
                continue

            # ── Parse response ───────────────────────────────────────────────
            try:
                data = resp.json()
            except Exception as e:
                raise GasResolverError(
                    f"Gelato relay returned invalid JSON: {e}"
                ) from e

            task_id = data.get("taskId")
            if not task_id:
                raise GasResolverError(
                    f"Gelato relay response missing 'taskId' field. "
                    f"Full response: {data}"
                )

            logger.info(
                f"[Gelato] ✅ Task submitted successfully | taskId={task_id}"
            )
            return task_id

        # All retries exhausted
        raise GasResolverError(
            f"Gelato relay failed after {retries} attempts. Last error: {last_error}"
        )

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _try_relay_link(
        self,
        wallet: str,
        chain: str,
        source_chain: str,
        gas_to_acquire: float,
        usdc_budget: Optional[float],
        dry_run: bool,
        private_key: Optional[str],
        current_balance: float,
        gas_needed: float,
    ) -> Optional[GasResolveResult]:
        """
        Attempt gas acquisition via Relay.link.

        Returns GasResolveResult if successful, None if Relay.link fails/unavailable.
        """
        chain_info = CHAINS[chain]
        try:
            quote = self.get_relay_link_quote(
                wallet=wallet,
                source_chain=source_chain,
                destination_chain=chain,
                gas_to_acquire=gas_to_acquire,
                usdc_budget=usdc_budget,
            )

            logger.info(
                f"[Relay.link] ✅ Quote OK | "
                f"Cost: {quote.origin_amount:.4f} USDC | "
                f"Receive: {quote.destination_amount:.6f} {chain_info['native_symbol']} | "
                f"Status: {quote.status}"
            )

            result = GasResolveResult(
                strategy=GasStrategy.RELAY_LINK,
                chain=chain,
                wallet_address=wallet,
                current_balance=current_balance,
                gas_needed=gas_needed,
                has_enough=False,
                source_chain=source_chain,
                usdc_cost=quote.origin_amount,
                gas_to_acquire=quote.destination_amount,
                relay_quote=quote,
                dry_run=dry_run,
                status="quoted",
                meta={
                    "chain_name": chain_info["name"],
                    "relay_request_id": quote.request_id,
                    "relay_steps_count": len(quote.steps),
                    "quote_expiry": quote.expiry_ts,
                },
            )

            if dry_run:
                logger.info("[Relay.link] 🔵 DRY-RUN — quote built but not submitted")
                return result

            # Live mode: execute the steps
            logger.info(f"[Relay.link] Executing {len(quote.steps)} step(s)...")
            executed = self._execute_relay_link_steps(
                quote=quote,
                wallet=wallet,
                source_chain=source_chain,
                private_key=private_key,
            )
            result.status = "submitted" if executed else "failed"

            # Verify execution via GET /requests
            if executed:
                time.sleep(3)  # Brief pause before checking
                req_status = self.check_relay_request_status(wallet)
                actual_status = req_status.get("status", "unknown")
                logger.info(f"[Relay.link] Post-execution status: {actual_status}")

                if actual_status == "fallback":
                    logger.warning(
                        "[Relay.link] ⚠️ Execution returned status='fallback' "
                        "(p13 lesson: this means the swap was not optimal). "
                        "Gas may have been acquired at a poor rate."
                    )
                    result.meta["relay_post_status"] = "fallback"
                elif actual_status in ("success", "confirmed", "complete"):
                    result.status = "confirmed"
                    logger.info(
                        f"[Relay.link] ✅ Gas acquired successfully! "
                        f"~{gas_to_acquire} {chain_info['native_symbol']} on {chain}"
                    )

            return result

        except GasResolverError as e:
            logger.warning(f"[GasResolver] Relay.link strategy failed: {e}")
            return None
        except Exception as e:
            logger.warning(f"[GasResolver] Relay.link unexpected error: {e}")
            return None

    def _execute_relay_link_steps(
        self,
        quote: RelayLinkQuote,
        wallet: str,
        source_chain: str,
        private_key: Optional[str],
    ) -> bool:
        """
        Execute the Relay.link steps (sign and send transactions).

        Each step may be an approval + swap transaction on the source chain.

        Returns True if steps were submitted, False on error.
        """
        if not quote.steps:
            logger.warning("[Relay.link] No steps to execute in quote")
            return False

        # Resolve private key
        if private_key is None:
            try:
                import subprocess
                result = subprocess.run(
                    [VAULT_PATH, "read", "ETH_PRIVATE_KEY"],
                    capture_output=True, text=True, check=True,
                )
                private_key = result.stdout.strip()
            except Exception as e:
                logger.error(f"[Relay.link] Cannot read private key from vault: {e}")
                return False

        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        from eth_account import Account
        account = Account.from_key(private_key)

        if account.address.lower() != wallet.lower():
            logger.error(
                f"[Relay.link] Key mismatch: key→{account.address}, wallet→{wallet}"
            )
            return False

        w3 = self._get_web3(source_chain)
        chain_id = CHAINS[source_chain]["chain_id"]

        for i, step in enumerate(quote.steps):
            items = step.get("items", [])
            for j, item in enumerate(items):
                tx_data = item.get("data", {})
                if not tx_data:
                    continue

                try:
                    nonce = w3.eth.get_transaction_count(wallet)
                    gas_price = w3.eth.gas_price

                    tx = {
                        "to":       tx_data.get("to"),
                        "data":     tx_data.get("data", "0x"),
                        "value":    int(tx_data.get("value", "0") or 0),
                        "gas":      int(tx_data.get("gas", 200_000) or 200_000),
                        "gasPrice": gas_price,
                        "nonce":    nonce,
                        "chainId":  chain_id,
                    }

                    signed = account.sign_transaction(tx)
                    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

                    if receipt.status != 1:
                        logger.error(
                            f"[Relay.link] Step {i+1}/{len(quote.steps)} "
                            f"item {j+1} reverted: {tx_hash.hex()}"
                        )
                        return False

                    logger.info(
                        f"[Relay.link] ✅ Step {i+1}/{len(quote.steps)} "
                        f"submitted: {tx_hash.hex()}"
                    )

                except Exception as e:
                    logger.error(
                        f"[Relay.link] Failed to execute step {i+1}: {e}"
                    )
                    return False

        return True

    def _try_gelato_relay(
        self,
        wallet: str,
        chain: str,
        current_balance: float,
        gas_needed: float,
        dry_run: bool,
        target: Optional[str] = None,
        calldata: Optional[str] = None,
        gas_limit: Optional[int] = None,
    ) -> Optional[GasResolveResult]:
        """
        Attempt gasless execution via Gelato relay.

        Phase 2: Live implementation — calls Gelato's sponsored-call API using
        GELATO_API_KEY from vault or env var, then verifies the returned taskId.

        In dry_run mode: returns a structured result without submitting to Gelato.
        In live mode: submits the transaction to Gelato and returns taskId.

        Args:
            wallet:       Wallet address (for logging/tracking)
            chain:        Target chain that needs gasless execution
            current_balance: Current native gas balance
            gas_needed:   Minimum gas required
            dry_run:      If True, skip actual Gelato submission
            target:       Contract to call gaslessly (required for live mode)
            calldata:     ABI-encoded calldata (required for live mode)
            gas_limit:    Optional gas limit override

        Returns GasResolveResult if Gelato is available/configured, None otherwise.
        """
        chain_info = CHAINS[chain]

        gelato_info = self.get_gelato_relay_info(chain)

        if not gelato_info["supported_chain"]:
            logger.warning(f"[Gelato] Chain '{chain}' not supported by Gelato relay")
            return None

        logger.info(
            f"[Gelato] Relay available for {chain} at {gelato_info['relay_endpoint']}"
        )

        # ── Load API key (Phase 2 — vault or env) ────────────────────────────
        gelato_key = self._load_gelato_api_key()

        if not dry_run:
            if not gelato_key:
                logger.warning(
                    "[Gelato] GELATO_API_KEY not configured — cannot execute live relay. "
                    "Add to vault: ~/.pi/agent/skills/agent-vault/vault.sh add GELATO_API_KEY <key> "
                    "or set GELATO_API_KEY env var. "
                    "Get a key at: https://app.gelato.network"
                )
                return None

            if not target or not calldata:
                logger.warning(
                    "[Gelato] Live mode requires 'target' and 'calldata' for sponsored-call relay. "
                    "Provide the contract address and calldata to relay gaslessly."
                )
                return None

        # ── Dry-run: return structured quote without submitting ───────────────
        if dry_run:
            has_key = gelato_key is not None
            logger.info(
                f"[Gelato] 🔵 DRY-RUN — API key {'found' if has_key else 'NOT configured'}. "
                f"Would relay via {gelato_info['relay_endpoint']}. "
                f"Set GELATO_API_KEY to enable live mode."
            )
            return GasResolveResult(
                strategy=GasStrategy.GELATO_RELAY,
                chain=chain,
                wallet_address=wallet,
                current_balance=current_balance,
                gas_needed=gas_needed,
                has_enough=False,
                dry_run=True,
                status="quoted",
                gelato_task_id=None,
                meta={
                    "chain_name": chain_info["name"],
                    "gelato_relay_endpoint": gelato_info["relay_endpoint"],
                    "gelato_api_key_configured": has_key,
                    "gelato_target": target or "(no target provided in dry-run)",
                    "description": (
                        "Gelato 1Balance relay selected as fallback (dry-run). "
                        f"Would execute transactions on {chain} without native gas. "
                        f"API key {'configured ✅' if has_key else 'NOT configured ❌ — see vault'}. "
                        "Docs: https://docs.gelato.network/developer-services/relay/gelato-1balance"
                    ),
                },
            )

        # ── Live mode: submit to Gelato sponsored-call API ───────────────────
        logger.info(
            f"[Gelato] 🚀 LIVE — submitting sponsored-call relay on {chain} | "
            f"target={target[:16]}... | calldata_len={len(calldata)}"
        )

        try:
            task_id = self._execute_gelato_relay(
                chain=chain,
                target=target,
                calldata=calldata,
                gelato_api_key=gelato_key,
                gas_limit=gas_limit,
            )
        except GasResolverError as e:
            logger.error(f"[Gelato] Live relay failed: {e}")
            return None

        # ── Verify taskId is valid (non-empty, hex-like string) ──────────────
        if not task_id or len(task_id) < 8:
            logger.error(f"[Gelato] Invalid taskId returned: {repr(task_id)}")
            return None

        # ── Brief wait + initial status check ────────────────────────────────
        logger.info(f"[Gelato] Waiting 3s before checking task status...")
        time.sleep(3)

        task_status = self.check_gelato_task_status(task_id)
        task_state = task_status.get("taskState", "unknown")
        tx_hash = task_status.get("transactionHash")

        logger.info(
            f"[Gelato] ✅ Task {task_id[:16]}... | state={task_state} | "
            f"txHash={tx_hash or 'pending'}"
        )

        # Map Gelato task states to our status
        if task_state in ("ExecSuccess",):
            status = "confirmed"
        elif task_state in ("ExecPending", "CheckPending", "WaitingForConfirmation"):
            status = "submitted"
        elif task_state in ("ExecReverted", "Cancelled"):
            logger.warning(f"[Gelato] Task {task_state} — relay may have failed")
            status = "failed"
        else:
            status = "submitted"  # Optimistic default for unknown states

        return GasResolveResult(
            strategy=GasStrategy.GELATO_RELAY,
            chain=chain,
            wallet_address=wallet,
            current_balance=current_balance,
            gas_needed=gas_needed,
            has_enough=False,
            dry_run=False,
            status=status,
            gelato_task_id=task_id,
            meta={
                "chain_name":          chain_info["name"],
                "gelato_relay_endpoint": gelato_info["relay_endpoint"],
                "gelato_task_id":       task_id,
                "gelato_task_state":    task_state,
                "gelato_tx_hash":       tx_hash or "",
                "gelato_target":        target,
            },
        )

    def _get_web3(self, chain: str) -> Web3:
        """Get (cached) Web3 instance for a chain."""
        if chain not in self._web3_cache:
            rpc = CHAINS[chain]["rpc"]
            w3 = Web3(Web3.HTTPProvider(
                rpc,
                request_kwargs={"timeout": self.timeout},
            ))
            self._web3_cache[chain] = w3
        return self._web3_cache[chain]


# ─── Module-level convenience function ────────────────────────────────────────

_default_resolver: Optional[GasResolver] = None


def resolve_gas(
    wallet: str,
    chain: str,
    source_chain: str = "base",
    usdc_budget: Optional[float] = None,
    dry_run: bool = True,
    private_key: Optional[str] = None,
) -> GasResolveResult:
    """
    Convenience function — resolve the cold start gas problem.

    Defaults to dry_run=True for safety.

    Example:
        from gas import resolve_gas, GasStrategy

        result = resolve_gas("0xYourWallet", "polygon", source_chain="base", dry_run=True)

        if result.strategy == GasStrategy.SKIP:
            print(f"Already have {result.current_balance:.4f} POL — good to go!")
        elif result.strategy == GasStrategy.RELAY_LINK:
            print(f"Relay.link: spend {result.usdc_cost:.4f} USDC → get {result.gas_to_acquire:.4f} POL")
        elif result.strategy == GasStrategy.GELATO_RELAY:
            print("Use Gelato gasless relay (GELATO_API_KEY required)")
        elif result.strategy == GasStrategy.MANUAL:
            print(f"Manual action needed: {result.error}")
    """
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = GasResolver()
    return _default_resolver.resolve(
        wallet=wallet,
        chain=chain,
        source_chain=source_chain,
        usdc_budget=usdc_budget,
        dry_run=dry_run,
        private_key=private_key,
    )
