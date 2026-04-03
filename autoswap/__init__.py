"""
AutoSwap — Cross-chain swaps in one line, built for AI agents.

The only cross-chain swap SDK that:
  - Resolves native gas (cold start problem) automatically
  - Routes via best DEX (Paraswap + Uniswap V3)
  - Bridges via Across Protocol (2-second fills)
  - Never sets amountOutMin=0 (sandwich protection built-in)
  - Works in dry-run mode for safe simulation

Usage:
    from autoswap import swap

    # Cross-chain swap: ETH (Base) → MYST (Polygon)
    result = swap(
        from_token="ETH",
        from_chain="base",
        to_token="MYST",
        to_chain="polygon",
        amount=0.003,
        slippage_max=2.0,
        dry_run=True,  # simulate without submitting
    )
    print(result.route_taken)   # "ETH→USDC (base) | bridge | USDC→MYST (polygon)"
    print(result.amount_out)    # estimated output in MYST

    # Live swap (reads ETH_PRIVATE_KEY from env or agent vault)
    result = swap("USDC", "base", "USDC", "polygon", 10.0)
"""

from .swap import swap, SwapResult, StepResult, SwapError  # noqa: F401
from .safety import SafetyError  # noqa: F401

__version__ = "0.1.0"
__author__ = "fino-oss"
__license__ = "MIT"

__all__ = ["swap", "SwapResult", "StepResult", "SwapError", "SafetyError", "__version__"]
