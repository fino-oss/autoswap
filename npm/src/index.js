/**
 * autoswap — Cross-chain swaps in one line, built for AI agents.
 *
 * This JS package is a thin wrapper around the AutoSwap Python SDK.
 * It calls `python3 -m autoswap.mcp_handler` via subprocess.
 *
 * Requirements:
 *   - Python 3.9+ with the autoswap pip package installed
 *   - OR: AUTOSWAP_PYTHON_PATH env var pointing to the autoswap package dir
 *
 * Usage:
 *   const { swap, quote } = require('autoswap');
 *
 *   // Dry-run cross-chain swap
 *   const result = await swap({
 *     fromToken: 'ETH',
 *     fromChain: 'base',
 *     toToken: 'MYST',
 *     toChain: 'polygon',
 *     amount: 0.003,
 *     slippageMax: 2.0,
 *     dryRun: true,
 *   });
 *   console.log(result.routeTaken);  // "ETH→USDC (base) | bridge | USDC→MYST (polygon)"
 *   console.log(result.amountOut);   // estimated MYST output
 */

'use strict';

const { spawn } = require('child_process');
const path = require('path');
const os = require('os');

// ── Config ────────────────────────────────────────────────────────────────────

const DEFAULT_TIMEOUT_MS = 60_000; // 60 seconds
const DEFAULT_PYTHON = process.env.AUTOSWAP_PYTHON || 'python3';

// Find the Python package directory (if using local install)
const AUTOSWAP_PYTHON_PATH = process.env.AUTOSWAP_PYTHON_PATH || null;

// ── Core subprocess call ───────────────────────────────────────────────────────

/**
 * Call the AutoSwap Python MCP handler via subprocess.
 *
 * @param {Object} params - Input parameters
 * @param {string[]} [extraArgs] - Extra CLI args (e.g. ['--quote-only'])
 * @param {number} [timeoutMs] - Timeout in milliseconds
 * @returns {Promise<Object>} - Parsed JSON response
 */
function callPython(params, extraArgs = [], timeoutMs = DEFAULT_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const args = ['-m', 'autoswap.mcp_handler', ...extraArgs];
    const env = { ...process.env };

    if (AUTOSWAP_PYTHON_PATH) {
      env.PYTHONPATH = AUTOSWAP_PYTHON_PATH + (env.PYTHONPATH ? ':' + env.PYTHONPATH : '');
    }

    const proc = spawn(DEFAULT_PYTHON, args, {
      env,
      timeout: timeoutMs,
    });

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (chunk) => { stdout += chunk; });
    proc.stderr.on('data', (chunk) => { stderr += chunk; });

    // Send input JSON to stdin
    proc.stdin.write(JSON.stringify(params));
    proc.stdin.end();

    proc.on('close', (code) => {
      if (!stdout.trim()) {
        reject(new Error(
          `AutoSwap Python process produced no output (exit ${code}). ` +
          `stderr: ${stderr.slice(0, 500)}`
        ));
        return;
      }

      try {
        const result = JSON.parse(stdout);
        resolve(result);
      } catch {
        reject(new Error(
          `AutoSwap returned invalid JSON (exit ${code}): ${stdout.slice(0, 500)}`
        ));
      }
    });

    proc.on('error', (err) => {
      if (err.code === 'ENOENT') {
        reject(new Error(
          `Python not found at '${DEFAULT_PYTHON}'. ` +
          `Set AUTOSWAP_PYTHON env var or install python3.`
        ));
      } else {
        reject(err);
      }
    });
  });
}

// ── Public API ─────────────────────────────────────────────────────────────────

/**
 * Execute or simulate a cross-chain token swap.
 *
 * @param {Object} options
 * @param {string} options.fromToken   - Input token (e.g. 'ETH', 'USDC', 'MYST')
 * @param {string} options.fromChain   - Source chain ('base', 'polygon', 'arbitrum', 'optimism')
 * @param {string} options.toToken     - Output token
 * @param {string} options.toChain     - Destination chain
 * @param {number} options.amount      - Amount in human units (e.g. 0.003 for 0.003 ETH)
 * @param {number} [options.slippageMax=2.0]   - Max slippage % (default 2.0)
 * @param {string} [options.walletKey]         - Private key (reads from env if omitted)
 * @param {boolean} [options.dryRun=false]     - Simulate without submitting
 * @param {number} [options.timeoutMs=60000]   - Timeout in milliseconds
 *
 * @returns {Promise<SwapResult>}
 *
 * @example
 * const result = await swap({
 *   fromToken: 'ETH', fromChain: 'base',
 *   toToken: 'MYST', toChain: 'polygon',
 *   amount: 0.003, dryRun: true
 * });
 * console.log(`Route: ${result.routeTaken}`);
 * console.log(`Output: ~${result.amountOut} MYST`);
 */
async function swap(options) {
  const {
    fromToken,
    fromChain,
    toToken,
    toChain,
    amount,
    slippageMax = 2.0,
    walletKey,
    dryRun = false,
    timeoutMs = DEFAULT_TIMEOUT_MS,
  } = options;

  // Validate required fields
  if (!fromToken) throw new Error('fromToken is required');
  if (!fromChain) throw new Error('fromChain is required');
  if (!toToken) throw new Error('toToken is required');
  if (!toChain) throw new Error('toChain is required');
  if (!amount || amount <= 0) throw new Error('amount must be a positive number');

  const params = {
    from_token: fromToken,
    from_chain: fromChain,
    to_token: toToken,
    to_chain: toChain,
    amount: amount,
    slippage_max: slippageMax,
    dry_run: dryRun,
  };

  if (walletKey) params.wallet_key = walletKey;

  const raw = await callPython(params, [], timeoutMs);

  // Transform snake_case → camelCase for JS consumers
  return _toCamelCase(raw);
}

/**
 * Get a price quote without executing the swap.
 * Faster than swap() with dryRun=true (skips tx building).
 *
 * @param {Object} options - Same as swap() but no walletKey/dryRun
 * @returns {Promise<QuoteResult>}
 *
 * @example
 * const q = await quote({ fromToken: 'ETH', fromChain: 'base', toToken: 'USDC', toChain: 'base', amount: 0.001 });
 * console.log(`Expected: ${q.expectedOutput} USDC`);
 * console.log(`Min out:  ${q.minOutput} USDC`);
 */
async function quote(options) {
  const { fromToken, fromChain, toToken, toChain, amount, slippageMax = 2.0, timeoutMs = 15_000 } = options;

  if (!fromToken || !fromChain || !toToken || !toChain || !amount) {
    throw new Error('fromToken, fromChain, toToken, toChain, and amount are all required');
  }

  const params = {
    from_token: fromToken,
    from_chain: fromChain,
    to_token: toToken,
    to_chain: toChain,
    amount,
    slippage_max: slippageMax,
  };

  const raw = await callPython(params, ['--quote-only'], timeoutMs);
  return _toCamelCase(raw);
}

// ── Helpers ────────────────────────────────────────────────────────────────────

/**
 * Recursively convert snake_case keys to camelCase.
 * @param {any} obj
 * @returns {any}
 */
function _toCamelCase(obj) {
  if (Array.isArray(obj)) {
    return obj.map(_toCamelCase);
  }
  if (obj && typeof obj === 'object') {
    const result = {};
    for (const [key, val] of Object.entries(obj)) {
      const camelKey = key.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
      result[camelKey] = _toCamelCase(val);
    }
    return result;
  }
  return obj;
}

// ── Exports ────────────────────────────────────────────────────────────────────

module.exports = { swap, quote };

/**
 * @typedef {Object} SwapResult
 * @property {boolean} success
 * @property {boolean} dryRun
 * @property {string} routeTaken       - E.g. "ETH→USDC (base) | bridge | USDC→MYST (polygon)"
 * @property {string} routeType        - "same_chain" | "direct_bridge" | "swap_bridge_swap"
 * @property {string} fromToken
 * @property {string} fromChain
 * @property {string} toToken
 * @property {string} toChain
 * @property {number} amountIn
 * @property {number} amountOut        - Estimated or actual output
 * @property {string[]} txHashes       - Empty in dry-run
 * @property {Object} fees             - {bridgeFee, gasStep1, ...}
 * @property {StepResult[]} steps      - Step-by-step breakdown
 * @property {string|null} error       - Error message if success=false
 */

/**
 * @typedef {Object} StepResult
 * @property {number} step
 * @property {string} type             - "swap" | "bridge" | "gas_resolve"
 * @property {string} fromToken
 * @property {string} toToken
 * @property {string} chain
 * @property {string|null} toChain
 * @property {number} amountIn
 * @property {number} amountOut
 * @property {string|null} txHash
 * @property {string} status           - "planned" | "submitted" | "confirmed" | "failed"
 */

/**
 * @typedef {Object} QuoteResult
 * @property {boolean} success
 * @property {number} expectedOutput
 * @property {number} minOutput
 * @property {string} route
 * @property {string} dex
 * @property {number|null} priceImpactPct
 * @property {string|null} bridgeFee
 * @property {number|null} estimatedTimeSeconds
 */
