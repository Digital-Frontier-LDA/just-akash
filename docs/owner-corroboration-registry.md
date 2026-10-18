# Owner corroboration registry 3

SDK 1.49.0 prepares a versioned replacement for the two owner corroboration
sources. This document records the source proposal and migration requirements;
it does not declare the candidate released, fully qualified or active in a
consumer. Registry entries alone do not grant resource authority.

## Sources and provenance

Both REST endpoints and the Crouton peer are recorded in
[the pinned Akash chain registry](https://github.com/cosmos/chain-registry/blob/cc1ed04b31326bc79f3208e571fb71fa0fddb987/akash/chain.json),
whose file SHA-256 is
`3720728bb01b964132cf1452678c6aaf20f0f5b2df098e2d56ceb577e741b1c2`.

| Source | REST endpoint | Operator | Named gateway / cache route |
| --- | --- | --- | --- |
| `crouton` | `https://akash-mainnet-api.crouton.digital` | Crouton Digital | `crouton-api-148.251.53.24` / `crouton-api-route` |
| `polkachu` | `https://akash-api.polkachu.com` | Polkachu | `cloudflare-polkachu-api` / `cloudflare-polkachu-api-route` |

On October 18, 2026, public REST `node_info` and the corresponding operator RPC
`status` responses identified the same node for each operator, with distinct node
identities between operators:

- Crouton: `c318e3febe1bac5dc1b2b3e43d906517321f1386`, matching the pinned
  registry peer at `148.251.53.24:12856`.
- Polkachu: `26402e60a2430168851743e2b14e06945f28a8b4`, matching
  [its published internal live peer](https://polkachu.com/api/v2/chains/akash/live_peers)
  at `65.108.228.209:12856`.

DNS/TLS observations map Crouton's named API route to its published operator
address. Polkachu's named public route passes through Cloudflare. Its
[published endpoint implementation](https://github.com/polkachu/cosmos-validators/tree/2b0d6bc4c4ffe59cc69e2c1fc93a0d96b3f36bfa/roles/support_public_endpoints)
proxies API and RPC to operator localhost ports. That publication describes an
implementation, not an attestation of the exact current deployment. The route
labels name the observed control and cache domains; they do not assert absence
of caching or exhaustive knowledge of backend infrastructure. Distinct operator
attribution, published peer mappings and observed node identities support the
separation inference. Equal chain responses alone would not establish it.

## Native transport identification

On October 18, the original native observer stopped at Polkachu's initial
containment read with HTTP 403. Two bounded urllib requests to the same path,
using the same TLS client and Accept header, returned HTTP 403 with urllib's
default User-Agent and HTTP 200 with valid deployment/group JSON when using
`just-akash-balance/1.0`. The observer now sends that existing SDK identifier,
matching `chain._lcd_get`. This measured request difference supports the header
change; it does not establish an operator-wide filtering policy or qualify the
full observation. Redirect refusal, TLS verification, strict JSON parsing, exact
height echoes, proof predicates and all transport budgets remain unchanged.

## Preserved proof contract

The existing `OWNER_CORROBORATION_SOURCES_V2` Python symbol remains available for
consumers of the current proof API. Its registry identity is explicitly **3**,
with a new registry digest and provenance digest. The Python symbol and proof
envelope version do not claim registry 2 compatibility or alias old evidence.

Both registered voters must still satisfy all existing predicates: separate
operators, hosts, gateway and cache route identities; agreeing complete signed
creation populations and transaction-index inclusion; matching deployment,
group and lease identity and state; a common committed-tip-minus-2 finalized
height; exact height echoes; at most five blocks of height skew; and the existing
180-second freshness bound. SDK proof algorithms and resource-effect authority
remain unchanged. Settlement remains separate from execution closure.

## Consumer migration and release

1. Qualify the actual versioned candidate over complete affected signed subjects,
   creation blocks and transaction indexes, then fresh deployment, all groups and
   all leases. Archive capability probes alone are insufficient.
2. Use normal review and merge. The existing release workflow requires successful
   exact main push gates, verifies tag/source ancestry and rechecks those recorded
   gates before publishing. Preserve the current core 0.18.0 wheel pin and contract.
3. Consume the published immutable SDK wheel and its digest. Independently qualify
   the actual installed SDK/core and consumer source; do not mutate installed SDK
   globals or introduce environment-selected source fallbacks.
4. Regenerate and authenticate complete execution history, catalog and required
   owner creation bundles under registry 3. A validator expecting registry 3 must
   reject registry 2 source identities and digest triples. Preserve dated registry
   2 evidence as dated evidence; never rewrite its provenance.
5. Require a complete fresh consumer fleet census and normal cleanup recovery
   evidence before production activation or completion claims. Registry preparation
   does not establish a restored fleet or satisfy a monitoring window.
