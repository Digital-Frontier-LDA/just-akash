# Terminal positively fenced unused-JIT removal (source proposal)

Fixed module: `python -I -m just_akash.scanner_v2_unused_cleanup`. This separate
terminal-only SDK callee receives only hosted GH_RUNNER_PAT; it has no Akash,
AGE, registry credential, wallet, deployment or lease-delete capability.

The reader independently binds the executing current actor to the protected-main
BB workflow_run callback, native job/step/latest attempt and exact SDK callee. It
reads the exact historical producer attempt/job/step/source, complete native
artifact pages, immutable compressed size/digest and canonical body bytes. This
first version requires the producer and cleanup SDK SHAs to be equal, binding the
positive witness to the same captured producer implementation. A later
cross-version adapter requires explicit source-pin review.

A complete JIT NO_WIRE/UNKNOWN/ACK chain plus lease-slot FENCE/NO_CREATE is required.
NO_CREATE is parent-attested actual child exit66/no signal with zero reserved
create frames. Any deployment phase, provider-lease phase, missing/duplicate/
foreign-job receipt, later producer attempt, cancelled/timed-out/unknown result,
or incomplete history holds. No owner/DSEQ/closed lease is invented. Only the
exact acknowledged returned native runner ID in the original required private
group is eligible, after fresh offline/idle proof.

Removal uses the original target-specific concurrency/effect namespace, complete
prior-handler history and durable immutable NO_WIRE/UNKNOWN/readback before the
one actual remove boundary. A poisoned one-use latch, renewed native/receipt/
history/group/ID checks and the held current UNKNOWN ACK gate that boundary.
Incomplete/missing prior executed-handler evidence blocks; no handler is silently
ignored. UNKNOWN remains immutable. Lost remove ACK never permits a second wire;
only two complete all-status org snapshots plus direct404 can prove absence.

Outputs use `unused_jit_qualified`, never global `cleanup_qualified`; they assert
no deployment/lease state. Current BB8a22 still pins producer22be/cleanup747 and
has not been rewired. Future explicit BB callback/profile/callee/secret-policy
composition and genuine capacity admission are prerequisites. All runtime,
full-cleanup, default and cancellation coverage remain unqualified. An already
executed legacy cleanup handler without this intent remains a conservative HOLD.
