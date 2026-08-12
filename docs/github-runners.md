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
| `GITHUB_API_UNAVAILABLE` | The runner listing was never readable, so the pool was never observable | **Your GitHub API budget** — see below. Never a verdict about a provider. |
| `INDETERMINATE` | The tooling itself failed | Never a verdict about Akash |

---

## The GitHub API budget is the real ceiling on pool size

**GitHub cannot filter runners by label.** `GET /orgs/{org}/actions/runners` takes only
`name`, and `name` is **exact-match** — verified live: a 12-character prefix of a real
runner's name returns `0`. The runner image randomises each replica's name from
`RUNNER_NAME_PREFIX`, so the names aren't known in advance and the filter is useless here.

Every poll therefore pages the **entire org listing**:

```
requests per poll     = ceil(total_org_runners / 100)     ← all runners, not just yours
requests per attempt  = that × RUNNER_WAIT_TRIES (90)
```

A 300-runner org polled for the full window is **~270 requests for one provision**, ×3
attempts. Against:

| Credential | Primary limit |
|---|---|
| PAT | **5,000 req/hour, shared across every token of that user and every repo using them** |
| GitHub App (org) | 5,000/hr, scaling to 12,500 |
| GitHub App (Enterprise Cloud) | 15,000/hr |

plus a secondary limit of **900 points/minute** (GET = 1 point, DELETE = 5) and **100
concurrent requests**. A handful of concurrent pools on one PAT reaches the ceiling in
minutes.

This is why `GITHUB_API_UNAVAILABLE` is its own failure world. A throttled read used to
be counted as *zero runners online*, which destroyed the lease, excluded the provider,
and reported `RUNNER_NEVER_REGISTERED` — naming a provider a `runner_deny` candidate for
our own rate limit. The same conflation in `runner_probe` produced `POD_NO_REGISTER`,
which is a **permanent** disqualification.

**The listing shrinks or grows on its own.** Offline registrations accumulate, every one
adds to the page count of every future poll, and once they overflow a page they are also
harder to clean. Teardown de-registration isn't hygiene at this scale — it is what keeps
the polling cost bounded. Skipping it is a compounding leak.

Practical levers, in order of effect:

1. **Give this workflow its own credential.** The PAT bucket is per-user, not per-repo.
2. **Always run the teardown** (`if: always()`), so the listing stays small.
3. **Fewer, larger pools** beat many small ones — the poll cost is per-provision and
   scales with the whole org, not with your pool.
4. **Raise `RUNNER_WAIT_TRIES`/`min-pool-size` deliberately**: waiting longer is more
   requests, and 90 tries × 5s = 7.5 min is tuned for a handful of replicas.

---

## One wallet cannot carry a spike, and GitHub cannot serialise it for you

`AKASH_API_KEY` is a **single Cosmos account**. A Cosmos account cannot have two
transactions in flight — they share a sequence number, so the loser is rejected with an
account-sequence mismatch, and **nothing in `just_akash` retries that**. Every deploy,
every destroy, every tag is such a transaction.

This repo's own wallet-touching workflows are serialised under the `akash-wallet`
concurrency group for exactly this reason. **A reusable workflow cannot inherit that
protection**: GitHub scopes concurrency groups *per repository*, so a group named here
creates one queue per calling repo and none at all across them. Many repos sharing one
key are unserialisable by any group name written in this file.

Untreated, the rejection looks like silence from the market: no DSEQ, no provider, and
after the attempt budget a `PROVIDER_CAPACITY` verdict — a fabricated outage, produced
hardest at spike, when the real cause is your own concurrency. It is now classified as
`WALLET_TX_CONTENTION` and backed off with jitter (a fixed backoff keeps concurrent
callers in lockstep and simply re-collides).

Backoff buys headroom for a few overlapping callers. **It does not scale to a spike** —
one sequence number means transactions from that account are inherently serial, so N
concurrent provisioners spend most of their attempt budget queueing behind each other.

**Escrow compounds it.** Every live lease holds its deposit against the same grant:

```
free = sum(grants) − escrow          ← what predicts a 402
```

At the default `required-deposit-usd: 5`, 25 concurrent pools lock ~$125 before a single
job runs. A grant measured at $170.62 was once $165 locked. So a spike does not fail at
the marginal deploy — it fails when the *aggregate* of everyone's live leases crosses the
grant, and the callers that lose are whichever deployed last.

Two shapes work at spike, and they are the same idea:

1. **One Akash account per concurrent provisioner.** Separate `AKASH_API_KEY`s mean
   separate sequence numbers and no contention at all. Escrow still has to be planned in
   aggregate, but nothing serialises.
2. **Funnel provisioning through one repo** that owns the wallet and serialises on the
   `akash-wallet` group, handing labels back to callers. Simple, but the queue is the
   throughput ceiling — the opposite of a spike.

Ephemeral churn pushes toward (1): `EPHEMERAL=true` means a runner leaves after one job,
so a spike is a continuous stream of create/destroy transactions, not a single burst.

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
just-akash runner-probe \
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

Deployments are tagged `<tag-prefix>-<run_id>` so a sweeper that matches on tags reaps
this run's lease and nothing else.

Note the limit of that guarantee: just-akash's tags live in a **local file**, never on
chain, so `--reap-runners` below cannot read them. It selects **every** old lone `runner`
deployment on the account, not just ones matching your prefix. `tag-prefix` scopes a
tag-matching sweeper; it does not scope that flag. A shared `ci-<id>` default once let one repo's cleanup **destroy a sibling
repo's live deployment**. Put your repo name in it.

## Always run the teardown

Two things leak, and only one is the lease:

1. **The lease** holds escrow against the grant the next run spends from — so a leak makes
   the *next* run's funding failure look like a market outage.
2. **The runner registration** persists in the org after the pod dies. Offline
   registrations once overflowed the first page of an org's runner listing and broke
   provisioning for *every repo in the org*. `EPHEMERAL=true` removes a runner that **ran
   a job**; it does nothing for one that registered and was never used.

**Nothing else reaps a runner lease by default.** `python -m just_akash.cleanup_stale`
classifies by service set, and a pool's service is `runner`, which lands in
`LEAVE-real-or-unknown` — nothing on chain proves a `runner` service is yours, and a
sweep that reaped by shape alone once destroyed 14 third-party deployments. Pass
`--reap-runners` to close lone `runner` services older than 6h; that flag is you
asserting the Console account hosts nothing but your own pools. Six hours, not one,
because `ephemeral: false` outlives a job and a slow matrix runs for hours.

The teardown also reports `deregister_failed`. Treat non-zero as urgent rather than
cosmetic: each surviving registration stays in the listing every future poll pages
through, so it makes the next run slower and likelier to be throttled.

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
