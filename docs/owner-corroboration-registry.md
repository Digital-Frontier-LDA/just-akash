# Owner corroboration registry 4 candidate

On October 20, 2026, the SDK 1.50.0 candidate replaces only registry 3's Crouton
entry with Allnodes' PublicNode REST route. Polkachu remains byte-for-byte
unchanged. This is a source proposal, not a declaration that the candidate is
released, fully qualified or active in a consumer. Registry entries and capability
observations do not grant resource authority.

## Sources and provenance

Both operators' REST and RPC endpoints are listed in
[the pinned Akash chain registry](https://github.com/cosmos/chain-registry/blob/cc1ed04b31326bc79f3208e571fb71fa0fddb987/akash/chain.json).
The existing provenance file SHA-256 remains
`3720728bb01b964132cf1452678c6aaf20f0f5b2df098e2d56ceb577e741b1c2`.
[PublicNode publishes the service](https://www.publicnode.com/); the pinned
registry attributes the Akash routes to Allnodes and Polkachu.

| Source | REST endpoint | Operator | Named gateway / cache route |
| --- | --- | --- | --- |
| `allnodes` | `https://akash-rest.publicnode.com` | Allnodes | `allnodes-publicnode-rest` / `allnodes-publicnode-rest-route` |
| `polkachu` | `https://akash-api.polkachu.com` | Polkachu | `cloudflare-polkachu-api` / `cloudflare-polkachu-api-route` |

On October 20, public REST `default_node_info.default_node_id` and the corresponding operator
RPC `node_info.id` identified the same node for each operator, with distinct
identities between operators:

- Allnodes: `821fa0f7ce74a211c5f5ec93cc6cc301564b92b6`.
- Polkachu: `26402e60a2430168851743e2b14e06945f28a8b4`.

Published operator attribution and matching REST/RPC node identities support the
inference that these are separate operator trust paths. Equal chain responses
alone would not establish it. The route labels name public gateway and cache
control domains; they do not assert cache absence, direct backend access or
exhaustive knowledge of the deployed infrastructure. Polkachu's
[published endpoint implementation](https://github.com/polkachu/cosmos-validators/tree/2b0d6bc4c4ffe59cc69e2c1fc93a0d96b3f36bfa/roles/support_public_endpoints)
describes localhost proxying, not an attestation of its exact current deployment.

## Public capability observation, not owned-subject qualification

On October 20, three bounded GETs to the Allnodes route returned an `akashnet-2`
latest block and HTTP 200 for both `GetBlockWithTxs` and the original indexed
transaction query at public height `29135224`. Both historical requests used a
freshly selected state-height header, which the responses echoed exactly. The
nonempty decoded block-transaction and indexed transaction/response list lengths
agreed. These results demonstrate the observed public
route capability at that height only. They do not authenticate an affected owned
creation transaction, establish complete affected-subject coverage, prove
continuity of a dated closure or qualify cleanup.

## Dated registry 3 transport history

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
consumers of the existing proof API. Its candidate registry identity is explicitly
**4**, with a different source registry digest. The pinned provenance document
and its digest are unchanged because that document already lists both operators.
The symbol and proof envelope do not alias registry 3 or registry 2 evidence to
registry 4.

Both registered voters must still satisfy all existing predicates: separate
operators, hosts, gateway and cache route identities; agreeing complete signed
creation populations and transaction-index inclusion; matching deployment,
group and lease identity and state; a common committed-tip-minus-2 finalized
height; exact height echoes; at most five blocks of height skew; and the existing
180-second freshness bound. SDK proof algorithms, proof mode, all read and
response budgets, and resource-effect authority remain unchanged. Settlement
remains separate from execution closure. The core 0.18.0 wheel pin is unchanged.

## Consumer migration and release

1. Qualify the actual versioned candidate over complete affected signed subjects,
   creation blocks and transaction indexes, then fresh deployment, all groups and
   all leases. Verify close recovery too: both voters must bind the complete
   signed close block and indexed population, successful per-hash transaction,
   and fresh terminal state. Public capability probes alone are insufficient.
2. Use normal review and merge. The existing release workflow requires successful
   exact main push gates, verifies tag/source ancestry and rechecks those recorded
   gates before publishing. Preserve the current core 0.18.0 wheel pin and contract.
3. Consume the published immutable SDK wheel and its digest. Independently qualify
   the actual installed SDK/core and consumer source; do not mutate installed SDK
   globals or introduce environment-selected source fallbacks.
4. Regenerate and authenticate the actual required execution history, catalog and
   owner creation bundles under registry 4. Validators must bind the new registry
   version, source digest and unchanged provenance digest explicitly. A validator
   expecting current registry 4 must not accept registry 3 source identities or
   digest triples as registry 4. Preserve original registry 3 and registry 2
   artifacts as dated evidence, without rewriting their provenance or asserting
   compatibility of their closure identities with the new voters.
5. Require a complete fresh consumer fleet census and normal cleanup recovery
   evidence before production activation or completion claims. Registry preparation
   does not establish a restored fleet or satisfy a monitoring window.
