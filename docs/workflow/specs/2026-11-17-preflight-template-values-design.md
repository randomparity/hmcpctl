# Refuse fictional identifiers before live-test preflight probes

Issue [#1898](https://github.com/randomparity/hmcpctl/issues/1898) describes a
partition provisioning leg skipped after its configured volume group could not
be found. Operators need all the relevant template variable names while they
can still correct the local configuration.

## Configuration and selection

Keep the fictional identifiers with their configuration fields in
`scripts/live_test/config.py`. Compare the parsed value for its own field to
the corresponding literal template. For the protected-name list, either sample
member is a fictional identifier even in a partially replaced list. Use the
existing field-to-environment mapping for diagnostics. Numeric settings,
generic profile defaults and the specific loopback listener can be valid in a lab.
Compare complete identifiers, including the commented dedicated system and
partition-prefix examples when explicitly configured. A similarly named real
target, or a template value belonging to a different field, passes this check.

Use the selected runnable verdicts in `scripts/live_test_preflight.py` to
determine which fields matter. Follow the named arm and its selected subtasks,
including shared baselines, protection checks, storage and ISO consumers.
HMC-only user management needs no managed-system name. Dedicated and bare-CEC
use their dedicated system and prefix. A disabled opt-in arm contributes no
fields. Named boot launchers consume their own path and the shared download
host. Both native launchers also read LIVE_TEST_LPAR_NAME to verify the owner
of the guest address. The ordinary provision-boot arm skips that lookup.
The bounded detach probe needs its system and volume group. Packet-owned
routes continue to use their own admission. A native preflight that consumes
the shared managed system must pass the same check before invoking its reader.

## Refusal and failure model

After finalizing arm selection and the detach override, collect all affected
keys for each runnable group. If any match, print the group and key names
with a request to edit the local configuration, and return nonzero before
any HMC client, native preflight, envelope or VLAN probe. Aggregate selection
fails before the first probe even if some other selected groups are clean.
The same diagnostic applies in configuration-only mode. The existing
configuration, credential and specific-bind checks retain their own failures.
Scope is limited to preflight. Runtime hardware eligibility, packet admission,
direct runner and recovery behavior continue under their existing contracts.

## Verification

Offline CLI tests exercise individual fields and mixed selections using a real
temporary configuration and refusing hardware doubles. Cover multiple offending
keys, the unchanged sample, admitted versus skipped groups, a partially replaced
protection list, near matches, native/packet input ownership, detach and round2.
Exercise a corrected fixture through normal success, including existing probe
tests. Update the operator guide to distinguish a preflight template refusal
from runner validation. Run the repository static, smoke, test and artifact
guards and all hooks. Live hardware is outside this verification.
