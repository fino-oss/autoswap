/**
 * AutoSwap TypeScript definitions
 */

export interface SwapOptions {
  fromToken: string;
  fromChain: 'base' | 'polygon' | 'arbitrum' | 'optimism' | 'ethereum';
  toToken: string;
  toChain: 'base' | 'polygon' | 'arbitrum' | 'optimism' | 'ethereum';
  amount: number;
  slippageMax?: number;
  walletKey?: string;
  dryRun?: boolean;
  timeoutMs?: number;
}

export interface QuoteOptions {
  fromToken: string;
  fromChain: 'base' | 'polygon' | 'arbitrum' | 'optimism' | 'ethereum';
  toToken: string;
  toChain: 'base' | 'polygon' | 'arbitrum' | 'optimism' | 'ethereum';
  amount: number;
  slippageMax?: number;
  timeoutMs?: number;
}

export interface StepResult {
  step: number;
  type: 'swap' | 'bridge' | 'gas_resolve';
  fromToken: string;
  toToken: string;
  chain: string;
  toChain: string | null;
  amountIn: number;
  amountOut: number;
  txHash: string | null;
  status: 'planned' | 'submitted' | 'confirmed' | 'failed';
}

export interface SwapResult {
  success: boolean;
  dryRun: boolean;
  routeTaken: string;
  routeType: 'same_chain' | 'direct_bridge' | 'swap_bridge_swap';
  fromToken: string;
  fromChain: string;
  toToken: string;
  toChain: string;
  amountIn: number;
  amountOut: number;
  txHashes: string[];
  fees: Record<string, number>;
  steps: StepResult[];
  error: string | null;
}

export interface QuoteResult {
  success: boolean;
  expectedOutput: number;
  minOutput: number;
  route: string;
  dex: string;
  priceImpactPct: number | null;
  bridgeFee: string | null;
  estimatedTimeSeconds: number | null;
}

/**
 * Execute or simulate a cross-chain token swap.
 *
 * @example
 * const result = await swap({ fromToken: 'ETH', fromChain: 'base', toToken: 'MYST', toChain: 'polygon', amount: 0.003, dryRun: true });
 */
export function swap(options: SwapOptions): Promise<SwapResult>;

/**
 * Get a price quote without executing the swap.
 *
 * @example
 * const q = await quote({ fromToken: 'ETH', fromChain: 'base', toToken: 'USDC', toChain: 'base', amount: 0.001 });
 */
export function quote(options: QuoteOptions): Promise<QuoteResult>;
