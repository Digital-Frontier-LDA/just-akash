# Dormant Sentry JIT tenant startup

`just_akash.sentry_jit_payload.render` is a source ingredient for a future
hosted controller. It is disabled by default and has no CLI or workflow caller.
It does not create a lease, mint a runner, authorize publication or qualify the
original 600-second route.

The historical publication of `df-akash-runner` commit
`bd9a14d8d3146ce6a20f6b559f963da0c956f3b9` produced manifest-list digest
`sha256:aaf3799b5e138abef0831bb8467ded7325164316f0cfde5c183fe6e129eae79e`
in GitHub run `34079692367`, job `101612500316`. Its token-only entrypoint predates
the repository's JIT startup support. Its Dockerfile does establish runner
2.337.0 under `/actions-runner`, from SHA256-pinned official archives.

Official `actions/runner` commit `397b032cbf865e9c3ddfab89d533ec19325e1273`
supports `ACTIONS_RUNNER_INPUT_JITCONFIG` and removes that environment input in
`CommandSettings`. The new payload uses `/actions-runner/bin/Runner.Listener run`
directly, avoiding the `run.sh` retry loop. It validates canonical outer and
inner base64, unique JSON keys, safe filenames and exact lower-camel `.runner`
`agentId`, `agentName` and `ephemeral: true`. Delivery is limited to 64KiB before
allocation to keep a single environment entry comfortably bounded; the original
JIT API primitive's 1MiB response bound remains unchanged. Numeric identity uses
the exact JSON integer range supported by the tenant's jq parser.

The startup program has no secret argv, registration-token fallback, local mint
or restart loop. It removes inherited exported environment before Listener exec.
Listener stdout/stderr are suppressed so startup failures cannot echo credentials.
The controller must use authenticated GitHub runner/job observations rather than
tenant log text. Actual job-log transport and execution remain unqualified.
Listener itself writes its short-lived configuration files into its runner root;
memory-only delivery does **not** mean Listener never persists its credentials.

Storage must already be declared in the input SDL. The renderer never adds a
volume or changes any profiles/deployment bytes. This distinct candidate requires
exactly one named `jit-state` 40GiB persistent volume of existing standard class
`beta3`, mounted at `/actions-runner/_work`. The actual nested JIT `workFolder`
must be `_work`, matching the unchanged mint request. It refuses 39GiB plus 1GiB
splits. CPU, memory and replicas remain 2, 6GiB and one.

Official runner `HostContext.cs` at the same immutable commit derives work from
runner root plus `WorkFolder`, then temp from work plus `_temp`.
`TempDirectoryManager.cs` exposes that path as runner temp. Existing backend
`104ac8b937f13ba4c1b40269d0246963764db205` workflow passes `RUNNER_TEMP` for the
report; the supervisor creates its task beside that report, and the owned payload
creates its runtime and QEMU rootfs beside its output. Thus their source paths can
share this one 40GiB work volume. These are source facts, not runtime observations.

The legacy literal unnamed 40GiB ephemeral-root template remains unchanged and
held by this renderer. Standard provider source only sets an ephemeral filesystem
limit for nonpersistent storage; a persistent 40GiB request does **not** prove
the same root/overlay capacity. The candidate proves neither available filesystem
bytes after overhead, image/update/root needs, actual guest disk capacity, actual
PVC/mount nor original 600-second route qualification. No root-floor equivalence
or resource-budget trade is claimed. These require actual owned runtime evidence.

Read-only metadata observed all three owned providers advertising persistent
storage class `beta3`; that is no proof of a bid, PVC, mounted filesystem,
retention across replacement, or the running provider version. RAM EmptyDir
would add to the container memory limit in standard provider source, so this
route refuses it. No provider changes or additional resource budget are allowed.

At runtime startup requires a distinct non-RAM filesystem mount, safe ownership
and mode, no previous runner configuration, and an exclusive `.jit-consumed` directory.
It flushes that directory's filesystem before executing Listener. Failed flush,
interrupted exec and any Listener exit retain the consumed marker. This prevents
replay only while that same mount is retained. Lost state or pod recreation must
remain UNKNOWN in the external controller until exact runner/lease reconciliation;
the local marker is never hosted authority or a replacement for cleanup.

No existing token workflow, renderer, build assertion, 600-second application
clock, 10-second cleanup, or 2-CPU/3072MiB guest contract changes. Actual owned
tenant startup, mount provenance, exact runner-to-lease binding, producer artifact
binding, whole-route timing and independent cleanup must pass before activation.
