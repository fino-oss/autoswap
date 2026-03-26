# autoswap

Cross-chain swaps in one line — built for AI agents.

See the [full documentation on GitHub](https://github.com/fino-oss/autoswap).

## Quick Start

```js
const { swap } = require('autoswap');

const result = await swap({
  fromToken: 'ETH',
  fromChain: 'base',
  toToken: 'MYST',
  toChain: 'polygon',
  amount: 0.003,
  dryRun: true,
});

console.log(result.routeTaken);
// → "ETH→USDC (base) | bridge | USDC→MYST (polygon)"
```

## Requirements

- Node.js >= 18
- Python 3.9+ with `pip install autoswap`

## License

MIT
