# Fixed qualification branch compatibility

The pinned controller SDK06589cf accepts only main/master/deploy-env as explicit non-reusable workflow branches. Add the single literal ci-akash-qualification for a separately protected Flynn qualification snapshot. This is not a wildcard: exact repository, runner group, selected workflow ref and independently approved producer SHA checks remain required before JIT. Reusable workflow admission is unchanged.

This compatible patch is based only on06589cf, independently of later main changes. Controller/setup callers must require an immutable exact branch, validate its source and protection, and scope its private group/environment before dispatch. SDK branch-name acceptance alone is not admission, immutability, source approval or deployment authorization.

The isolated release branch runs exact copies of the original lint/typecheck/full unit/symbol-presence jobs. Original main CI, commands, coverage, permissions and paid Akash E2E remain unchanged. No paid Akash E2E is triggered by this compatibility PR; deployment/transport code is untouched. The eventual controller pin must identify the reviewed commit directly and retain all other compatibility/runtime requirements.

## Corrected immutable campaign

The first frozen caller at ci-akash-qualification pins a controller whose object-only GitHub reader refuses the branch-rules array before provisioning (Flynn run39136993564; controller correction df-cicd487). Preserve that branch, failed request markers and historical evidence. Add exactly ci-akash-qualification-v2 for a separately reviewed corrected caller; no version pattern or arbitrary branch is accepted. Keep the v1 literal available for its historical cleanup binding.

Exercise both literals against every supported branch restriction and both matching/mismatched producer revisions. The two campaigns cannot borrow one another's group workflow ref. Reusable workflow scope, repository/group/producer checks and original quality jobs are unchanged. This SDK patch creates no branch, environment policy, runner group, JIT configuration or deployment. A v2 controller/caller review, immutable source, fresh controls and separate durable no-replay record must precede a new request.
