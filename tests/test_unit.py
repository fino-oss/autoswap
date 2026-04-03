"""
test_unit.py — Pytest unit tests for AutoSwap SDK

All tests run without any network calls, wallet, or API keys.
Pure logic tests using mocks where needed.

Coverage:
  - safety.py: calc_min_output, detect_sandwich_risk, SafetyError
  - autoswap/__init__.py: import surface (swap, SwapResult, SwapError, SafetyError)
  - autoswap/swap.py: input validation (bad chain, zero amount)
  - autoswap/swap.py: SwapResult structure
"""

import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# ── Path setup: works both from source and from installed package ────────────
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src  = os.path.join(_root, "src")
for _p in [_root, _src]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Imports under test ───────────────────────────────────────────────────────
from safety import (
    calc_min_output,
    detect_sandwich_risk,
    SafetyError,
    SandwichRiskLevel,
)
from swap import swap, SwapResult, SwapError


# ══════════════════════════════════════════════════════════════════════════════
# safety.py — calc_min_output
# ══════════════════════════════════════════════════════════════════════════════

class TestCalcMinOutput:
    def test_basic_2pct_slippage(self):
        """2% slippage on 5 USDC → 4.9 USDC"""
        result = calc_min_output(5_000_000, slippage_bps=200)
        assert result == 4_900_000

    def test_result_never_zero(self):
        """min_output > 0 for any realistic quoted amount"""
        # 1 raw unit is too tiny (floor still rounds to 0 → SafetyError is correct)
        # Use a realistic minimum: 1000 raw units
        result = calc_min_output(1_000, slippage_bps=200)
        assert result >= 1

    def test_raises_on_zero_quote(self):
        """quoted_amount=0 must raise SafetyError (p13 lesson: never set amountOutMin=0)"""
        with pytest.raises(SafetyError, match="quoted_amount is 0"):
            calc_min_output(0, slippage_bps=200)

    def test_raises_on_negative_quote(self):
        """quoted_amount<0 must raise SafetyError"""
        with pytest.raises(SafetyError):
            calc_min_output(-100, slippage_bps=200)

    def test_min_output_less_than_quoted(self):
        """min_output must be strictly less than quoted for any positive slippage"""
        quoted = 1_000_000
        result = calc_min_output(quoted, slippage_bps=100)
        assert result < quoted

    def test_max_slippage_raises(self):
        """Slippage > SLIPPAGE_MAX_BPS (500 = 5%) must raise SafetyError"""
        with pytest.raises(SafetyError, match="exceeds maximum"):
            calc_min_output(1_000_000, slippage_bps=5000)

    def test_at_max_allowed_slippage(self):
        """At exactly 500 bps (5%), result must be > 0"""
        result = calc_min_output(1_000_000, slippage_bps=500)
        assert result > 0

    def test_default_slippage(self):
        """Default slippage is 200 bps (2%)"""
        result_default = calc_min_output(1_000_000)
        result_explicit = calc_min_output(1_000_000, slippage_bps=200)
        assert result_default == result_explicit

    def test_1pct_slippage(self):
        """1% slippage on 10 USDC → 9.9 USDC"""
        result = calc_min_output(10_000_000, slippage_bps=100)
        assert result == 9_900_000


# ══════════════════════════════════════════════════════════════════════════════
# safety.py — detect_sandwich_risk
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectSandwichRisk:
    def test_critical_risk_on_zero_min_output(self):
        """amountOutMinimum=0 must be CRITICAL risk (the core p13 lesson)"""
        tx = {
            "amountOutMinimum": 0,
            "amountOutQuoted":  2_000_000,
            "token_in":  "ETH",
            "token_out": "USDC",
        }
        risk = detect_sandwich_risk(tx)
        assert risk.level == SandwichRiskLevel.CRITICAL

    def test_missing_key_raises(self):
        """Missing amountOutMinimum key must raise SafetyError"""
        with pytest.raises(SafetyError, match="amountOutMinimum"):
            detect_sandwich_risk({"token_in": "ETH"})

    def test_low_risk_on_valid_tx(self):
        """A well-formed tx with 5% slippage should not be CRITICAL"""
        tx = {
            "amountOutMinimum": 1_900_000,   # ~1.9 USDC min (5% slippage)
            "amountOutQuoted":  2_000_000,   # 2 USDC quoted
            "token_in":  "ETH",
            "token_out": "USDC",
        }
        risk = detect_sandwich_risk(tx)
        assert risk.level != SandwichRiskLevel.CRITICAL


# ══════════════════════════════════════════════════════════════════════════════
# autoswap public API — import surface
# ══════════════════════════════════════════════════════════════════════════════

class TestPublicAPIImports:
    def test_swap_function_importable(self):
        """from autoswap import swap must work"""
        assert callable(swap)

    def test_swapresult_importable(self):
        """SwapResult must be importable"""
        assert SwapResult is not None

    def test_swaperror_importable(self):
        """SwapError must be importable"""
        assert issubclass(SwapError, Exception)

    def test_safetyerror_importable_from_package(self):
        """SafetyError must be importable from autoswap package"""
        # This tests the fix for the README bug: from autoswap import SafetyError
        sys.path.insert(0, _root)
        from autoswap import SafetyError as PackageSafetyError
        assert issubclass(PackageSafetyError, Exception)


# ══════════════════════════════════════════════════════════════════════════════
# swap() — input validation (no network calls)
# ══════════════════════════════════════════════════════════════════════════════

class TestSwapInputValidation:
    def test_raises_on_zero_amount(self):
        """amount=0 must raise SwapError immediately"""
        with pytest.raises(SwapError, match="amount must be positive"):
            swap("ETH", "base", "USDC", "base", 0.0, dry_run=True)

    def test_raises_on_negative_amount(self):
        """amount<0 must raise SwapError immediately"""
        with pytest.raises(SwapError):
            swap("ETH", "base", "USDC", "base", -1.0, dry_run=True)

    def test_raises_on_unknown_chain(self):
        """Unknown chain must raise SwapError"""
        with pytest.raises(SwapError):
            swap("ETH", "solana", "USDC", "base", 0.001, dry_run=True)

    def test_raises_on_unknown_from_chain(self):
        """Unknown from_chain must raise SwapError"""
        with pytest.raises(SwapError):
            swap("ETH", "fakenet", "USDC", "base", 0.001, dry_run=True)


# ══════════════════════════════════════════════════════════════════════════════
# SwapResult — structure and fields
# ══════════════════════════════════════════════════════════════════════════════

class TestSwapResultStructure:
    def _make_mock_result(self, **kwargs):
        """Build a minimal SwapResult for structure tests."""
        defaults = dict(
            success=True,
            dry_run=True,
            route_taken="ETH→USDC (base) | bridge | USDC→MYST (polygon)",
            route_type="swap_bridge_swap",
            from_token="ETH",
            from_chain="base",
            to_token="MYST",
            to_chain="polygon",
            amount_in=0.003,
            amount_out=47.3,
            tx_hashes=[],
            fees={"bridge_fee": 0.08},
            steps=[],
            error=None,
        )
        defaults.update(kwargs)
        return SwapResult(**defaults)

    def test_swapresult_has_required_fields(self):
        r = self._make_mock_result()
        assert hasattr(r, "success")
        assert hasattr(r, "dry_run")
        assert hasattr(r, "route_taken")
        assert hasattr(r, "route_type")
        assert hasattr(r, "amount_in")
        assert hasattr(r, "amount_out")
        assert hasattr(r, "tx_hashes")
        assert hasattr(r, "fees")
        assert hasattr(r, "steps")
        assert hasattr(r, "error")

    def test_swapresult_dryrun_has_no_txhashes(self):
        """In dry-run mode, tx_hashes must be empty"""
        r = self._make_mock_result(dry_run=True, tx_hashes=[])
        assert r.tx_hashes == []

    def test_swapresult_print_summary_callable(self):
        """print_summary() must exist and be callable (README documents it)"""
        r = self._make_mock_result()
        assert callable(r.print_summary)

    def test_swapresult_failed_has_error(self):
        """Failed result must have error set"""
        r = self._make_mock_result(success=False, error="Route not found")
        assert r.success is False
        assert r.error is not None


# ══════════════════════════════════════════════════════════════════════════════
# Package installability — wheel content
# ══════════════════════════════════════════════════════════════════════════════

class TestWheelContent:
    def test_wheel_contains_autoswap_package(self):
        """The wheel must contain autoswap/*.py (not just autoswap/__init__.py)"""
        import zipfile, glob
        wheels = glob.glob(os.path.join(_root, "dist", "*.whl"))
        if not wheels:
            pytest.skip("No wheel found in dist/ — run 'python3 -m build' first")
        with zipfile.ZipFile(wheels[0]) as z:
            files = z.namelist()
            autoswap_py = [f for f in files if f.startswith("autoswap/") and f.endswith(".py")]
            assert len(autoswap_py) > 1, (
                f"Wheel only has {autoswap_py} — expected swap.py, router.py, etc."
            )

    def test_wheel_has_core_modules(self):
        """Wheel must include swap.py, router.py, bridge.py, gas.py, safety.py"""
        import zipfile, glob
        wheels = glob.glob(os.path.join(_root, "dist", "*.whl"))
        if not wheels:
            pytest.skip("No wheel found in dist/ — run 'python3 -m build' first")
        with zipfile.ZipFile(wheels[0]) as z:
            files = z.namelist()
            for module in ["swap.py", "router.py", "bridge.py", "gas.py", "safety.py"]:
                assert f"autoswap/{module}" in files, (
                    f"autoswap/{module} missing from wheel — pip install will be broken"
                )
