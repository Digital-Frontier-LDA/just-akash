# Scanner v2: returned registration ID and acknowledged create candidates

This optional source route uses role `bb-native-trufflehog-v2`. Existing profiles,
registration-token code, runner image/defaults, and original test oracles retain
their previous behavior. The SDK `scanner_v2` job is exclusive of the original
pool and rollback jobs. Its fixed hosted entry is:

```
<fixed hosted Python 3.13.9 venv> -I -m just_akash.scanner_v2_controller
```

The controller requires the private BB repository (1071436278), actual current
main dispatch/source, exact reusable SDK SHA, actual executing native producer
job/step, fixed resources (4 CPU, 16Gi memory, 96Gi storage, one ephemeral runner),
and the three existing owned providers. It repeats native admission and strict
restricted-group membership before privileged boundaries. The v2 group named
`bb-native-trufflehog-v2` is selected only for BB and its main SecretScanning
workflow. The manifest in `.github/runner-groups/bb-native-trufflehog-v2.json` is
required declarative state; it has not been applied. A missing real group ID
refuses. This route does not broaden or reuse Default.

A future BB reusable caller must use job name `Provision one owned native
TruffleHog runner v2`, pin the same immutable SDK revision in `uses` and
`just-akash-ref`, pass `runner-v2-group-id`, and consume SDK `runner-targets`.
The runner identity is `dfci-bb-th-<run>-a<attempt>-r<round>`; the placement is
`borduas-th-run-<run>-a<attempt>-r<round>-end`. Old v1 labels are incompatible.
The hosted job has only one SDK round and never replaces an unknown operation.

The hosted recorder acknowledges immutable public NO_WIRE, UNKNOWN, and ACK
records. Acknowledged native JIT ID is required before any tenant configuration
is delivered. No legacy registration-token preflight/verdict probe runs on v2.
The exact private E71 image already supports `RUNNER_JIT_CONFIG` at its actual
`/entrypoint.sh`; no image override, provider change, or rebuild is needed.
Tenant env has only encoded ephemeral JIT configuration, EPHEMERAL,
RUNNER_EXECUTION_USER, and RUNNER_NAME. Registry credentials use standard SDL
credentials, while management PAT/API/AGE credentials remain hosted.

The existing SDK deploy and create-lease seams add acknowledged public records
without changing other profiles. Wire commitments describe actual submitted
bytes; redacted public projections expose no configuration/token/password. The
controller retains the host-only SDK local candidate receipt on later ACK failure
and removes the credential-bearing temporary SDL. This local retention does not
survive host loss. The SDK only allocates/returns Console DSEQ after create: a kill after broadcast and
before returned identity remains unresolved. A public UNKNOWN never authorizes
replay, replacement, close, or registration removal.

This source is not activation. Missing actual group application/readback, hosted
immutable-backend behavior, private pull/Linux Runner.Listener/owned-provider
execution and full close/remove/readback/callback coverage remain HOLD. The
controller explicitly emits `cleanup_qualified=false`; normal lease closure and
native registration removal are not implemented by this new controller. Its
readiness result is only a candidate and cannot qualify full cleanup or rollout.
No GitHub CI run, live JIT mint, deployment, registry write, or provider mutation
was performed while preparing this source.

An independent official Go SDL v0.4.5-rc0 fixture currently rejects this unchanged
no-ports shape as `zero global services`. The proposed official-version/reconcile
prerequisite is therefore HOLD. No endpoint was silently added; this is not an
observed Console failure or proof of physical incompatibility. A separately
reviewed compatible payload/version solution is required before activation.
