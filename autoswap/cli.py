"""
autoswap CLI — run cross-chain swaps from the command line.

Usage:
    autoswap --from ETH --from-chain base --to MYST --to-chain polygon --amount 0.003
    autoswap --from USDC --from-chain base --to USDC --to-chain polygon --amount 10 --dry-run
    autoswap --from ETH --from-chain base --to USDC --to-chain base --amount 0.001 --slippage 1.5
"""

import argparse
import json
import logging
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="autoswap",
        description="Cross-chain swaps in one line — built for AI agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run cross-chain swap (no transaction submitted)
  autoswap --from ETH --from-chain base --to MYST --to-chain polygon --amount 0.003 --dry-run

  # Live swap with custom slippage
  autoswap --from USDC --from-chain base --to USDC --to-chain polygon --amount 10.0 --slippage 1.0

  # JSON output for agent pipelines
  autoswap --from ETH --from-chain base --to USDC --to-chain base --amount 0.001 --json
        """,
    )

    parser.add_argument("--from", dest="from_token", required=True,
                        help="Input token (e.g. ETH, USDC, MYST)")
    parser.add_argument("--from-chain", required=True,
                        help="Source chain (base, polygon, arbitrum, optimism)")
    parser.add_argument("--to", dest="to_token", required=True,
                        help="Output token (e.g. MYST, ETH, USDC)")
    parser.add_argument("--to-chain", required=True,
                        help="Destination chain")
    parser.add_argument("--amount", type=float, required=True,
                        help="Amount of input token (e.g. 0.003)")
    parser.add_argument("--slippage", type=float, default=2.0,
                        help="Max slippage %% (default: 2.0)")
    parser.add_argument("--wallet", default=None,
                        help="Private key (0x...) — reads from ETH_PRIVATE_KEY env if not set")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate the swap without submitting transactions")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output result as JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    try:
        from autoswap import swap

        result = swap(
            from_token=args.from_token,
            from_chain=args.from_chain,
            to_token=args.to_token,
            to_chain=args.to_chain,
            amount=args.amount,
            wallet_key=args.wallet,
            slippage_max=args.slippage,
            dry_run=args.dry_run,
        )

        if args.json_output:
            output = {
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
            print(json.dumps(output, indent=2))
        else:
            result.print_summary()

        sys.exit(0 if result.success else 1)

    except KeyboardInterrupt:
        print("\n[autoswap] Interrupted.")
        sys.exit(1)
    except Exception as e:
        if args.json_output:
            print(json.dumps({"success": False, "error": str(e)}))
        else:
            print(f"[autoswap] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
