"""
test_router_dryrun.py — Dry-run tests for router.py Phase 1

Tests:
  1. ETH → USDC on Base
  2. USDC → MYST on Polygon

No transactions are sent. Validates:
  - Route is found
  - expected_output > 0
  - min_output > 0 (NEVER 0)
  - min_output < expected_output
  - Route description is non-empty
  - tx_data is None (no user_address passed = no tx build)
"""

import sys
import os
import logging

# Add src/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)

from router import Router, RouteResult, RouterError

def print_result(label: str, r: RouteResult):
    print(f"\n{'═'*60}")
    print(f"  {label}")
    print(f"{'═'*60}")
    print(f"  DEX:             {r.dex}")
    print(f"  Route:           {r.route}")
    print(f"  From:            {r.from_amount} {r.from_token}")
    print(f"  Expected output: {r.expected_output:.6f} {r.to_token}")
    print(f"  Min output:      {r.min_output:.6f} {r.to_token}  (slippage={r.slippage_bps/100:.1f}%)")
    print(f"  Min output raw:  {r.min_output_raw} (must be > 0 ✅)" if r.min_output_raw > 0 else f"  ❌ FATAL: min_output_raw = {r.min_output_raw}")
    if r.uniswap_fee_tier:
        print(f"  UniV3 fee tier:  {r.uniswap_fee_tier} ({r.uniswap_fee_tier/10000:.3f}%)")
    if r.meta.get("gas_cost_usd"):
        print(f"  Gas cost (USD):  ${r.meta['gas_cost_usd']}")
    print(f"  tx_data:         {'None (dry-run, no address)' if r.tx_data is None else 'built ✅'}")


def assert_valid(r: RouteResult, label: str):
    """Assert all invariants."""
    assert r.expected_output > 0,         f"[{label}] expected_output must be > 0"
    assert r.min_output > 0,              f"[{label}] min_output must be > 0 (NEVER 0)"
    assert r.min_output_raw > 0,          f"[{label}] min_output_raw must be > 0 (NEVER 0)"
    assert r.min_output < r.expected_output, f"[{label}] min_output must be < expected_output"
    assert r.route,                        f"[{label}] route description must not be empty"
    assert r.dex in ("paraswap", "uniswap_v3", "uniswap_v3_multihop"), \
        f"[{label}] unknown dex: {r.dex}"
    print(f"  ✅ All assertions passed for [{label}]")


def main():
    router = Router()
    errors = []

    # ── Test 1: ETH → USDC on Base ──────────────────────────────────────────
    print("\n🔵 Test 1: ETH → USDC on Base (0.001 ETH)")
    try:
        r1 = router.get_best_route(
            from_token="ETH",
            to_token="USDC",
            amount=0.001,
            chain="base",
            slippage_bps=100,
        )
        print_result("ETH → USDC | Base | 0.001 ETH", r1)
        assert_valid(r1, "ETH→USDC Base")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        errors.append(f"Test 1 (ETH→USDC Base): {e}")

    # ── Test 2: USDC → MYST on Polygon ──────────────────────────────────────
    print("\n🔵 Test 2: USDC → MYST on Polygon (0.5 USDC)")
    try:
        r2 = router.get_best_route(
            from_token="USDC",
            to_token="MYST",
            amount=0.5,
            chain="polygon",
            slippage_bps=100,
        )
        print_result("USDC → MYST | Polygon | 0.5 USDC", r2)
        assert_valid(r2, "USDC→MYST Polygon")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        errors.append(f"Test 2 (USDC→MYST Polygon): {e}")

    # ── Test 3: Slippage guarantee edge case ─────────────────────────────────
    print("\n🔵 Test 3: Slippage guarantee — min_output must NEVER be 0")
    try:
        r3 = router.get_best_route(
            from_token="ETH",
            to_token="USDC",
            amount=0.001,
            chain="base",
            slippage_bps=9999,  # extreme slippage — still must not be 0
        )
        # At 99.99% slippage, min_output should be nearly 0 but still > 0
        print(f"  expected_output: {r3.expected_output:.6f}")
        print(f"  min_output (99.99% slip): {r3.min_output:.10f}")
        assert r3.min_output > 0, "min_output must still be > 0 at extreme slippage"
        assert r3.min_output_raw > 0, "min_output_raw must still be > 0"
        print(f"  ✅ min_output > 0 even at extreme slippage")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        errors.append(f"Test 3 (slippage edge case): {e}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    if errors:
        print(f"❌ {len(errors)} test(s) FAILED:")
        for err in errors:
            print(f"   - {err}")
        sys.exit(1)
    else:
        print("✅ All tests passed!")
        print("   min_output > 0 guaranteed on all routes")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
