"""
mcp_handler.py — AutoSwap MCP Tool Handler

Entry point when AutoSwap is called as an MCP subprocess tool.
Reads JSON from stdin (or command args), executes swap/quote, returns JSON to stdout.

Protocol:
  Input  (stdin): JSON object matching the tool's input_schema
  Output (stdout): JSON object matching the tool's output_schema
  Exit code: 0 = success (swap or quote succeeded), 1 = error

Usage (MCP runtime calls this automatically):
  python3 -m autoswap.mcp_handler           # swap mode (reads from stdin)
  python3 -m autoswap.mcp_handler --quote-only  # quote-only mode
"""

import json
import sys
import logging
import os

# Suppress non-critical logs (agents want clean JSON output)
logging.basicConfig(level=logging.WARNING)


def read_input() -> dict:
    """Read JSON input from stdin."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({
                "success": False,
                "error": "No input provided. Expected JSON on stdin."
            }))
            sys.exit(1)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({
            "success": False,
            "error": f"Invalid JSON input: {e}"
        }))
        sys.exit(1)


def run_swap(params: dict) -> dict:
    """Execute or simulate a swap and return the result as a dict."""
    try:
        from autoswap import swap

        result = swap(
            from_token=params["from_token"],
            from_chain=params["from_chain"],
            to_token=params["to_token"],
            to_chain=params["to_chain"],
            amount=float(params["amount"]),
            wallet_key=params.get("wallet_key"),
            slippage_max=float(params.get("slippage_max", 2.0)),
            dry_run=bool(params.get("dry_run", False)),
        )

        return {
            "success": result.success,
            "dry_run": result.dry_run,
            "route_taken": result.route_taken,
            "route_type": result.route_type,
            "from_token": result.from_token,
            "from_chain": result.from_chain,
            "to_token": result.to_token,
            "to_chain": result.to_chain,
            "amount_in": result.amount_in,
            "amount_out": result.amount_out,
            "tx_hashes": result.tx_hashes,
            "fees": result.fees,
            "steps": [
                {
                    "step": s.step,
                    "type": s.step_type,
                    "from_token": s.from_token,
                    "to_token": s.to_token,
                    "chain": s.chain,
                    "to_chain": s.to_chain,
                    "amount_in": s.amount_in,
                    "amount_out": s.amount_out,
                    "tx_hash": s.tx_hash,
                    "status": s.status,
                }
                for s in result.steps
            ],
            "error": result.error,
        }

    except KeyError as e:
        return {"success": False, "error": f"Missing required parameter: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_quote(params: dict) -> dict:
    """Get a fast price quote without executing the swap."""
    try:
        # Import directly from src for speed
        _src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
        if _src_dir not in sys.path:
            sys.path.insert(0, os.path.abspath(_src_dir))

        from router import Router, RouterError

        router = Router()
        from_chain = params["from_chain"].lower()
        to_chain = params.get("to_chain", from_chain).lower()
        slippage_max = float(params.get("slippage_max", 2.0))
        slippage_bps = int(slippage_max * 100)

        route = router.get_best_route(
            from_token=params["from_token"],
            to_token=params["to_token"],
            amount=float(params["amount"]),
            chain=from_chain,
            slippage_bps=slippage_bps,
        )

        return {
            "success": True,
            "expected_output": route.expected_output,
            "min_output": route.min_output,
            "route": route.route,
            "dex": route.dex,
            "price_impact_pct": None,  # TODO: calculate from liquidity
            "bridge_fee": None if from_chain == to_chain else "~0.05-0.20 USDC (Across)",
            "estimated_time_seconds": None if from_chain == to_chain else 5,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    quote_only = "--quote-only" in sys.argv

    params = read_input()

    if quote_only:
        result = run_quote(params)
    else:
        result = run_swap(params)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("success", False) else 1)


if __name__ == "__main__":
    main()
