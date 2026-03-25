"""
bridge.py — Across Protocol bridge module for AutoSwap (p14)

Bridges tokens cross-chain using Across Protocol's SpokePool depositV3.
Ported from p13/scripts/bridge-to-polygon.js — rewritten in Python.

Usage:
    from bridge import Bridge, BridgeResult

    bridge = Bridge()
    result = bridge.bridge(
        token="USDC",
        amount=0.01,
        from_chain="base",
        to_chain="polygon",
        wallet="0x...",
        dry_run=True,          # simulate only — no tx submitted
    )
    print(result.tx_data)
    print(result.output_amount)

    # Live mode (reads ETH_PRIVATE_KEY from vault):
    result = bridge.bridge("USDC", 5.0, "base", "polygon", "0x...", dry_run=False)
"""

import time
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple

import requests
from web3 import Web3
from eth_account import Account

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

ACROSS_API = "https://app.across.to/api"
VAULT_PATH = "/Users/sam/.pi/agent/skills/agent-vault/vault.sh"

# Across SpokePool addresses (hardcoded per task spec)
SPOKE_POOLS: Dict[str, str] = {
    "base":    "0x09aea4b2242abC8bb4BB78D537A67a245A7bEC64",
    "polygon": "0x9295ee1d8C5b022Be115A2AD3c30C72E34e7F096",
}

# Chain configuration
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

# Token registry: {chain: {symbol: {address, decimals}}}
# USDC addresses per task spec
TOKENS: Dict[str, Dict] = {
    "base": {
        "USDC": {
            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "decimals": 6,
        },
        "WETH": {
            "address": "0x4200000000000000000000000000000000000006",
            "decimals": 18,
        },
        "ETH": {
            "address": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
            "decimals": 18,
        },
    },
    "polygon": {
        "USDC": {
            # Polygon native USDC (Circle's native issuance)
            "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "decimals": 6,
        },
        "USDC_E": {
            # Polygon bridged USDC.e
            "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "decimals": 6,
        },
        "WETH": {
            "address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
            "decimals": 18,
        },
        "WMATIC": {
            "address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
            "decimals": 18,
        },
        "MYST": {
            "address": "0x1379E8886A944d2D9d440b3d88DF536Aea08d9F3",
            "decimals": 18,
        },
    },
}

# ERC-20 ABI (minimal — approve, balanceOf, allowance)
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
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
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
]

# Across SpokePool depositV3 ABI
SPOKE_POOL_ABI = [
    {
        "name": "depositV3",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "depositor",           "type": "address"},
            {"name": "recipient",           "type": "address"},
            {"name": "inputToken",          "type": "address"},
            {"name": "outputToken",         "type": "address"},
            {"name": "inputAmount",         "type": "uint256"},
            {"name": "outputAmount",        "type": "uint256"},
            {"name": "destinationChainId",  "type": "uint256"},
            {"name": "exclusiveRelayer",    "type": "address"},
            {"name": "quoteTimestamp",      "type": "uint32"},
            {"name": "fillDeadline",        "type": "uint32"},
            {"name": "exclusivityDeadline", "type": "uint32"},
            {"name": "message",             "type": "bytes"},
        ],
        "outputs": [],
    }
]

ZERO_ADDRESS     = "0x0000000000000000000000000000000000000000"
FILL_DEADLINE_OFFSET = 3600  # +1 hour from now (same as JS bridge)


# ─── Data structures ───────────────────────────────────────────────────────────

@dataclass
class BridgeFee:
    """Fee quote from Across Protocol suggested-fees API."""
    total_relay_fee_raw: int         # total fee in raw token units
    total_relay_fee: float           # total fee in human units
    lp_fee_raw: int                  # LP portion
    relayer_gas_fee_raw: int         # gas cost portion
    relayer_capital_fee_raw: int     # capital cost portion
    quote_timestamp: int             # Unix timestamp of this quote
    fill_deadline: int               # Absolute Unix ts — deadline for relayer fill
    exclusivity_deadline: int        # Absolute Unix ts — exclusivity period end
    exclusive_relayer: str           # Address of exclusive relayer (or ZERO_ADDRESS)
    estimated_fill_time_sec: int     # Estimated seconds to fill on dest chain
    is_amount_too_low: bool          # True if amount < min bridge amount


@dataclass
class BridgeResult:
    """Result of a Bridge.bridge() call."""
    # Inputs
    token: str
    from_chain: str
    to_chain: str
    wallet_address: str
    input_amount: float
    input_amount_raw: int

    # Quote
    fee: BridgeFee
    output_amount: float             # human units (after fee)
    output_amount_raw: int           # raw units

    # Tx data (ready to sign+send, even in dry-run)
    tx_data: Optional[Dict[str, Any]]

    # Execution results (None in dry-run)
    approve_tx_hash: Optional[str] = None
    deposit_tx_hash: Optional[str] = None
    deposit_block: Optional[int] = None

    # Status
    dry_run: bool = False
    status: str = "pending"          # "dry_run" | "submitted" | "confirmed" | "failed"
    error: Optional[str] = None

    # Extra metadata
    meta: Dict[str, Any] = field(default_factory=dict)


class BridgeError(Exception):
    """Raised when a bridge operation fails."""
    pass


# ─── Bridge ───────────────────────────────────────────────────────────────────

class Bridge:
    """
    Across Protocol bridge module.

    Workflow:
      1. check_routes()     — verify route exists on Across
      2. get_fees()         — fetch quote from suggested-fees API
      3. build_deposit_tx() — encode depositV3 calldata
      4. [live] approve + submit depositV3 + wait for confirmation
      5. [optional] poll destination chain for fill
    """

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._web3_cache: Dict[str, Web3] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def bridge(
        self,
        token: str,
        amount: float,
        from_chain: str,
        to_chain: str,
        wallet: str,
        dry_run: bool = True,
        private_key: Optional[str] = None,
        output_token: Optional[str] = None,
        wait_for_fill: bool = False,
    ) -> BridgeResult:
        """
        Bridge tokens from one chain to another via Across Protocol.

        Args:
            token:        Token symbol (e.g. "USDC")
            amount:       Amount in human units (e.g. 0.01 for 0.01 USDC)
            from_chain:   Source chain ("base" | "polygon")
            to_chain:     Destination chain ("polygon" | "base")
            wallet:       Wallet address (0x...)
            dry_run:      If True, build tx but don't submit (default: True for safety)
            private_key:  Hex private key (overrides vault lookup)
            output_token: Output token on dest chain (default: same symbol as token)
            wait_for_fill: If True, poll destination chain until fill arrives

        Returns:
            BridgeResult with fee quote, tx_data, and (if live) tx hashes

        Raises:
            BridgeError: route unavailable, fees exceed amount, tx reverts
        """
        from_chain   = from_chain.lower()
        to_chain     = to_chain.lower()
        token        = token.upper()
        output_token = (output_token or token).upper()

        # Validate chains
        if from_chain not in CHAINS:
            raise BridgeError(f"Unsupported from_chain '{from_chain}'. Use: {list(CHAINS.keys())}")
        if to_chain not in CHAINS:
            raise BridgeError(f"Unsupported to_chain '{to_chain}'. Use: {list(CHAINS.keys())}")
        if from_chain == to_chain:
            raise BridgeError("from_chain and to_chain must be different")

        # Resolve tokens
        src_info  = self._resolve_token(token, from_chain)
        dest_info = self._resolve_token(output_token, to_chain)

        amount_raw = int(amount * (10 ** src_info["decimals"]))
        wallet     = Web3.to_checksum_address(wallet)

        logger.info(
            f"[Bridge] {token} {amount} {from_chain}→{to_chain} | "
            f"wallet={wallet[:10]}... | dry_run={dry_run}"
        )

        # ── 1. Check route availability ──────────────────────────────────────
        available = self.check_routes(
            input_token=src_info["address"],
            output_token=dest_info["address"],
            from_chain=from_chain,
            to_chain=to_chain,
        )
        if not available:
            raise BridgeError(
                f"Route not available: {token}→{output_token} "
                f"({from_chain}→{to_chain}). "
                f"Token pair may not be supported by Across Protocol."
            )
        logger.info(f"[Bridge] ✅ Route {from_chain}→{to_chain} available")

        # ── 2. Fetch fee quote ───────────────────────────────────────────────
        fee = self.get_fees(
            input_token=src_info["address"],
            output_token=dest_info["address"],
            from_chain=from_chain,
            to_chain=to_chain,
            amount_raw=amount_raw,
            recipient=wallet,
            input_decimals=src_info["decimals"],
        )
        logger.info(
            f"[Bridge] Fee: {fee.total_relay_fee:.6f} {token} | "
            f"ETA: ~{fee.estimated_fill_time_sec}s"
        )

        if fee.is_amount_too_low:
            raise BridgeError(
                f"Amount {amount} {token} is too low for Across bridge "
                f"(fees would exceed or equal the bridged amount)"
            )

        output_amount_raw = amount_raw - fee.total_relay_fee_raw
        if output_amount_raw <= 0:
            raise BridgeError(
                f"Output amount would be ≤ 0 after fees. "
                f"Input: {amount} {token}, Fee: {fee.total_relay_fee:.6f} {token}. "
                f"Increase the bridge amount."
            )
        output_amount = output_amount_raw / (10 ** dest_info["decimals"])

        logger.info(
            f"[Bridge] Input:  {amount:.6f} {token}"
        )
        logger.info(
            f"[Bridge] Output: {output_amount:.6f} {output_token} (after {fee.total_relay_fee:.6f} fee)"
        )

        # ── 3. Build depositV3 tx ────────────────────────────────────────────
        tx_data = self.build_deposit_tx(
            depositor=wallet,
            recipient=wallet,
            input_token=src_info["address"],
            output_token=dest_info["address"],
            input_amount_raw=amount_raw,
            output_amount_raw=output_amount_raw,
            from_chain=from_chain,
            to_chain=to_chain,
            fee=fee,
        )

        result = BridgeResult(
            token=token,
            from_chain=from_chain,
            to_chain=to_chain,
            wallet_address=wallet,
            input_amount=amount,
            input_amount_raw=amount_raw,
            fee=fee,
            output_amount=output_amount,
            output_amount_raw=output_amount_raw,
            tx_data=tx_data,
            dry_run=dry_run,
            meta={
                "src_token_address":  src_info["address"],
                "dest_token_address": dest_info["address"],
                "from_chain_id":      CHAINS[from_chain]["chain_id"],
                "to_chain_id":        CHAINS[to_chain]["chain_id"],
                "spoke_pool":         SPOKE_POOLS[from_chain],
                "output_token":       output_token,
            },
        )

        # ── 4. Dry-run: return early ─────────────────────────────────────────
        if dry_run:
            result.status = "dry_run"
            logger.info("[Bridge] 🔵 DRY-RUN — tx built but not submitted")
            return result

        # ── 5. Live: resolve private key ─────────────────────────────────────
        if private_key is None:
            private_key = self._read_vault("ETH_PRIVATE_KEY")

        # ── 6. Execute: approve + depositV3 ─────────────────────────────────
        try:
            approve_hash, deposit_hash, block = self._execute(
                private_key=private_key,
                wallet=wallet,
                input_token=src_info["address"],
                input_amount_raw=amount_raw,
                tx_data=tx_data,
                from_chain=from_chain,
            )
            result.approve_tx_hash = approve_hash
            result.deposit_tx_hash = deposit_hash
            result.deposit_block   = block
            result.status          = "confirmed"
            logger.info(
                f"[Bridge] ✅ depositV3 confirmed at block {block} | "
                f"tx: {deposit_hash}"
            )
        except Exception as e:
            result.status = "failed"
            result.error  = str(e)
            raise BridgeError(f"Bridge execution failed: {e}") from e

        # ── 7. (Optional) wait for fill on destination ───────────────────────
        if wait_for_fill:
            logger.info(f"[Bridge] Waiting for fill on {to_chain}...")
            filled = self._wait_for_fill(
                output_token_address=dest_info["address"],
                recipient=wallet,
                expected_amount_raw=output_amount_raw,
                to_chain=to_chain,
                timeout_sec=600,
            )
            result.meta["fill_detected"] = filled
            if filled:
                logger.info(f"[Bridge] 🎉 Fill confirmed on {to_chain}!")
            else:
                logger.warning(f"[Bridge] ⚠️ Fill not detected after 10 minutes")

        return result

    def check_routes(
        self,
        input_token: str,
        output_token: str,
        from_chain: str,
        to_chain: str,
    ) -> bool:
        """
        Verify a bridge route is available via Across available-routes API.

        GET https://app.across.to/api/available-routes
            ?originChainId=8453&destinationChainId=137

        Returns True if the token pair route exists.
        """
        from_chain_id = CHAINS[from_chain]["chain_id"]
        to_chain_id   = CHAINS[to_chain]["chain_id"]

        params = {
            "originChainId":      from_chain_id,
            "destinationChainId": to_chain_id,
        }

        try:
            resp = requests.get(
                f"{ACROSS_API}/available-routes",
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            routes = resp.json()
        except requests.RequestException as e:
            logger.warning(f"[Bridge] available-routes check failed: {e}")
            # If the API is down, we proceed optimistically
            # (the deposit will revert if route doesn't exist)
            logger.warning("[Bridge] Proceeding anyway (API unreachable)")
            return True

        if not isinstance(routes, list):
            logger.warning(f"[Bridge] Unexpected routes response format: {type(routes)}")
            return True

        # Search for our exact token pair
        for route in routes:
            if (
                route.get("originToken", "").lower() == input_token.lower()
                and route.get("destinationToken", "").lower() == output_token.lower()
            ):
                logger.debug(f"[Bridge] Route found: {route}")
                return True

        logger.warning(
            f"[Bridge] No exact route for {input_token}→{output_token} | "
            f"Total routes checked: {len(routes)}"
        )
        return False

    def get_fees(
        self,
        input_token: str,
        output_token: str,
        from_chain: str,
        to_chain: str,
        amount_raw: int,
        recipient: str,
        input_decimals: int = 6,
    ) -> BridgeFee:
        """
        Fetch suggested fees from Across Protocol API.

        GET https://app.across.to/api/suggested-fees
            ?inputToken=...&outputToken=...&originChainId=...
            &destinationChainId=...&amount=...&recipient=...

        Returns:
            BridgeFee with total fee breakdown and timing info.
        """
        from_chain_id = CHAINS[from_chain]["chain_id"]
        to_chain_id   = CHAINS[to_chain]["chain_id"]

        params = {
            "inputToken":         input_token,
            "outputToken":        output_token,
            "originChainId":      from_chain_id,
            "destinationChainId": to_chain_id,
            "amount":             str(amount_raw),
            "recipient":          recipient,
        }

        resp = requests.get(
            f"{ACROSS_API}/suggested-fees",
            params=params,
            timeout=self.timeout,
        )

        # Handle 400 errors gracefully — Across returns 400 for amount-too-low
        if resp.status_code == 400:
            try:
                err_data = resp.json()
                if err_data.get("isAmountTooLow") or "too low" in str(err_data.get("error", "")).lower():
                    raise BridgeError(
                        f"Amount is below Across Protocol minimum bridge amount. "
                        f"Try a larger amount. "
                        f"API response: {err_data.get('error', 'amount too low')}"
                    )
                raise BridgeError(f"Across suggested-fees 400: {err_data}")
            except (ValueError, KeyError):
                raise BridgeError(f"Across suggested-fees 400: {resp.text[:200]}")

        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise BridgeError(f"Across suggested-fees error: {data['error']}")

        # Total fee (mirrors JS: BigInt(quote.totalRelayFee.total))
        total_fee_raw = int(data["totalRelayFee"]["total"])

        # Fee breakdown
        lp_fee_raw          = int(data.get("lpFee", {}).get("total", 0))
        relayer_gas_raw     = int(data.get("relayerGasFee", {}).get("total", 0))
        relayer_capital_raw = int(data.get("relayerCapitalFee", {}).get("total", 0))

        # Timing (mirrors JS logic)
        quote_ts = int(data["timestamp"])
        fill_dl  = int(time.time()) + FILL_DEADLINE_OFFSET   # current_time + 1h
        # exclusivityDeadline from API is a *duration* offset from quoteTimestamp
        excl_offset = int(data.get("exclusivityDeadline", 0) or 0)
        excl_dl     = quote_ts + excl_offset

        # Exclusive relayer (or zero address)
        excl_rl = data.get("exclusiveRelayer") or ZERO_ADDRESS

        # Estimate
        est_fill     = int(data.get("estimatedFillTimeSec", 300))
        is_too_low   = bool(data.get("isAmountTooLow", False))

        total_fee_human = total_fee_raw / (10 ** input_decimals)

        return BridgeFee(
            total_relay_fee_raw=total_fee_raw,
            total_relay_fee=total_fee_human,
            lp_fee_raw=lp_fee_raw,
            relayer_gas_fee_raw=relayer_gas_raw,
            relayer_capital_fee_raw=relayer_capital_raw,
            quote_timestamp=quote_ts,
            fill_deadline=fill_dl,
            exclusivity_deadline=excl_dl,
            exclusive_relayer=excl_rl,
            estimated_fill_time_sec=est_fill,
            is_amount_too_low=is_too_low,
        )

    def build_deposit_tx(
        self,
        depositor: str,
        recipient: str,
        input_token: str,
        output_token: str,
        input_amount_raw: int,
        output_amount_raw: int,
        from_chain: str,
        to_chain: str,
        fee: BridgeFee,
    ) -> Dict[str, Any]:
        """
        Encode the depositV3() calldata for the Across SpokePool.

        This is equivalent to:
            spokePool.depositV3(depositor, recipient, inputToken, outputToken,
                                inputAmount, outputAmount, destinationChainId,
                                exclusiveRelayer, quoteTimestamp, fillDeadline,
                                exclusivityDeadline, message)

        Returns:
            Dict with keys: to, data, value, chainId, gas
            (ready to sign with eth_account)
        """
        w3 = self._get_web3(from_chain)
        spoke_pool_addr = SPOKE_POOLS[from_chain]
        to_chain_id     = CHAINS[to_chain]["chain_id"]

        spoke_pool = w3.eth.contract(
            address=Web3.to_checksum_address(spoke_pool_addr),
            abi=SPOKE_POOL_ABI,
        )

        # web3 v7 uses encode_abi() instead of deprecated encodeABI()
        calldata = spoke_pool.encode_abi(
            "depositV3",
            args=[
                Web3.to_checksum_address(depositor),
                Web3.to_checksum_address(recipient),
                Web3.to_checksum_address(input_token),
                Web3.to_checksum_address(output_token),
                input_amount_raw,
                output_amount_raw,
                to_chain_id,
                Web3.to_checksum_address(fee.exclusive_relayer),
                fee.quote_timestamp,      # uint32 — quoteTimestamp
                fee.fill_deadline,        # uint32 — fillDeadline
                fee.exclusivity_deadline, # uint32 — exclusivityDeadline
                b"",                      # bytes  — empty message
            ],
        )

        return {
            "to":      spoke_pool_addr,
            "data":    calldata,
            "value":   "0x0",                         # USDC bridge — no ETH value
            "chainId": CHAINS[from_chain]["chain_id"],
            "gas":     200_000,
        }

    # ── Execution (live mode) ─────────────────────────────────────────────────

    def _execute(
        self,
        private_key: str,
        wallet: str,
        input_token: str,
        input_amount_raw: int,
        tx_data: Dict[str, Any],
        from_chain: str,
    ) -> Tuple[Optional[str], str, int]:
        """
        Sign and submit: ERC-20 approve + depositV3.

        Returns:
            (approve_tx_hash | None, deposit_tx_hash, block_number)
        """
        w3 = self._get_web3(from_chain)

        # Normalize private key
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        account = Account.from_key(private_key)
        if account.address.lower() != wallet.lower():
            raise BridgeError(
                f"Private key mismatch: key controls {account.address}, "
                f"but wallet is {wallet}"
            )

        spoke_pool_addr = SPOKE_POOLS[from_chain]
        chain_id        = CHAINS[from_chain]["chain_id"]

        # ── Step 1: Approve if needed ────────────────────────────────────────
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(input_token),
            abi=ERC20_ABI,
        )
        allowance = token_contract.functions.allowance(
            wallet,
            Web3.to_checksum_address(spoke_pool_addr),
        ).call()

        approve_hash = None
        if allowance < input_amount_raw:
            logger.info(f"[Bridge] Approving {input_amount_raw} for SpokePool...")
            nonce     = w3.eth.get_transaction_count(wallet)
            gas_price = w3.eth.gas_price

            approve_txn = token_contract.functions.approve(
                Web3.to_checksum_address(spoke_pool_addr),
                input_amount_raw,
            ).build_transaction({
                "from":     wallet,
                "nonce":    nonce,
                "gasPrice": gas_price,
                "chainId":  chain_id,
            })

            signed = account.sign_transaction(approve_txn)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status != 1:
                raise BridgeError(f"Approve tx reverted: {tx_hash.hex()}")

            approve_hash = receipt.transactionHash.hex()
            logger.info(f"[Bridge] ✅ Approved (tx: {approve_hash})")
        else:
            logger.info("[Bridge] Allowance already sufficient — skipping approve")

        # ── Step 2: depositV3 ────────────────────────────────────────────────
        logger.info("[Bridge] Submitting depositV3...")
        nonce     = w3.eth.get_transaction_count(wallet)
        gas_price = w3.eth.gas_price

        deposit_txn = {
            "to":       tx_data["to"],
            "data":     tx_data["data"],
            "value":    int(tx_data["value"], 16),
            "gas":      tx_data["gas"],
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  chain_id,
        }

        signed_deposit = account.sign_transaction(deposit_txn)
        tx_hash        = w3.eth.send_raw_transaction(signed_deposit.raw_transaction)
        receipt        = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            raise BridgeError(
                f"depositV3 reverted. Tx: {receipt.transactionHash.hex()}"
            )

        deposit_hash = receipt.transactionHash.hex()
        block        = receipt.blockNumber
        return approve_hash, deposit_hash, block

    def _wait_for_fill(
        self,
        output_token_address: str,
        recipient: str,
        expected_amount_raw: int,
        to_chain: str,
        timeout_sec: int = 600,
        poll_interval: int = 15,
    ) -> bool:
        """
        Poll the destination chain balance to detect when the Across fill arrives.

        Strategy: record initial balance, poll every poll_interval seconds.
        If balance increases by >= expected_amount_raw → fill confirmed.

        Returns True if fill detected, False on timeout.
        """
        w3 = self._get_web3(to_chain)
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(output_token_address),
            abi=ERC20_ABI,
        )

        initial_balance = token_contract.functions.balanceOf(recipient).call()
        logger.info(
            f"[Bridge] Polling {to_chain} for fill | "
            f"initial balance: {initial_balance} | "
            f"expecting: {expected_amount_raw}"
        )

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                current_balance = token_contract.functions.balanceOf(recipient).call()
                gained = current_balance - initial_balance
                if gained >= expected_amount_raw:
                    logger.info(
                        f"[Bridge] 🎉 Fill detected on {to_chain}! "
                        f"Gained: {gained} ({gained / 1e6:.6f} units)"
                    )
                    return True
                elapsed = int(time.time() - (deadline - timeout_sec))
                logger.debug(
                    f"[Bridge] Still waiting... gained={gained}/{expected_amount_raw} "
                    f"({elapsed}s elapsed)"
                )
            except Exception as e:
                logger.warning(f"[Bridge] Poll error: {e}")

        logger.warning(f"[Bridge] Fill not detected within {timeout_sec}s timeout")
        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_token(self, symbol: str, chain: str) -> Dict:
        """Resolve a token symbol to {address, decimals}."""
        chain_tokens = TOKENS.get(chain, {})
        if symbol in chain_tokens:
            return chain_tokens[symbol]
        # Raw address fallback
        if symbol.startswith("0x"):
            logger.warning(
                f"Token address {symbol} not in registry for '{chain}', "
                f"assuming 18 decimals"
            )
            return {"address": symbol, "decimals": 18}
        raise BridgeError(
            f"Token '{symbol}' not found on chain '{chain}'. "
            f"Known tokens: {list(chain_tokens.keys())}"
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

    def _read_vault(self, key: str) -> str:
        """Read a secret from the agent vault (vault.sh)."""
        try:
            result = subprocess.run(
                [VAULT_PATH, "read", key],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise BridgeError(
                f"Vault read failed for key '{key}': {e.stderr.strip()}"
            ) from e
        except FileNotFoundError:
            raise BridgeError(
                f"Vault not found at {VAULT_PATH}. "
                f"Pass private_key directly or set up the agent vault."
            )


# ─── Module-level convenience function ────────────────────────────────────────

_default_bridge: Optional[Bridge] = None


def bridge(
    token: str,
    amount: float,
    from_chain: str,
    to_chain: str,
    wallet: str,
    dry_run: bool = True,
    private_key: Optional[str] = None,
    output_token: Optional[str] = None,
    wait_for_fill: bool = False,
) -> BridgeResult:
    """
    Convenience function — bridge tokens cross-chain via Across Protocol.

    Defaults to dry_run=True for safety.

    Example:
        from bridge import bridge
        result = bridge("USDC", 0.01, "base", "polygon", "0xYourWallet", dry_run=True)
        print(f"Output: {result.output_amount:.6f} USDC")
        print(f"Fee:    {result.fee.total_relay_fee:.6f} USDC")
        print(f"ETA:    ~{result.fee.estimated_fill_time_sec}s")
        print(f"tx:     {result.tx_data}")
    """
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = Bridge()
    return _default_bridge.bridge(
        token=token,
        amount=amount,
        from_chain=from_chain,
        to_chain=to_chain,
        wallet=wallet,
        dry_run=dry_run,
        private_key=private_key,
        output_token=output_token,
        wait_for_fill=wait_for_fill,
    )
