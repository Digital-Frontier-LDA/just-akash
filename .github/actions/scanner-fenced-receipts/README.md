# Scanner positive prelease fence (source proposal)

The two existing scanner-v2 profiles delegate to four byte-exact original action
modules in `legacy/`. The new `bb-native-trufflehog-v2-fenced` logical profile uses
the same native scanner_v2 job/step, fixed private E71 physical profile, original
JIT admission and official artifact client.

The fixed Python3.13.9 child starts with `-I -S`. The action captures the bootstrap,
eight-module pure SDK closure and their SHA256 table before SDK execution. No
site/.pth code executes; imports outside that closed pure namespace are refused
until the controller irreversibly changes its state to MAY_CREATE. The JIT mint
has already returned and its real native ID has an immutable read-back ACK.

Before enabling that runtime, the child must receive a typed MAY_CREATE control
ACK from this live parent, bound to the exact JIT ID, binding and request hash.
The parent irreversibly disables its own NO_CREATE route before replying.
Missing, partial or mismatched replies prevent runtime entry. This control ACK
is volatile session state; it is not a durable artifact or a new cleanup proof.

Only an ordinary failure in the fixed guest-contract, empty-private-scratch or
reader/helper metadata recheck can poison the child to FENCED. The parent closes
all later deployment/lease reservations before acknowledging FENCE. Only after
actual child exit66 with signal=null may the still-running parent upload and
read back NO_CREATE linked to FENCE and the real JIT ACK. This is positive
source-bound evidence, not inference from absent artifacts, outputs or DSEQ.
The captured pure closure launches no subprocess/helper/descendant. Receipt
workers have artifact/read credentials only and no create credentials.

The create latch is poisoned before unsafe SDK imports, SOPS/Go helpers, original
NO_WIRE/UNKNOWN and deploy entry. Any later failure is outside this fence route.
The original deployments and default/legacy profiles retain their old paths;
there is no automatic provider cleanup or create retry. Native and group gates
repeat before irreversible effects. No private credential/config enters public
receipt bodies. The bounded official-client transport may retry the same public
immutable artifact bytes before an effect; that never retries JIT/create/remove.

A signal/kill before attestation, missing/lost readback, unknown child exit or any
create reservation yields HOLD. A parent/job cancellation after durable NO_CREATE
is different from a child signal, but the first reader deliberately refuses a
cancelled native producer and leaves that case unsupported. Post-helper failures
and physical hosted runtime/durability remain unqualified. Per-run artifacts are
not cross-run CAS or an exactly-once service.
