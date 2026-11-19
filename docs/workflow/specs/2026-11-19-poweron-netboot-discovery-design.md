# Discovering the AIX/Linux PowerOn network-boot contract

[Issue #2032](https://github.com/randomparity/hmcpctl/issues/2032) asks which parameters
the installed HMC firmware binds to a network boot. The discovery record will identify
the build, the LogicalPartition PowerOn template, schema roots, and transitive includes
or imports. It will compare the operation-specific evidence with IBM's PowerOn reference.

An authenticated read of an AIX/Linux partition's template can advertise parameter names.
Schema inspection follows references from the partition and job documents and inspects
types, names, constraints and requiredness in their operation context. An empty template,
an empty HTTP 200, and a generic name/value container each leave the runtime contract open.
Missing or unreadable dependencies are recorded with the limits they place on the verdict.

The record will distinguish affirmative binding, explicit rejection and unresolved fields
for the inspected build. It will identify the evidence that would resolve each gap.
Only sanitized paths, firmware and structural findings are published. Production exposure
belongs to #868 and an actual boot belongs to #2033.

## Failure model

- Missing build, an unidentified partition environment, or an inaccessible template leaves
  that observation incomplete. Report which read failed and what a retry must establish.
- For each candidate in #868's PowerOn table, record the exact reference spelling,
  applicability and requiredness. This includes IBM i address descriptions, IPv6 image,
  adapter selection, retries, timeout, and alternate activation spellings, even if the
  live template has zero parameters.
- A runtime rejection applies only to its observed firmware, operation value and request
  context. Generic resource declarations and unmapped imports leave operation support open.
- Raw responses stay private. #868 owns the production decision and #2033 the boot proof.
