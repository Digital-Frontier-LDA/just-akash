# Akash-hosted GitHub Actions runners

Two reusable workflows that run your CI on Akash instead of GitHub-hosted runners, and —
more importantly — **tell you why** when they can't.

> [!note]
> **`runner-v1` is tagged** (2026-08-07). The gate: **three providers, each passing three
> CONSECUTIVE full-bar attempts** — 9 of 9 overall, and 3/3 per provider, which is the
> part that matters. Consecutive-per-provider is the bar; 9/9 in aggregate would also be
> satisfied by one provider passing nine times, and that is not the same claim. Each
> attempt was scheduled → registered → **ran a real dispatched job** → torn down cleanly.
> See *Which providers may host a runner*.
>
> **`runner-v1` is MOVABLE.** Referencing it is not pinning: when the tag moves, every
> consumer's CI changes with it, without a commit on their side. The examples below use
> `@runner-v1` for readability — for reproducible CI, reference a **commit SHA**, which
> is the only immutable form.

```yaml
jobs:
  pool:
    uses: Digital-Frontier-LDA/just-akash/.github/workflows/runner-pool.yml@runner-v1
    with:
      runner-label:  fast-pool-${{ github.run_id }}
      tag-prefix:    ci-myrepo          # REQUIRED — must name YOUR repo
      github-org:    my-org
      pool-size:     '4'
      min-pool-size: '2'                # 2 of 4 is still usable
    secrets:
      AKASH_API_KEY: ${{ secrets.AKASH_API_KEY }}
      GH_RUNNER_PAT: ${{ secrets.GH_RUNNER_PAT }}

  build:
    needs: pool
    runs-on: ${{ fromJSON(needs.pool.outputs.runner-targets) }}
    steps:
      - run: make test

  teardown:
    needs: [pool, build]
    if: always()
    uses: Digital-Frontier-LDA/just-akash/.github/workflows/runner-teardown.yml@runner-v1
    with:
      dseq:         ${{ needs.pool.outputs.dseq }}
      tag-prefix:   ci-myrepo
      runner-label: fast-pool-${{ github.run_id }}
      github-org:   my-org
    secrets:
      AKASH_API_KEY: ${{ secrets.AKASH_API_KEY }}
      GH_RUNNER_PAT: ${{ secrets.GH_RUNNER_PAT }}
```

`runner-targets` is the ergonomic core: the pool's labels when healthy, `["ubuntu-latest"]`
when not. `runs-on` cannot be conditional, so without this a bad pool means every
downstream job fails to schedule.

---

## The problem this actually solves

The failure was never "Akash is unreliable". It was that **every** failure printed
`(infra)`. A drained wallet, a market with no capacity, and a provider that takes the
lease but never starts the pod are three different problems with three different fixes —
and they were indistinguishable in the logs.

So the standing remedy became "switch this job back to GitHub-hosted runners." That
works, and it is why the bill grows. Measured at ~25,900 billable minutes per week.

Every failure here names which world it came from, via the `failure_reason` output:

| `failure_reason` | What it means | What to actually do |
|---|---|---|
| `WALLET_UNDERFUNDED` | Free credit after escrow is below the threshold | **Top up the wallet.** Not a CI defect. |
| `NO_ELIGIBLE_BIDDER` | The provider spec was malformed or filtered to nothing | Fix the `providers` input |
| `PROVIDER_CAPACITY` | Nobody bid within the window | Market condition — retry later or widen the pool |
| `RUNNER_NEVER_REGISTERED` | A provider won the lease, the runner never came online | Qualify that provider; it is a `runner_deny` candidate |
| `INDETERMINATE` | The tooling itself failed | Never a verdict about Akash |

---

## Free credit is not the grant

This is the single most expensive misreading in the history of this setup.

```
free = sum(grants) − escrow
```

Every **active** deployment holds escrow against the same grant. So the grant alone reads
perfectly healthy while Console already returns `402`. Measured once at 165 of a 170.62
grant locked by live deployments — a dashboard showing "170 available" next to a CI run
failing with insufficient balance.

`free_usd` is the number that predicts a 402, and it's exposed as an output so a failure
can say *"top up the wallet"* instead of *"(infra)"*.

A `402` is also **not** a missing bid: it is rejected before an order exists, so no
provider ever saw it. The pool classifies it separately and does not retry, because
retrying a balance rejection just burns the attempt budget and then reports a market
outage.

---

## Which providers may host a runner

A provider can be online, huge, cheap, willing to bid, and **win** — and never schedule
the runner pod. That lease is worse than no bid: it consumes the attempt, holds escrow,
and stalls to the timeout. One instance was traced to an 1800-second stall.

This is not inferable from price or health. **just-akash takes the cheapest bid in
whatever set you give it**, so an unproven provider undercutting a proven one *captures*
the runner and kills it — measured at ~24 uact against ~27. Hence the markers:

```yaml
providers: |
  [{"address":"akash1…","runner_host":true,"failover_priority":10},
   {"address":"akash1…","runner_deny":true},
   {"address":"akash1…"}]
```

| Marker | Meaning |
|---|---|
| `runner_host` | **Proven** to bring a runner online. Ordered as a strictly earlier tier. |
| `runner_deny` | Leases but never schedules the pod. **Never tried.** Wins over any other marker. |
| `failover_priority` | Order within a tier. Lower first. |

There is **no default provider list**, deliberately. These markers are measurements of
one specific fleet at one specific resource profile; shipping a default would make one
operator's trust decision everyone else's. Qualify your own:

```bash
# No PAT needed. With `admin:org` on your existing credential the probe mints a
# short-lived runner registration token itself, so full qualification is the default.
python -m just_akash.runner_probe \
  --providers akash1…,akash1… \
  --cpu 4 --memory 16Gi --storage 30Gi \
  --org my-org --attempts 3 --json
```

Each attempt takes a **real lease and spends real credit**, so it stops early on a
disqualifying outcome rather than paying three times to confirm what one attempt proved.

> **Minting beats supplying a PAT, and not only for convenience.** A PAT expiry is
> silent — it surfaces as `runner did not come online` after a ~15-minute wait, i.e.
> indistinguishable from a provider fault. An expired PAT handed to the probe reports
> `POD_NO_REGISTER` and demotes healthy providers for what is really your own credential
> failure. A token minted seconds before use cannot be stale.

### Outcomes, and which of them can promote

| outcome | meaning | promotes? |
|---|---|---|
| `PASS` | every stage measured and passed | ✅ counts toward the streak |
| `SCHEDULED_ONLY` | scheduled fine; registration and/or job **never measured** | ❌ never |
| `NO_BID` | capacity or price — says nothing about hosting | ❌ and never demotes |
| `LEASE_NO_POD` / `POD_NO_REGISTER` / `JOB_NOT_RUN` | measured failures | ❌ **permanent `runner_deny`** |
| `TEARDOWN_FAILED` | hosted fine but leaked the lease | ❌ our bug, not theirs |
| `INDETERMINATE` | the probe itself failed | ❌ never a verdict about a provider |

`SCHEDULED_ONLY` exists because "not checked" and "checked and fine" are different
claims. Without it, a probe that never measured registration returned `PASS`, and three
of those promoted a provider to `runner_host` on a bar nobody verified.

### It refuses to disqualify without a positive control

If a run never observes a running container on **any** provider, every disqualification
is downgraded to `INDETERMINATE` and the run says so loudly.

This is not defensive padding. A detector that has only ever returned one answer has not
been validated — it has only been observed agreeing with itself. A probe once reported
`LEASE_NO_POD` for the fleet's one production-proven host, because an un-propagated lease
looks exactly like one that will never schedule; a `runner_deny` was recorded from it and
had to be withdrawn. Since `runner_deny` is permanent and outranks any later streak, a
false one silently shrinks the pool.

**Include a provider you know serves in every probe run.** It costs one lease and makes
every negative in that run mean something.

A provider is promoted to `runner_host` only after the real runner SDL, at **your**
profile, is scheduled → registers within 120s → **runs a real no-op job dispatched at its
own label** → tears down cleanly,
**three consecutive times**. Disqualification outranks any later streak: promotion has to
be harder than demotion when the failure is expensive and silent while success is cheap
and obvious.

> **Storage is the tightest constraint on eligibility.** A provider qualified at 30Gi is
> *not* qualified at 100Gi. Re-probe when you change the profile.

### You want at least 3 proven hosts

Below that, one silent provider takes the whole pool down and CI falls back to billed
runners — the exact cost this removes. The pool emits `runner_hosts_proven` and warns
while you are under three.

---

## A partial pool is usable

`min-pool-size` exists because all-or-nothing is not the conservative choice, it is the
destructive one. A provider once delivered 6 of 12 runners and was rejected **and had its
lease closed** — removing the only provider actually serving us, in favour of nothing.

Set `min-pool-size` to the smallest count your matrix can make progress on. The run warns
when it accepts a partial pool, so degraded capacity doesn't masquerade as slow CI.

---

## `tag-prefix` is required and has no default

Deployments are tagged `<tag-prefix>-<run_id>` so a sweeper reaps this run's lease and
nothing else. A shared `ci-<id>` default once let one repo's cleanup **destroy a sibling
repo's live deployment**. Put your repo name in it.

## Always run the teardown

Two things leak, and only one is the lease:

1. **The lease** holds escrow against the grant the next run spends from — so a leak makes
   the *next* run's funding failure look like a market outage.
2. **The runner registration** persists in the org after the pod dies. Offline
   registrations once overflowed the first page of an org's runner listing and broke
   provisioning for *every repo in the org*. `EPHEMERAL=true` removes a runner that **ran
   a job**; it does nothing for one that registered and was never used.

`if: always()` matters: a teardown gated on success leaves the lease open on exactly the
runs that failed — the ones most likely to have left something burning escrow. The
teardown reads the deployment state back rather than trusting the exit code, because
`just-akash destroy` exits non-zero while printing `Deployment closed`, and a zero exit is
not proof either.

---

## Required secrets

| Secret | Why |
|---|---|
| `AKASH_API_KEY` | Akash Console API. just-akash reads this name directly. |
| `GH_RUNNER_PAT` | Registers runners into the org and polls their status. |

The PAT needs org runner-registration rights. It is embedded in the SDL, so the rendered
SDL is echoed with `ACCESS_TOKEN` stripped, and `actions/checkout` runs with
`persist-credentials: false` — a persisted token on a runner that later executes job code
is a credential the job never asked for.

> **A PAT expiry is silent.** It surfaces as `runner did not come online` after a 15-minute
> wait, which reads exactly like a provider fault. If runners stop registering across
> *all* providers at once, check the PAT before suspecting the market.
