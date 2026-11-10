# Creation-memory private entry consolidation

## Decision
Replace the independent live_creation_memory.py parser/custody/dispatch body with
runner._run_private_group("creation-memory", __doc__, argv), matching five existing wrappers.
In _run_private_group call existing private_destination unconditionally, rather than the
selector-dependent _validate_private_results: the fixed private entry must always be private,
including creation-memory which is absent from the direct-selector policy map. Keep that map,
runner argument schema and numbered task/group policies unchanged.
The shared private_destination currently checks canonical absolute/symlink/fresh results and
sidecar/existing mode0700 parent/outside all repositories, but lacks old wrapper's UID check.
Add parent.st_uid == os.getuid() there, retaining every current check and fail-closed behavior.
Migrate the existing owner-test patch to this actual shared owner. No wrapper-local policy.
Existing helper sets077 for dispatch and restores prior umask in finally; creation-memory now
uses the same custody guarantee. Keep exact fixed group and return/exception propagation.

## Failure model
Required: reject occupied results/sidecars, dangling result/sidecar symlinks, relative/noncanonical
paths, missing/exposed/foreign-owned parent and repository destinations before configuration or
dispatch. Successful/failing dispatch restores original umask. All existing checks remain.
Accepted: admission is not a transaction against hostile concurrent filesystem replacement;
existing private directory custody and atomic0600 writer remain. Existing common canonical policy
replaces the old wrapper's path.resolve acceptance; no compatibility alternate route is added.
No new HMC operation, hardware evidence or permission is introduced.

## Validation
Focused-test: add creation-memory dangling result/sidecar, relative/repository and success/error
umask cases. Observe actual oldwrapper dispatch/umask violation red, then commonhelper green.
Existing owner/freshness/help/fixedgroup tests retain assertions and patch actualowner.
Task-test-not-applicable: helper reuse introduces no new domain workflow; existing runner,
creation-memory arm, all five privatewrapper and corresponding arm/recovery cases prove retention.
Run affected offline tests, lint/type/format/diff and all14normalhooks. Root final fullverify,
pinned allfilehooks and wholebranchreview remain. No hardware or publicwrite.
