# p14 — AutoSwap SDK

**Date** : 2026-03-25
**Modèle** : C — SDK open-source gratuit + API hosted payante
**Objectif** : Le premier outil de swap cross-chain autonome, sûr et fiable pour agents IA

---

## Problème

Faire un swap cross-chain aujourd'hui (ex: ETH Base → MYST Polygon) nécessite :
1. Trouver la route (quel DEX, quel bridge, quel path multi-hop)
2. Gérer le gas natif sur la chaîne cible (cold start problem)
3. Protéger contre le slippage et les sandwich attacks
4. Gérer les erreurs (provider down, rate limit, tx revert)
5. Écrire 3+ scripts manuels à chaque fois

**Aucun outil ne résout ça en une ligne.** Ni pour les agents IA, ni pour les devs.

**Preuve** : on l'a vécu aujourd'hui — 3 scripts, 5 bugs corrigés en live, Relay.link qui échoue, Sam qui doit intervenir manuellement pour envoyer du POL.

---

## Solution

```python
from autoswap import swap

result = swap(
    from_token="ETH",
    from_chain="base",
    to_token="MYST",
    to_chain="polygon",
    amount=0.003,
    wallet=private_key,
    slippage_max=2.0,
)
# → route optimale, bridge, gas natif, sécurité — tout automatique
```

---

## Ce que AutoSwap gère

| Fonctionnalité | Détail |
|---|---|
| **Route optimale** | Query Paraswap/1inch/Uniswap, choisit le meilleur prix |
| **Bridge automatique** | Across Protocol (2s), fallback Relay.link |
| **Gas natif** | Détecte le cold start, résout via swap interne ou gasless relay |
| **Slippage protection** | Jamais de amountOutMin=0, calcul automatique basé sur liquidité |
| **Retry + fallback** | Si un provider échoue, essaie le suivant |
| **Multi-chain** | Base, Polygon, Arbitrum, Optimism, Ethereum mainnet |
| **Dry-run** | Mode simulation sans exécuter de vraies transactions |

---

## Modèle C — Open-source + API payante

### Gratuit (SDK self-hosted)
- npm package `autoswap` / pip package `autoswap`
- L'utilisateur fournit ses propres clés RPC
- 0 frais, 100% open-source (MIT)
- Distribution : npm, GitHub, MCP tool directory

### Payant (API hosted)
- `POST https://api.autoswap.dev/swap`
- Pas besoin de gérer les RPC, les clés, les providers
- Frais : 0.1% du montant swappé OU $0.01/swap (le plus élevé)
- Paiement automatique : déduit du montant swappé
- Revenue target : 13k swaps/mois = $130-200/mois

---

## Architecture

```
autoswap/
├── src/
│   ├── router.py       ← trouve la meilleure route (Paraswap, 1inch, Uniswap)
│   ├── bridge.py       ← bridge cross-chain (Across, Relay.link)
│   ├── gas.py          ← résout le cold start gas problem
│   ├── safety.py       ← slippage calc, sandwich detection, validation
│   ├── executor.py     ← signe et envoie les transactions
│   └── swap.py         ← API publique (la fonction swap())
├── api/
│   └── server.py       ← API HTTP hosted (FastAPI)
├── tests/
├── package.json        ← npm wrapper
└── README.md
```

---

## Avantages compétitifs

1. **Vécu et testé** — construit à partir d'un vrai problème, pas théorique
2. **Agent-first** — conçu pour les agents IA (pas de UI, pas de login)
3. **Cold start gas** — le seul qui résout le "0 gas natif" automatiquement
4. **MCP compatible** — exposable comme outil Claude/Cursor/Cline
5. **Le code existe déjà à 70%** — swap-eth-to-usdc.js, bridge-to-polygon.js, relay-swap

---

## Code existant récupérable

| Script p13 | → Module autoswap |
|---|---|
| `swap-eth-to-usdc.js` | `router.py` (Uniswap V3) |
| `bridge-to-polygon.js` | `bridge.py` (Across Protocol) |
| `relay-swap-usdc-to-matic.js` | `gas.py` (Relay.link gasless) |
| Paraswap integration | `router.py` (Paraswap aggregator) |

---

## Phases

### Phase 1 — MVP SDK Python (~2 jours)
- `swap()` fonctionne pour Base↔Polygon
- Route via Paraswap (meilleur prix)
- Bridge via Across
- Gas natif via gasless relay OU petit swap interne
- Slippage protection (min 1%, max configurable)
- Tests sur vrais swaps dry-run

### Phase 2 — npm package + MCP tool (~1 jour)
- Wrapper JS pour npm
- MCP tool descriptor (intégration Claude/Cursor)
- README + exemples

### Phase 3 — API hosted (~1 jour)
- FastAPI server
- Endpoint POST /swap
- Commission automatique (0.1% déduit)
- Déploiement Railway (gratuit)

### Phase 4 — Distribution
- Publier sur npm, PyPI
- Lister sur MCP directories
- Post sur Dev.to / Farcaster
- RPGF application (Optimism/Base grants)

---

## Succès

✅ Phase 1 : `swap()` exécute un vrai swap cross-chain Base→Polygon en 1 appel
✅ Phase 2 : package publié sur npm/PyPI
✅ Phase 3 : API live avec premier swap payant d'un utilisateur externe
✅ Phase 4 : 100+ swaps/mois via l'API → premiers revenus
