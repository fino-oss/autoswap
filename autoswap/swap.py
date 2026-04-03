"""
swap.py — AutoSwap Orchestrator (Phase 1 public API)

Single entry point for all swaps (same-chain or cross-chain).
Orchestrates router.py + bridge.py + gas.py + safety.py in one call.

Usage:
    from swap import swap, SwapResult

    # Cross-chain: ETH (Base) → MYST (Polygon) — 3-step route
    result = swap(
        from_token="ETH",
        from_chain="base",
        to_token="MYST",
        to_chain="polygon",
        amount=0.003,
        wallet_key=None,      # reads ETH_PRIVATE_KEY from vault
        slippage_max=2.0,
        dry_run=True,
    )
    print(result.route_taken)  # "ETH→USDC (base) | bridge USDC base→polygon | USDC→MYST (polygon)"
    print(result.amount_out)   # estimated output in MYST

    # Same-chain: ETH → USDC (Base)
    result = swap("ETH", "base", "USDC", "base", 0.001, wallet_key=key, dry_run=True)

Route logic:
  1. from_chain == to_chain  → single swap via router.py
  2. from_chain != to_chain:
     a. Check native gas on to_chain (gas.py) → resolve via Relay.link if needed
     b. If from_token is directly bridgeable to to_token → bridge.py direct
     c. Otherwise: swap→bridge→swap (3-step route)
     Each step validated by safety.py (amountOutMin > 0, sandwich check).

Wallet key resolution:
  - If wallet_key is a hex private key (0x + 64 chars) → use directly
  - Otherwise → read ETH_PRIVATE_KEY from agent vault via subprocess
"""

import logging
import subprocess
import sys
import os
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

# ── Path setup: allow import from tests/ directory ────────────────────────────
_src_dir = os.path.dirname(os.path.abspath(__file__))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from .router import Router, RouteResult, RouterError, TOKENS as ROUTER_TOKENS
from .bridge import Bridge, BridgeResult, BridgeError, TOKENS as BRIDGE_TOKENS
from .gas import GasResolver, GasResolveResult, GasStrategy, GasResolverError
from .safety import (
    calc_min_output,
    detect_sandwich_risk,
    SafetyError,
    SandwichRiskLevel,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

VAULT_PATH = "/Users/sam/.pi/agent/skills/agent-vault/vault.sh"

# Tokens that can be bridged directly via Across Protocol per chain pair
# (Checked empirically — USDC is the most reliable, WETH is possible but less tested)
BRIDGEABLE_TOKENS: Dict[Tuple[str, str], List[str]] = {
    ("base", "polygon"):    ["USDC", "WETH"],
    ("polygon", "base"):    ["USDC", "WETH"],
    ("base", "arbitrum"):   ["USDC", "WETH"],
    ("arbitrum", "base"):   ["USDC", "WETH"],
    ("base", "optimism"):   ["USDC", "WETH"],
    ("optimism", "base"):   ["USDC", "WETH"],
    ("polygon", "arbitrum"):["USDC"],
    ("arbitrum", "polygon"):["USDC"],
}

# Preferred bridge token (best liquidity, most reliable on Across)
PREFERRED_BRIDGE_TOKEN = "USDC"


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class StepResult:
    """Result of one step in a multi-step swap route."""
    step: int                        # 1, 2, 3, ...
    step_type: str                   # "swap" | "bridge" | "gas_resolve"
    from_token: str
    to_token: str
    chain: str                       # For bridge: this is from_chain
    to_chain: Optional[str]          # Only used for bridge steps
    amount_in: float
    amount_out: float                # Estimated; actual may differ slightly
    tx_hash: Optional[str] = None   # None in dry-run or before execution
    tx_data: Optional[Dict[str, Any]] = None  # Encoded calldata (ready to sign)
    dry_run: bool = True
    status: str = "planned"          # "planned" | "submitted" | "confirmed" | "failed"
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        """Human-readable step description."""
        if self.step_type == "bridge":
            return (
                f"Step {self.step} [bridge] "
                f"{self.amount_in:.6f} {self.from_token} "
                f"{self.chain}→{self.to_chain} "
                f"→ ~{self.amount_out:.6f} {self.to_token}"
            )
        elif self.step_type == "gas_resolve":
            return (
                f"Step {self.step} [gas_resolve] "
                f"Acquire native gas on {self.chain} via {self.meta.get('strategy', '?')}"
            )
        else:
            return (
                f"Step {self.step} [swap] "
                f"{self.amount_in:.6f} {self.from_token}→{self.to_token} "
                f"on {self.chain} "
                f"→ ~{self.amount_out:.6f} {self.to_token}"
            )


@dataclass
class SwapResult:
    """
    Complete result of a swap() call.

    In dry-run mode: all steps are "planned", tx_data is built but not submitted.
    In live mode: tx_hashes are populated, status = "confirmed" or "failed".
    """
    # Overall outcome
    success: bool
    dry_run: bool

    # Route taken
    route_taken: str                 # Human-readable: "ETH→USDC (base) | bridge | USDC→MYST (polygon)"
    route_type: str                  # "same_chain" | "direct_bridge" | "swap_bridge_swap"

    # Tokens / chains
    from_token: str
    from_chain: str
    to_token: str
    to_chain: str

    # Amounts
    amount_in: float
    amount_out: float                # Estimated total output (0 if failed)

    # Transaction hashes (empty list in dry-run)
    tx_hashes: List[str]

    # Step-by-step breakdown
    steps: List[StepResult]

    # Fee breakdown: {description: amount}
    # e.g., {"bridge_fee": 0.05, "router_fee_step1": 0.001, ...}
    fees: Dict[str, float]

    # Error details (when success=False)
    error: Optional[str] = None
    error_step: Optional[int] = None

    # Extra metadata
    meta: Dict[str, Any] = field(default_factory=dict)

    def print_summary(self) -> None:
        """Print a formatted summary to stdout."""
        sep = "═" * 65
        print(f"\n{sep}")
        status_icon = "✅" if self.success else "❌"
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        print(f"  {status_icon} AutoSwap {mode} — {self.route_type}")
        print(f"{sep}")
        print(f"  Route:     {self.route_taken}")
        print(f"  Input:     {self.amount_in} {self.from_token} ({self.from_chain})")
        print(f"  Output:    ~{self.amount_out:.6f} {self.to_token} ({self.to_chain})")
        print(f"  Steps:     {len(self.steps)}")
        for step in self.steps:
            print(f"    {step.describe()}")
        if self.fees:
            print(f"  Fees:      {self.fees}")
        if self.tx_hashes:
            print(f"  TX hashes: {self.tx_hashes}")
        if self.error:
            print(f"  Error:     {self.error}")
        print(f"{sep}\n")


class SwapError(Exception):
    """Raised when a swap operation fails fatally."""
    pass


# ─── Public API ───────────────────────────────────────────────────────────────

def swap(
    from_token: str,
    from_chain: str,
    to_token: str,
    to_chain: str,
    amount: float,
    wallet_key: Optional[str] = None,
    slippage_max: float = 2.0,
    dry_run: bool = False,
) -> SwapResult:
    """
    Execute (or simulate) a swap — same-chain or cross-chain.

    Args:
        from_token:   Input token symbol (e.g. "ETH", "USDC", "MYST")
        from_chain:   Source chain ("base", "polygon", "arbitrum", "optimism")
        to_token:     Output token symbol (e.g. "MYST", "ETH", "USDC")
        to_chain:     Destination chain (same as from_chain for single-chain swap)
        amount:       Amount of from_token to swap (in human units, e.g. 0.003 for 0.003 ETH)
        wallet_key:   Hex private key (0x...) or None to read from vault
        slippage_max: Maximum acceptable slippage in percent (default 2.0 = 2%)
        dry_run:      If True, build all tx_data but don't submit (default False)

    Returns:
        SwapResult with success status, route taken, amounts, tx_hashes, and steps.

    Raises:
        SwapError: For unrecoverable errors (bad chain, zero amount, etc.)
        SafetyError: If safety checks fail (e.g. amountOutMin would be 0)

    Examples:
        # Dry-run cross-chain swap
        result = swap("ETH", "base", "MYST", "polygon", 0.003, dry_run=True)
        result.print_summary()

        # Live swap (reads key from vault)
        result = swap("USDC", "base", "USDC", "polygon", 10.0)
        if result.success:
            print(f"Received {result.amount_out:.4f} USDC on polygon")
    """
    # ── Normalize inputs ─────────────────────────────────────────────────────
    from_token  = from_token.upper()
    to_token    = to_token.upper()
    from_chain  = from_chain.lower()
    to_chain    = to_chain.lower()
    slippage_bps = int(slippage_max * 100)  # 2.0% → 200 bps

    if amount <= 0:
        raise SwapError(f"amount must be positive, got {amount}")

    logger.info(
        f"[Swap] {amount} {from_token} ({from_chain}) → {to_token} ({to_chain}) "
        f"| slippage={slippage_max}% | dry_run={dry_run}"
    )

    # ── Load private key (needed for live mode + gas check address) ──────────
    private_key = _load_wallet_key(wallet_key)
    wallet_address = _derive_address(private_key)
    logger.info(f"[Swap] Wallet: {wallet_address}")

    # ── Route: same-chain or cross-chain ─────────────────────────────────────
    if from_chain == to_chain:
        return _execute_same_chain(
            from_token=from_token,
            to_token=to_token,
            chain=from_chain,
            amount=amount,
            wallet_address=wallet_address,
            private_key=private_key,
            slippage_bps=slippage_bps,
            dry_run=dry_run,
        )
    else:
        return _execute_cross_chain(
            from_token=from_token,
            from_chain=from_chain,
            to_token=to_token,
            to_chain=to_chain,
            amount=amount,
            wallet_address=wallet_address,
            private_key=private_key,
            slippage_bps=slippage_bps,
            dry_run=dry_run,
        )


# ─── Same-chain swap ──────────────────────────────────────────────────────────

def _execute_same_chain(
    from_token: str,
    to_token: str,
    chain: str,
    amount: float,
    wallet_address: str,
    private_key: str,
    slippage_bps: int,
    dry_run: bool,
) -> SwapResult:
    """Single-chain swap via best DEX (Paraswap / Uniswap V3)."""
    logger.info(f"[Swap] Route: same-chain swap {from_token}→{to_token} on {chain}")

    router = Router()
    steps: List[StepResult] = []
    tx_hashes: List[str] = []
    fees: Dict[str, float] = {}

    try:
        route: RouteResult = router.get_best_route(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
            slippage_bps=slippage_bps,
            user_address=wallet_address,
        )
    except RouterError as e:
        return SwapResult(
            success=False,
            dry_run=dry_run,
            route_taken=f"{from_token}→{to_token} on {chain}",
            route_type="same_chain",
            from_token=from_token, from_chain=chain,
            to_token=to_token,     to_chain=chain,
            amount_in=amount,
            amount_out=0.0,
            tx_hashes=[],
            steps=[],
            fees={},
            error=str(e),
            error_step=1,
        )

    # Safety check: sandwich risk
    sandwich = detect_sandwich_risk({
        "amountOutMinimum": route.min_output_raw,
        "amountOutQuoted":  route.expected_output_raw,
        "token_in":  from_token,
        "token_out": to_token,
    })
    if sandwich.should_block:
        raise SafetyError(
            f"[Swap] Safety block on same-chain swap: {sandwich.reason}"
        )
    if sandwich.level in (SandwichRiskLevel.HIGH, SandwichRiskLevel.MEDIUM):
        logger.warning(f"[Swap] Sandwich risk [{sandwich.level}]: {sandwich.reason}")

    logger.info(
        f"[Swap] Route found via {route.dex}: "
        f"expected={route.expected_output:.6f} {to_token}, "
        f"min={route.min_output:.6f} {to_token}"
    )

    step = StepResult(
        step=1,
        step_type="swap",
        from_token=from_token,
        to_token=to_token,
        chain=chain,
        to_chain=None,
        amount_in=amount,
        amount_out=route.expected_output,
        tx_data=route.tx_data,
        dry_run=dry_run,
        status="planned" if dry_run else "pending",
        meta={
            "dex":          route.dex,
            "route_desc":   route.route,
            "min_output":   route.min_output,
            "slippage_bps": slippage_bps,
        },
    )
    steps.append(step)

    # Gas cost estimate from Paraswap
    gas_cost_usd = route.meta.get("gas_cost_usd")
    if gas_cost_usd:
        fees["gas_step1"] = float(gas_cost_usd) if gas_cost_usd else 0.0

    amount_out = route.expected_output

    # ── Live execution ────────────────────────────────────────────────────────
    if not dry_run:
        tx_hash, amount_out = _submit_router_tx(
            route=route,
            private_key=private_key,
            wallet_address=wallet_address,
            chain=chain,
        )
        if tx_hash:
            step.tx_hash = tx_hash
            step.status = "confirmed"
            tx_hashes.append(tx_hash)
        else:
            step.status = "failed"
            return SwapResult(
                success=False,
                dry_run=dry_run,
                route_taken=f"{from_token}→{to_token} on {chain}",
                route_type="same_chain",
                from_token=from_token, from_chain=chain,
                to_token=to_token,     to_chain=chain,
                amount_in=amount,
                amount_out=0.0,
                tx_hashes=[],
                steps=steps,
                fees=fees,
                error="Router swap tx failed",
                error_step=1,
            )
    else:
        step.status = "planned"

    route_desc = f"{from_token}→{to_token} via {route.dex} ({chain})"

    return SwapResult(
        success=True,
        dry_run=dry_run,
        route_taken=route_desc,
        route_type="same_chain",
        from_token=from_token, from_chain=chain,
        to_token=to_token,     to_chain=chain,
        amount_in=amount,
        amount_out=amount_out,
        tx_hashes=tx_hashes,
        steps=steps,
        fees=fees,
        meta={
            "dex":        route.dex,
            "slippage_bps": slippage_bps,
            "sandwich_risk": str(sandwich.level),
        },
    )


# ─── Cross-chain swap ─────────────────────────────────────────────────────────

def _execute_cross_chain(
    from_token: str,
    from_chain: str,
    to_token: str,
    to_chain: str,
    amount: float,
    wallet_address: str,
    private_key: str,
    slippage_bps: int,
    dry_run: bool,
) -> SwapResult:
    """
    Cross-chain swap orchestration:
      1. Check gas on to_chain → resolve if needed
      2a. If direct bridge possible → bridge only
      2b. Otherwise → swap→bridge→swap
    """
    steps: List[StepResult] = []
    tx_hashes: List[str] = []
    fees: Dict[str, float] = {}

    # ── Step 0: Gas check on to_chain ────────────────────────────────────────
    # (Not counted as a numbered step unless action is needed)
    gas_step = _check_and_resolve_gas(
        wallet=wallet_address,
        chain=to_chain,
        source_chain=from_chain,
        private_key=private_key,
        dry_run=dry_run,
    )

    if gas_step is not None:
        # Gas resolution was needed — add as step 1
        gas_step.step = 1
        steps.append(gas_step)
        fees["gas_resolve"] = gas_step.meta.get("usdc_cost", 0.0)

        if not dry_run and gas_step.status == "failed":
            return SwapResult(
                success=False,
                dry_run=dry_run,
                route_taken=f"gas_resolve({to_chain}) → FAILED",
                route_type="swap_bridge_swap",
                from_token=from_token, from_chain=from_chain,
                to_token=to_token,     to_chain=to_chain,
                amount_in=amount,
                amount_out=0.0,
                tx_hashes=[],
                steps=steps,
                fees=fees,
                error=f"Gas resolution failed on {to_chain}: {gas_step.error}",
                error_step=1,
            )

    base_step = len(steps)  # How many steps before the actual swap steps

    # ── Determine route type ─────────────────────────────────────────────────
    bridge_token = _find_bridge_token(from_token, from_chain, to_token, to_chain)
    logger.info(f"[Swap] Bridge token: {bridge_token}")

    # Direct bridge: from_token == to_token AND both are bridgeable
    is_direct_bridge = (
        from_token == to_token
        and from_token == bridge_token
    )

    if is_direct_bridge:
        return _route_direct_bridge(
            token=from_token,
            from_chain=from_chain,
            to_chain=to_chain,
            amount=amount,
            wallet_address=wallet_address,
            private_key=private_key,
            dry_run=dry_run,
            pre_steps=steps,
            tx_hashes=tx_hashes,
            fees=fees,
            base_step=base_step,
        )
    else:
        return _route_swap_bridge_swap(
            from_token=from_token,
            from_chain=from_chain,
            to_token=to_token,
            to_chain=to_chain,
            bridge_token=bridge_token,
            amount=amount,
            wallet_address=wallet_address,
            private_key=private_key,
            slippage_bps=slippage_bps,
            dry_run=dry_run,
            pre_steps=steps,
            tx_hashes=tx_hashes,
            fees=fees,
            base_step=base_step,
        )


# ─── Route: Direct bridge (e.g. USDC base → USDC polygon) ────────────────────

def _route_direct_bridge(
    token: str,
    from_chain: str,
    to_chain: str,
    amount: float,
    wallet_address: str,
    private_key: str,
    dry_run: bool,
    pre_steps: List[StepResult],
    tx_hashes: List[str],
    fees: Dict[str, float],
    base_step: int,
) -> SwapResult:
    """Bridge a token directly (same token on both sides)."""
    logger.info(
        f"[Swap] Route: direct bridge {token} {from_chain}→{to_chain} "
        f"amount={amount}"
    )

    bridge_obj = Bridge()

    try:
        bridge_result: BridgeResult = bridge_obj.bridge(
            token=token,
            amount=amount,
            from_chain=from_chain,
            to_chain=to_chain,
            wallet=wallet_address,
            dry_run=dry_run,
            private_key=private_key if not dry_run else None,
        )
    except BridgeError as e:
        step = StepResult(
            step=base_step + 1,
            step_type="bridge",
            from_token=token,
            to_token=token,
            chain=from_chain,
            to_chain=to_chain,
            amount_in=amount,
            amount_out=0.0,
            dry_run=dry_run,
            status="failed",
            error=str(e),
        )
        all_steps = pre_steps + [step]
        return SwapResult(
            success=False,
            dry_run=dry_run,
            route_taken=f"bridge {token} {from_chain}→{to_chain}",
            route_type="direct_bridge",
            from_token=token, from_chain=from_chain,
            to_token=token,   to_chain=to_chain,
            amount_in=amount,
            amount_out=0.0,
            tx_hashes=[],
            steps=all_steps,
            fees=fees,
            error=str(e),
            error_step=base_step + 1,
        )

    fees["bridge_fee"] = bridge_result.fee.total_relay_fee

    bridge_step = StepResult(
        step=base_step + 1,
        step_type="bridge",
        from_token=token,
        to_token=token,
        chain=from_chain,
        to_chain=to_chain,
        amount_in=amount,
        amount_out=bridge_result.output_amount,
        tx_data=bridge_result.tx_data,
        tx_hash=bridge_result.deposit_tx_hash,
        dry_run=dry_run,
        status="planned" if dry_run else "confirmed",
        meta={
            "fee": bridge_result.fee.total_relay_fee,
            "fee_raw": bridge_result.fee.total_relay_fee_raw,
            "estimated_fill_time_sec": bridge_result.fee.estimated_fill_time_sec,
            "exclusive_relayer": bridge_result.fee.exclusive_relayer,
            "spoke_pool": bridge_result.meta.get("spoke_pool"),
        },
    )

    if not dry_run and bridge_result.deposit_tx_hash:
        tx_hashes.append(bridge_result.deposit_tx_hash)
        if bridge_result.approve_tx_hash:
            tx_hashes.insert(0, bridge_result.approve_tx_hash)

    all_steps = pre_steps + [bridge_step]
    route_desc = f"bridge {token} {from_chain}→{to_chain}"

    return SwapResult(
        success=True,
        dry_run=dry_run,
        route_taken=route_desc,
        route_type="direct_bridge",
        from_token=token, from_chain=from_chain,
        to_token=token,   to_chain=to_chain,
        amount_in=amount,
        amount_out=bridge_result.output_amount,
        tx_hashes=tx_hashes,
        steps=all_steps,
        fees=fees,
        meta={
            "bridge": "across_protocol",
            "estimated_fill_time_sec": bridge_result.fee.estimated_fill_time_sec,
        },
    )


# ─── Route: swap → bridge → swap ─────────────────────────────────────────────

def _route_swap_bridge_swap(
    from_token: str,
    from_chain: str,
    to_token: str,
    to_chain: str,
    bridge_token: str,
    amount: float,
    wallet_address: str,
    private_key: str,
    slippage_bps: int,
    dry_run: bool,
    pre_steps: List[StepResult],
    tx_hashes: List[str],
    fees: Dict[str, float],
    base_step: int,
) -> SwapResult:
    """
    3-step route: swap→bridge→swap

    Step 1 (conditional): swap from_token → bridge_token (on from_chain)
    Step 2: bridge bridge_token (from_chain → to_chain)
    Step 3 (conditional): swap bridge_token → to_token (on to_chain)

    Examples:
      ETH/base → MYST/polygon:
        Step 1: ETH → USDC  (base, via Paraswap)
        Step 2: USDC base → USDC polygon  (Across Protocol)
        Step 3: USDC → MYST  (polygon, via Paraswap)

      ETH/base → USDC/polygon:  (bridge_token = USDC = to_token)
        Step 1: ETH → USDC  (base)
        Step 2: USDC base → USDC polygon
        [Step 3 skipped — already have USDC]
    """
    router_obj = Router()
    bridge_obj = Bridge()

    all_steps = list(pre_steps)
    step_num = base_step + 1

    need_step1 = (from_token != bridge_token)  # swap from_token → bridge_token
    need_step3 = (to_token != bridge_token)     # swap bridge_token → to_token

    current_amount = amount
    all_succeeded = True
    final_amount_out = 0.0

    # ── Step 1 (optional): Swap from_token → bridge_token on from_chain ──────
    if need_step1:
        logger.info(
            f"[Swap] Step {step_num}: {from_token}→{bridge_token} on {from_chain} "
            f"| amount={current_amount}"
        )
        try:
            route1: RouteResult = router_obj.get_best_route(
                from_token=from_token,
                to_token=bridge_token,
                amount=current_amount,
                chain=from_chain,
                slippage_bps=slippage_bps,
                user_address=wallet_address,
            )
        except RouterError as e:
            step1 = StepResult(
                step=step_num, step_type="swap",
                from_token=from_token, to_token=bridge_token,
                chain=from_chain, to_chain=None,
                amount_in=current_amount, amount_out=0.0,
                dry_run=dry_run, status="failed", error=str(e),
            )
            all_steps.append(step1)
            return _fail(
                from_token, from_chain, to_token, to_chain,
                amount, dry_run, all_steps, tx_hashes, fees,
                error=f"Step {step_num} ({from_token}→{bridge_token}): {e}",
                error_step=step_num,
            )

        # Safety check
        sandwich1 = detect_sandwich_risk({
            "amountOutMinimum": route1.min_output_raw,
            "amountOutQuoted":  route1.expected_output_raw,
            "token_in": from_token, "token_out": bridge_token,
        })
        if sandwich1.should_block:
            raise SafetyError(
                f"[Swap] Step {step_num} safety block: {sandwich1.reason}"
            )

        step1 = StepResult(
            step=step_num, step_type="swap",
            from_token=from_token, to_token=bridge_token,
            chain=from_chain, to_chain=None,
            amount_in=current_amount,
            amount_out=route1.expected_output,
            tx_data=route1.tx_data,
            dry_run=dry_run,
            status="planned",
            meta={
                "dex": route1.dex,
                "route_desc": route1.route,
                "min_output": route1.min_output,
                "slippage_bps": slippage_bps,
                "sandwich_risk": str(sandwich1.level),
            },
        )

        if not dry_run:
            tx_hash1, actual_out1 = _submit_router_tx(
                route=route1, private_key=private_key,
                wallet_address=wallet_address, chain=from_chain,
            )
            if tx_hash1:
                step1.tx_hash = tx_hash1
                step1.status = "confirmed"
                step1.amount_out = actual_out1
                tx_hashes.append(tx_hash1)
                current_amount = actual_out1
            else:
                step1.status = "failed"
                all_steps.append(step1)
                return _fail(
                    from_token, from_chain, to_token, to_chain,
                    amount, dry_run, all_steps, tx_hashes, fees,
                    error=f"Step {step_num} ({from_token}→{bridge_token}) tx failed",
                    error_step=step_num,
                )
        else:
            current_amount = route1.expected_output

        all_steps.append(step1)
        fees[f"gas_step{step_num}"] = float(route1.meta.get("gas_cost_usd") or 0)
        step_num += 1

    # ── Step 2: Bridge bridge_token from_chain → to_chain ────────────────────
    logger.info(
        f"[Swap] Step {step_num}: bridge {bridge_token} "
        f"{from_chain}→{to_chain} | amount={current_amount}"
    )

    try:
        bridge_result: BridgeResult = bridge_obj.bridge(
            token=bridge_token,
            amount=current_amount,
            from_chain=from_chain,
            to_chain=to_chain,
            wallet=wallet_address,
            dry_run=dry_run,
            private_key=private_key if not dry_run else None,
        )
    except BridgeError as e:
        step_bridge = StepResult(
            step=step_num, step_type="bridge",
            from_token=bridge_token, to_token=bridge_token,
            chain=from_chain, to_chain=to_chain,
            amount_in=current_amount, amount_out=0.0,
            dry_run=dry_run, status="failed", error=str(e),
        )
        all_steps.append(step_bridge)
        return _fail(
            from_token, from_chain, to_token, to_chain,
            amount, dry_run, all_steps, tx_hashes, fees,
            error=f"Step {step_num} (bridge {bridge_token}): {e}",
            error_step=step_num,
        )

    fees["bridge_fee"] = bridge_result.fee.total_relay_fee

    step_bridge = StepResult(
        step=step_num, step_type="bridge",
        from_token=bridge_token, to_token=bridge_token,
        chain=from_chain, to_chain=to_chain,
        amount_in=current_amount,
        amount_out=bridge_result.output_amount,
        tx_data=bridge_result.tx_data,
        tx_hash=bridge_result.deposit_tx_hash,
        dry_run=dry_run,
        status="planned" if dry_run else "confirmed",
        meta={
            "fee": bridge_result.fee.total_relay_fee,
            "fee_raw": bridge_result.fee.total_relay_fee_raw,
            "estimated_fill_time_sec": bridge_result.fee.estimated_fill_time_sec,
            "exclusive_relayer": bridge_result.fee.exclusive_relayer,
            "spoke_pool": bridge_result.meta.get("spoke_pool"),
        },
    )

    if not dry_run and bridge_result.deposit_tx_hash:
        if bridge_result.approve_tx_hash:
            tx_hashes.append(bridge_result.approve_tx_hash)
        tx_hashes.append(bridge_result.deposit_tx_hash)

    current_amount = bridge_result.output_amount
    all_steps.append(step_bridge)
    step_num += 1

    # If to_token == bridge_token, we're done after the bridge
    if not need_step3:
        route_parts = []
        if need_step1:
            route_parts.append(f"{from_token}→{bridge_token} ({from_chain})")
        route_parts.append(f"bridge {bridge_token} {from_chain}→{to_chain}")
        route_desc = " | ".join(route_parts)

        return SwapResult(
            success=True,
            dry_run=dry_run,
            route_taken=route_desc,
            route_type="swap_bridge_swap" if need_step1 else "direct_bridge",
            from_token=from_token, from_chain=from_chain,
            to_token=to_token,     to_chain=to_chain,
            amount_in=amount,
            amount_out=current_amount,
            tx_hashes=tx_hashes,
            steps=all_steps,
            fees=fees,
            meta={
                "bridge": "across_protocol",
                "bridge_token": bridge_token,
                "estimated_fill_time_sec": bridge_result.fee.estimated_fill_time_sec,
            },
        )

    # ── Step 3: Swap bridge_token → to_token on to_chain ─────────────────────
    logger.info(
        f"[Swap] Step {step_num}: {bridge_token}→{to_token} on {to_chain} "
        f"| amount={current_amount}"
    )

    try:
        route3: RouteResult = router_obj.get_best_route(
            from_token=bridge_token,
            to_token=to_token,
            amount=current_amount,
            chain=to_chain,
            slippage_bps=slippage_bps,
            user_address=wallet_address,
        )
    except RouterError as e:
        step3 = StepResult(
            step=step_num, step_type="swap",
            from_token=bridge_token, to_token=to_token,
            chain=to_chain, to_chain=None,
            amount_in=current_amount, amount_out=0.0,
            dry_run=dry_run, status="failed", error=str(e),
        )
        all_steps.append(step3)
        return _fail(
            from_token, from_chain, to_token, to_chain,
            amount, dry_run, all_steps, tx_hashes, fees,
            error=f"Step {step_num} ({bridge_token}→{to_token}): {e}",
            error_step=step_num,
        )

    # Safety check
    sandwich3 = detect_sandwich_risk({
        "amountOutMinimum": route3.min_output_raw,
        "amountOutQuoted":  route3.expected_output_raw,
        "token_in": bridge_token, "token_out": to_token,
    })
    if sandwich3.should_block:
        raise SafetyError(
            f"[Swap] Step {step_num} safety block: {sandwich3.reason}"
        )

    step3 = StepResult(
        step=step_num, step_type="swap",
        from_token=bridge_token, to_token=to_token,
        chain=to_chain, to_chain=None,
        amount_in=current_amount,
        amount_out=route3.expected_output,
        tx_data=route3.tx_data,
        dry_run=dry_run,
        status="planned",
        meta={
            "dex": route3.dex,
            "route_desc": route3.route,
            "min_output": route3.min_output,
            "slippage_bps": slippage_bps,
            "sandwich_risk": str(sandwich3.level),
        },
    )

    if not dry_run:
        tx_hash3, actual_out3 = _submit_router_tx(
            route=route3, private_key=private_key,
            wallet_address=wallet_address, chain=to_chain,
        )
        if tx_hash3:
            step3.tx_hash = tx_hash3
            step3.status = "confirmed"
            step3.amount_out = actual_out3
            tx_hashes.append(tx_hash3)
            final_amount_out = actual_out3
        else:
            step3.status = "failed"
            all_steps.append(step3)
            return _fail(
                from_token, from_chain, to_token, to_chain,
                amount, dry_run, all_steps, tx_hashes, fees,
                error=f"Step {step_num} ({bridge_token}→{to_token}) tx failed",
                error_step=step_num,
            )
    else:
        final_amount_out = route3.expected_output

    all_steps.append(step3)
    fees[f"gas_step{step_num}"] = float(route3.meta.get("gas_cost_usd") or 0)

    # ── Build final route description ─────────────────────────────────────────
    parts = []
    if need_step1:
        parts.append(f"{from_token}→{bridge_token} ({from_chain})")
    parts.append(f"bridge {bridge_token} {from_chain}→{to_chain}")
    if need_step3:
        parts.append(f"{bridge_token}→{to_token} ({to_chain})")
    route_desc = " | ".join(parts)

    return SwapResult(
        success=True,
        dry_run=dry_run,
        route_taken=route_desc,
        route_type="swap_bridge_swap",
        from_token=from_token, from_chain=from_chain,
        to_token=to_token,     to_chain=to_chain,
        amount_in=amount,
        amount_out=final_amount_out,
        tx_hashes=tx_hashes,
        steps=all_steps,
        fees=fees,
        meta={
            "bridge": "across_protocol",
            "bridge_token": bridge_token,
            "estimated_fill_time_sec": bridge_result.fee.estimated_fill_time_sec,
        },
    )


# ─── Gas check helper ─────────────────────────────────────────────────────────

def _check_and_resolve_gas(
    wallet: str,
    chain: str,
    source_chain: str,
    private_key: str,
    dry_run: bool,
) -> Optional[StepResult]:
    """
    Check if wallet has enough native gas on `chain`.
    If not, attempt resolution via Relay.link.

    Returns a StepResult if action was needed, None if gas is sufficient.
    """
    resolver = GasResolver()

    try:
        gas_result: GasResolveResult = resolver.resolve(
            wallet=wallet,
            chain=chain,
            source_chain=source_chain,
            dry_run=dry_run,
            private_key=private_key if not dry_run else None,
        )
    except GasResolverError as e:
        logger.error(f"[Swap] Gas resolve error: {e}")
        # Return a failed gas step
        return StepResult(
            step=0,
            step_type="gas_resolve",
            from_token="USDC",
            to_token=f"native_{chain}",
            chain=chain,
            to_chain=None,
            amount_in=0,
            amount_out=0,
            dry_run=dry_run,
            status="failed",
            error=str(e),
        )

    if gas_result.strategy == GasStrategy.SKIP:
        logger.info(
            f"[Swap] Gas OK on {chain}: {gas_result.current_balance:.4f} "
            f"{_native_symbol(chain)} (min: {gas_result.gas_needed})"
        )
        return None  # No action needed

    # Gas resolution needed
    logger.info(
        f"[Swap] Gas needed on {chain} — strategy: {gas_result.strategy.value}"
    )

    status = "planned" if dry_run else (
        "confirmed" if gas_result.status == "confirmed" else
        "submitted" if gas_result.status == "submitted" else
        "failed"
    )

    relay_quote = gas_result.relay_quote
    usdc_cost = gas_result.usdc_cost or 0.0

    gas_acquire = gas_result.gas_to_acquire or gas_result.gas_needed

    return StepResult(
        step=0,  # Will be renumbered by caller
        step_type="gas_resolve",
        from_token="USDC",
        to_token=f"native_{chain}",
        chain=source_chain,
        to_chain=chain,
        amount_in=usdc_cost,
        amount_out=gas_acquire,
        dry_run=dry_run,
        status=status,
        error=gas_result.error if gas_result.strategy == GasStrategy.MANUAL else None,
        meta={
            "strategy": gas_result.strategy.value,
            "usdc_cost": usdc_cost,
            "gas_to_acquire": gas_acquire,
            "gas_needed": gas_result.gas_needed,
            "current_balance": gas_result.current_balance,
            "relay_request_id": relay_quote.request_id if relay_quote else None,
            "description": (
                f"Acquire {gas_acquire:.4f} native gas on {chain} "
                f"via {gas_result.strategy.value} "
                f"(cost: ~{usdc_cost:.4f} USDC)"
            ),
        },
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _find_bridge_token(
    from_token: str,
    from_chain: str,
    to_token: str,
    to_chain: str,
) -> str:
    """
    Select the best intermediate token for bridging.

    Strategy:
      1. If from_token is bridgeable → use it as bridge_token (avoids first swap)
      2. If to_token is bridgeable → use it as bridge_token (avoids last swap)
      3. Use PREFERRED_BRIDGE_TOKEN (USDC) as intermediate
    """
    bridgeable = BRIDGEABLE_TOKENS.get((from_chain, to_chain), [])

    if not bridgeable:
        raise SwapError(
            f"No bridge route configured for {from_chain}→{to_chain}. "
            f"Supported routes: {list(BRIDGEABLE_TOKENS.keys())}"
        )

    # from_token is directly bridgeable → no step 1 needed
    if from_token in bridgeable:
        return from_token

    # to_token is bridgeable → no step 3 needed
    if to_token in bridgeable:
        return to_token

    # Default: use USDC as the bridge token
    if PREFERRED_BRIDGE_TOKEN in bridgeable:
        return PREFERRED_BRIDGE_TOKEN

    return bridgeable[0]


def _load_wallet_key(wallet_key: Optional[str]) -> str:
    """
    Load private key: use provided key directly, or read from vault.

    A valid private key is 0x + 64 hex chars (66 chars total) or 64 hex chars.
    Anything else is treated as "read from vault".
    """
    if wallet_key:
        # Check if it looks like a private key (0x + 64 hex = 66 chars)
        clean = wallet_key.lstrip("0x")
        if len(clean) == 64 and all(c in "0123456789abcdefABCDEF" for c in clean):
            return wallet_key if wallet_key.startswith("0x") else "0x" + wallet_key

    # Read from vault
    logger.debug("[Swap] Reading ETH_PRIVATE_KEY from vault...")
    try:
        result = subprocess.run(
            [VAULT_PATH, "read", "ETH_PRIVATE_KEY"],
            capture_output=True,
            text=True,
            check=True,
        )
        key = result.stdout.strip()
        if not key:
            raise SwapError("Vault returned empty ETH_PRIVATE_KEY")
        return key
    except subprocess.CalledProcessError as e:
        raise SwapError(
            f"Cannot read ETH_PRIVATE_KEY from vault: {e.stderr.strip()}. "
            f"Either pass wallet_key directly or set up the agent vault."
        ) from e
    except FileNotFoundError:
        raise SwapError(
            f"Vault not found at {VAULT_PATH}. "
            f"Pass wallet_key directly as a hex private key."
        )


def _derive_address(private_key: str) -> str:
    """Derive the public wallet address from a private key."""
    try:
        from eth_account import Account
        account = Account.from_key(private_key)
        return account.address
    except Exception as e:
        raise SwapError(f"Cannot derive address from private key: {e}") from e


def _native_symbol(chain: str) -> str:
    """Return the native token symbol for a chain."""
    symbols = {
        "base":     "ETH",
        "polygon":  "POL",
        "arbitrum": "ETH",
        "optimism": "ETH",
        "ethereum": "ETH",
    }
    return symbols.get(chain, "native")


def _submit_router_tx(
    route: RouteResult,
    private_key: str,
    wallet_address: str,
    chain: str,
) -> Tuple[Optional[str], float]:
    """
    Sign and submit a router swap transaction.

    Returns (tx_hash, amount_out) on success.
    Returns (None, 0.0) on failure.

    In Phase 1 this is a simplified executor — a full executor.py
    module will be added in Phase 2 for production robustness.
    """
    if not route.tx_data:
        logger.error("[Swap] Cannot submit: tx_data is None (no user_address was set?)")
        return None, 0.0

    try:
        from web3 import Web3
        from eth_account import Account

        # Get chain RPC
        chain_rpcs = {
            "base":     "https://mainnet.base.org",
            "polygon":  "https://polygon.drpc.org",
            "arbitrum": "https://arb1.arbitrum.io/rpc",
            "optimism": "https://mainnet.optimism.io",
        }
        rpc_url = chain_rpcs.get(chain)
        if not rpc_url:
            logger.error(f"[Swap] No RPC configured for chain '{chain}'")
            return None, 0.0

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        account = Account.from_key(private_key)

        tx = route.tx_data.copy()
        tx["from"]   = wallet_address
        tx["nonce"]  = w3.eth.get_transaction_count(wallet_address)
        tx["chainId"] = tx.get("chainId", w3.eth.chain_id)

        # Ensure value is int
        value_raw = tx.get("value", "0x0")
        if isinstance(value_raw, str):
            tx["value"] = int(value_raw, 16)

        # Gas estimation with buffer
        try:
            gas_estimate = w3.eth.estimate_gas(tx)
            tx["gas"] = int(gas_estimate * 1.2)
        except Exception:
            tx["gas"] = tx.get("gas", 300_000)

        tx["gasPrice"] = w3.eth.gas_price

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            logger.error(f"[Swap] Swap tx reverted: {tx_hash.hex()}")
            return None, 0.0

        logger.info(f"[Swap] ✅ Swap confirmed: {tx_hash.hex()}")
        # Return expected output (actual on-chain amount would require event parsing)
        return tx_hash.hex(), route.expected_output

    except Exception as e:
        logger.error(f"[Swap] Router tx submission failed: {e}")
        return None, 0.0


def _fail(
    from_token: str, from_chain: str,
    to_token: str, to_chain: str,
    amount: float,
    dry_run: bool,
    steps: List[StepResult],
    tx_hashes: List[str],
    fees: Dict[str, float],
    error: str,
    error_step: int,
) -> SwapResult:
    """Build a failed SwapResult."""
    return SwapResult(
        success=False,
        dry_run=dry_run,
        route_taken=f"FAILED at step {error_step}",
        route_type="swap_bridge_swap",
        from_token=from_token, from_chain=from_chain,
        to_token=to_token,     to_chain=to_chain,
        amount_in=amount,
        amount_out=0.0,
        tx_hashes=tx_hashes,
        steps=steps,
        fees=fees,
        error=error,
        error_step=error_step,
    )


# ─── Module-level convenience ─────────────────────────────────────────────────

# Re-export for convenience
__all__ = ["swap", "SwapResult", "StepResult", "SwapError"]
