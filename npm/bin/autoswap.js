#!/usr/bin/env node
/**
 * autoswap CLI — cross-chain swaps from the command line
 *
 * Usage:
 *   autoswap --from ETH --from-chain base --to MYST --to-chain polygon --amount 0.003 --dry-run
 *   autoswap --from USDC --from-chain base --to USDC --to-chain polygon --amount 10 --slippage 1.0
 *   autoswap quote --from ETH --from-chain base --to USDC --to-chain base --amount 0.001
 */

'use strict';

const { swap, quote } = require('../src/index.js');

const args = process.argv.slice(2);

function usage() {
  console.log(`
Usage:
  autoswap --from <TOKEN> --from-chain <CHAIN> --to <TOKEN> --to-chain <CHAIN> --amount <NUM> [options]
  autoswap quote --from <TOKEN> --from-chain <CHAIN> --to <TOKEN> --to-chain <CHAIN> --amount <NUM>

Options:
  --from          Input token (ETH, USDC, MYST, ...)
  --from-chain    Source chain (base, polygon, arbitrum, optimism)
  --to            Output token
  --to-chain      Destination chain
  --amount        Amount in human units (e.g. 0.003)
  --slippage      Max slippage % (default: 2.0)
  --dry-run       Simulate without submitting transactions
  --json          Output as JSON
  --wallet        Private key (0x...) — reads ETH_PRIVATE_KEY from env if omitted
  -h, --help      Show this help

Supported chains: base, polygon, arbitrum, optimism, ethereum
Supported tokens: ETH, USDC, WETH, MYST, POL, DAI, USDT, ...

Examples:
  # Dry-run: ETH (Base) → MYST (Polygon)
  autoswap --from ETH --from-chain base --to MYST --to-chain polygon --amount 0.003 --dry-run

  # Live swap: USDC bridge Base → Polygon
  autoswap --from USDC --from-chain base --to USDC --to-chain polygon --amount 10

  # Quote only (fast, no wallet needed)
  autoswap quote --from ETH --from-chain base --to USDC --to-chain base --amount 0.001
`);
}

// Parse args
function parseArgs(argv) {
  const opts = { quoteMode: false, json: false, dryRun: false };
  let i = 0;

  if (argv[0] === 'quote') {
    opts.quoteMode = true;
    i = 1;
  }

  for (; i < argv.length; i++) {
    switch (argv[i]) {
      case '--from':       opts.fromToken = argv[++i]; break;
      case '--from-chain': opts.fromChain = argv[++i]; break;
      case '--to':         opts.toToken = argv[++i]; break;
      case '--to-chain':   opts.toChain = argv[++i]; break;
      case '--amount':     opts.amount = parseFloat(argv[++i]); break;
      case '--slippage':   opts.slippageMax = parseFloat(argv[++i]); break;
      case '--wallet':     opts.walletKey = argv[++i]; break;
      case '--dry-run':    opts.dryRun = true; break;
      case '--json':       opts.json = true; break;
      case '-h':
      case '--help':       usage(); process.exit(0);
      default:
        console.error(`Unknown option: ${argv[i]}`);
        usage();
        process.exit(1);
    }
  }
  return opts;
}

async function main() {
  if (args.length === 0 || args.includes('--help') || args.includes('-h')) {
    usage();
    process.exit(0);
  }

  const opts = parseArgs(args);

  if (!opts.fromToken || !opts.fromChain || !opts.toToken || !opts.toChain || !opts.amount) {
    console.error('Error: --from, --from-chain, --to, --to-chain, --amount are all required');
    usage();
    process.exit(1);
  }

  try {
    let result;

    if (opts.quoteMode) {
      result = await quote({
        fromToken: opts.fromToken,
        fromChain: opts.fromChain,
        toToken: opts.toToken,
        toChain: opts.toChain,
        amount: opts.amount,
        slippageMax: opts.slippageMax || 2.0,
      });

      if (opts.json) {
        console.log(JSON.stringify(result, null, 2));
      } else {
        console.log('\n═══════════════════════════════════════════════════════════════');
        console.log(`  📊 AutoSwap Quote`);
        console.log('═══════════════════════════════════════════════════════════════');
        console.log(`  Route:    ${result.route || 'N/A'}`);
        console.log(`  Input:    ${opts.amount} ${opts.fromToken} (${opts.fromChain})`);
        console.log(`  Expected: ~${result.expectedOutput} ${opts.toToken}`);
        console.log(`  Min out:  ${result.minOutput} ${opts.toToken}`);
        if (result.bridgeFee) console.log(`  Bridge:   ${result.bridgeFee}`);
        if (result.estimatedTimeSeconds) console.log(`  ETA:      ~${result.estimatedTimeSeconds}s`);
        console.log('═══════════════════════════════════════════════════════════════\n');
      }
    } else {
      result = await swap({
        fromToken: opts.fromToken,
        fromChain: opts.fromChain,
        toToken: opts.toToken,
        toChain: opts.toChain,
        amount: opts.amount,
        slippageMax: opts.slippageMax || 2.0,
        walletKey: opts.walletKey,
        dryRun: opts.dryRun,
      });

      if (opts.json) {
        console.log(JSON.stringify(result, null, 2));
      } else {
        const icon = result.success ? '✅' : '❌';
        const mode = result.dryRun ? 'DRY-RUN' : 'LIVE';
        console.log('\n═══════════════════════════════════════════════════════════════');
        console.log(`  ${icon} AutoSwap ${mode} — ${result.routeType || ''}`);
        console.log('═══════════════════════════════════════════════════════════════');
        console.log(`  Route:    ${result.routeTaken}`);
        console.log(`  Input:    ${result.amountIn} ${result.fromToken} (${result.fromChain})`);
        console.log(`  Output:   ~${result.amountOut} ${result.toToken} (${result.toChain})`);
        console.log(`  Steps:    ${(result.steps || []).length}`);
        (result.steps || []).forEach(s => {
          const arrow = s.toChain ? `${s.chain}→${s.toChain}` : s.chain;
          console.log(`    Step ${s.step} [${s.type}]: ${s.amountIn} ${s.fromToken} → ~${s.amountOut} ${s.toToken} (${arrow}) [${s.status}]`);
        });
        if (result.txHashes && result.txHashes.length > 0) {
          console.log(`  TXs:      ${result.txHashes.join(', ')}`);
        }
        if (result.error) {
          console.log(`  Error:    ${result.error}`);
        }
        console.log('═══════════════════════════════════════════════════════════════\n');
      }
    }

    process.exit(result.success ? 0 : 1);
  } catch (err) {
    if (opts && opts.json) {
      console.log(JSON.stringify({ success: false, error: err.message }));
    } else {
      console.error(`[autoswap] Error: ${err.message}`);
    }
    process.exit(1);
  }
}

main();
