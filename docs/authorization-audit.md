# Authorization audit records

The server writes one structured record for every authorization decision it makes at the
MCP dispatch boundary, and further records for the other things an operator has to be
able to audit. The sections under [The records](#the-records) are the full set; this
document is the contract they keep. The decision behind it is
[ADR 0040](adr/0040-authorization-audit-events.md).

## What you get, and when you get nothing

**`authorization` records** exist for calls the access policy authorizes — which is all of
them, since ADR 0041 made a policy mandatory. Before it, a server started without
`--access-policy NAME` had no authorizer on any dispatch path and emitted no authorization
record at all, for the same reason it enforced nothing at all. There is no such server now,
so every deployment writes one record per decision, and the delivery guarantee below
applies to every deployment rather than only to those that opted in.

**`ownership-override` and `ownership-denied` records are not policy-gated.** They come
from the ADR 0011 ownership check inside the handler, which runs whether or not a policy
is selected — and on the CLI and Python API paths, which have no policy at all. So an
unpolicied server can still produce those. For an `hmcpctl.api` consumer that check is the
only authorization boundary that applies, which is why its refusals are recorded here
rather than left to the `authorization` event ([ADR 0100](adr/0100-ownership-denial-audit-record.md)).

**`install-attempted` records are not policy-gated either**, and for a stronger reason:
the detached `installios` path has no ownership check to gate and no HMC job to poll, so
for an `hmcpctl.api` consumer this record is the only trace an irreversible install
leaves in this process ([ADR 0102](adr/0102-install-submission-audit-record.md)).

Other things produce no record, by design:

- a call to a tool the policy's ceiling withheld — it is never registered, so nothing
  reaches the boundary;
- `tools/list`, which discloses the whole ceiling in one call.

`hmc_list_configured_hosts` and `hmc_effective_permissions` declare no connection
argument and used to be a third case. They are recorded now: the target dimension
decides for them, so a `targets` table denies both with reason `target-unboundable`
and an `"all-targets"` grant permits them. Their `connection` object reads
`"state": "absent"` with a **null** `"resolved"`, which is what distinguishes a tool
with no connection argument from a caller who omitted `profile` — the latter resolves
to `"<default>"`.

**An empty audit stream is therefore not evidence that nothing was attempted.**

## The records

Each is one physical line of ASCII JSON. `time` and `event` come first on every one,
and every caller-supplied value is truncated to **128 characters** with no marker — a
truncated value is exactly that long, so you can measure it.

### `event: "authorization"`

| field | value |
|---|---|
| `time` | UTC, ISO 8601 |
| `event` | `"authorization"` |
| `policy` | the selected policy's name |
| `tool` | the MCP tool name |
| `effect` | `read`, `mutate`, `destructive`, or `arbitrary-command` |
| `decision` | `"allow"` or `"deny"` |
| `reason` | one of the codes below |
| `connection` | `{"state", "selector", "resolved"}` |
| `targets` | a list of `{"kind", "argument", "state", "value"}`, or `null` |
| `attribution` | `{"claim", "source", "verified"}` |

```json
{"time":"2026-08-19T18:00:00+00:00","event":"authorization","policy":"lab-scoped","tool":"hmc_power_off_lpar","effect":"destructive","decision":"deny","reason":"target-not-granted","connection":{"state":"present","selector":"lab","resolved":"lab"},"targets":[{"kind":"lpar","argument":"lpar_name_or_uuid","state":"present","value":"other-01"}],"attribution":{"claim":null,"source":"environment:HMC_AGENT_ID","verified":false}}
```

`connection.state` is `present` when the caller supplied a connection string, `absent`
when it supplied nothing or an empty string, and `unreadable` when it supplied a value
of another type. That value's `repr()` is never rendered. `connection.selector` is the
caller's own string, or `null` in the other two states. A `targets` entry's `state` is
the same vocabulary, describing one selector instead of the connection.

`connection.resolved` is which HMC the call would actually have used: a profile key,
`"<default>"` for the environment/default connection, `"<unresolved>"` when the token
names nothing configured, or `null` when the configuration could not be read at all.

> **`<default>` and `<unresolved>` are reserved renderings that share a string space
> with legal profile keys.** Profile names are TOML keys and are not validated, so
> `[profiles."<unresolved>"]` and `[profiles."<default>"]` are both legal — and a call
> naming one renders identically to the sentinel it collides with. A filter on either
> value would collect both cases. Rename such a profile if you have one.

> **An `HMC_HOST` collapse is visible only when the caller named a connection.**
> `state: "present"` with `resolved: "<default>"` arises no other way — unless you have a
> profile literally named `<default>`, per the collision above. A caller that omitted the
> argument renders `absent`/`"<default>"` whether or not `HMC_HOST` is set.

`targets` is `null` when the selectors were never extracted — the
`configuration-unreadable` case, and also the rarer one where assembling the record
failed and it degraded to nulls rather than being dropped. Treat `null` as "not
recorded", for any reason code, rather than as a synonym for one. `[]` means the tool
declares no selector. The same reading applies to a `null` `connection.resolved`. An integer selector —
`vios_partition_id`, the only one — records `state: "present"` with its decimal
rendering; a boolean records `unreadable`.

### `event: "ownership-override"`

Emitted when an [ADR 0011](adr/0011-multi-agent-lpar-ownership.md) LPAR ownership
override is exercised. Always `WARNING`.

**Not every record is a human decision.** Most are: a caller passing
`ownership_override` is an operator-approved exception to a single mutation. One
internal caller also emits it — `provision_lpar`'s activation leg passes the
override unconditionally, because the ownership token it would authorize against is
the one the same workflow stamped moments earlier
([ADR 0092](adr/0092-uniform-lpar-ownership-authorization-rule.md) Consequences).
That leg is reached only when `HMC_AUTHORIZE_POWER_OPERATIONS` is on, so with the
guard off — the default — every record in this stream is caller-supplied. With it
on, expect one record per `provision_lpar(power_on=True)`. The record carries no
field distinguishing the two sources; an alert on this event should account for that
before the guard is enabled.

The converse holds too, and it is the more surprising half: while the guard is off,
the power path emits **no** record even when a caller passes `ownership_override`.
The parameter is read only inside the guarded branch, so with the setting off it is
inert — nothing was bypassed, because nothing was checked. Silence in this stream on
the power path is therefore not evidence that no override was requested.

**This event records ownership checks that were *bypassed*, never ones that
*refused*.** A refusal is the `ownership-denied` record below, which #467 added; an
alert on *this* event therefore counts approved exceptions rather than attempts, and
the one below is what tells "nobody tried" from "many were refused".

```json
{"time":"2026-08-19T18:00:00+00:00","event":"ownership-override","system":"sys-a","lpar":"db-01","host":"hmc-a.example","attribution":{"claim":"agent-7","source":"config:agent_id","verified":false}}
```

`host` is the hostname or address of the HMC the override was approved on — the
same `HMCConfig.host` whose `agent_id` the attribution records. It names a
machine, not a grant, so it is its own field rather than an arm of the
authorization record's `connection` object. An unset `HMC_HOST` renders as an
empty string.

For a system-wide profile restore, `lpar` is `"*"`: the approved bypass covers every
current profile and any backup-only definition, not one named partition. Other override
records continue to name the individual partition whose ownership check was bypassed.

It carries no `policy`, `decision`, `reason`, or `targets`, and not as nulls —
an ownership check on a token parsed from an LPAR description is not an
access-policy decision, and empty fields would read as one.

### `event: "ownership-denied"`

Emitted when an [ADR 0011](adr/0011-multi-agent-lpar-ownership.md) LPAR ownership
check *refuses*, immediately before the `PermissionError` is raised. Always
`WARNING`, so `--audit-level WARNING` — the setting that drops permits — keeps
these. The decision is [ADR 0100](adr/0100-ownership-denial-audit-record.md).

| field | value |
|---|---|
| `time` | UTC, ISO 8601 |
| `event` | `"ownership-denied"` |
| `operation` | `lpar-decommission-snapshot`, `lpar-mutation`, or `lpar-profile-restore` |
| `denial` | `malformed-token` or `foreign-owner` |
| `system` | the managed system the partition lives on |
| `lpar` | the partition whose mutation was refused |
| `owner` | the owner the LPAR's token claims, or `null` |
| `host` | the HMC the check read the token from |
| `attribution` | `{"claim", "source", "verified"}` |

```json
{"time":"2026-08-26T18:00:00+00:00","event":"ownership-denied","operation":"lpar-mutation","denial":"foreign-owner","system":"sys-a","lpar":"db-01","owner":"agent-3","host":"hmc-a.example","attribution":{"claim":"agent-7","source":"config:agent_id","verified":false}}
```

`operation` names which guard entry point refused — `lpar-mutation` covers ordinary
single-partition mutations, `lpar-decommission-snapshot` the ownership read that
precedes a decommission, and `lpar-profile-restore` the current-partition checks before
a system-wide profile restore — and not which MCP tool or API function called it.
Per-tool granularity is deliberately out of scope; where the caller matters and the
transport is MCP, join on the `authorization` record for the same call.

`denial` names which of the guard's two rules refused. `foreign-owner` is a
well-formed token naming another agent, and the record carries *both* halves of the
comparison that failed: `owner` is the claimed owner, `attribution.claim` the agent
that was refused. `malformed-token` is a description carrying `[hmcpctl` that no
token could be parsed from, and there `owner` is `null` — nothing parsed, so the
record carries the actor alone.

A `malformed-token` record identifies the partition but not the malformation. Triage
means reading the description off the HMC out of band, and two alerts on one
permanently-broken token read the same as an ongoing incident. That is accepted
rather than closed: a description is unbounded operator-authored text that would be
cut at 128 characters — often before the malformation — and it can carry an ADR 0064
caller token beside the ownership one, so a field for it would disclose more than it
triages.

**A denial count is not a count of hostile attempts.** `docs/environment-variables.md`
prescribes retry-after-refusal as the sanctioned override procedure, so a legitimate
override is now *preceded* by a denial record carrying the same `system`, `lpar`, and
`attribution.claim` as the `ownership-override` record seconds later. There is no
correlation identifier, so pairing them means matching those three fields inside a
time window — and an override that was never refused (`provision_lpar`'s activation
leg, or any caller who passes `ownership_override` on the first attempt) will pair
against an unrelated earlier denial if one is in the window. A refused `--dry-run`
decommission preflight — the sequence the [CLI guide](cli.md) prescribes — emits the same record
as a refused destructive one, because the inventory read authorizes ahead of the
dry-run return. No field distinguishes either case; this is the same caveat the
override record above carries for its own two sources.

**Silence here is not proof of no refusal**, for one specific reason worth writing
down: with `HMC_AUTHORIZE_POWER_OPERATIONS` off — the default — the power path never
runs the guard, so no denial is possible there and none is recorded.

A denied caller can drive these at attempt rate, bounded in practice by the HMC round
trip each denial costs. Under `hmcpctl serve` they land on the bounded sink, which
drops and says so with a `records-dropped` count. The CLI's coroutine boundary
uses a scoped synchronous fallback handler when logging is otherwise unconfigured;
Python API callers retain `logging.lastResort`. Both routes have no queue bound
or drop count. The CLI preserves JSON for these denial/ownership events. Only
TLS-disabled events receive a compact terminal display, retaining their event
identity, time, host and source; redirected stderr keeps the original JSON.
Configured handlers and custom `logging.lastResort` handlers remain unchanged.

The denial record names its `operation` and the override record does not. Closing
that asymmetry would mean adding a field to the override record, which #467
deliberately left alone; the denial stream is complete without it.

It carries no `policy`, `decision`, `reason`, `targets`, or `connection`, and not as
nulls, for the reason the override record gives.

### `event: "install-attempted"`

Emitted immediately **before** `install_vios` submits a detached
`installios` to the HMC. Always `WARNING`, so `--audit-level WARNING` keeps it. The
decision is [ADR 0102](adr/0102-install-submission-audit-record.md).

```json
{"time":"2026-08-26T18:00:00+00:00","event":"install-attempted","system":"sys-a","partition":"vios-01","log_path":"/tmp/hmcpctl-installios-vios-01.log","host":"hmc-a.example","attribution":{"claim":"agent-7","source":"config:agent_id","verified":false}}
```

`system` and `partition` are the resolved HMC CLI names the submission was composed
with; `host` is the `HMCConfig.host` of the client that submitted, and an unset
`HMC_HOST` renders as an empty string. `attribution.claim` is the acting agent —
`HMC_AGENT_ID`, or `hmcpctl` when unset — the same claim the ownership records carry,
so an unconfigured deployment's records name one actor and can be joined.

`log_path` is the HMC-side path the install writes to, and the field to read when a
submission raises. It is `/tmp/hmcpctl-installios-<slug>.log`, where `<slug>` is the
partition name with every character outside `[A-Za-z0-9._-]` replaced by `_`
([ADR 0070](adr/0070-installios-cli-bridge-for-install-tools.md)). Two things follow, and
both are why `system` and `host` sit beside it here. The managed system is not part of the
path, so same-named partitions on different managed systems behind one HMC share one file;
and the slug collapses distinct names, so `vios 01` and `vios_01` do too. The redirect
truncates, so each colliding install destroys the other's only diagnostic record — and the
directory is `/tmp`, which a reboot or a tmpfiles sweep may clear.

`log_path` takes the shared 128-character bound like every other value, with no marker.
The template's fixed part is 28 characters and nothing bounds a partition name, so a long
enough one yields a path the bound then cuts — leaving a value that does not exist and
looks well-formed. Whether the record still tells you the real path depends on where the
name falls: `partition` takes the same bound, so a name past 128 characters is cut too and
the path is gone from both fields, while a name of 101 to 128 characters leaves `partition`
whole and the path recoverable as `/tmp/hmcpctl-installios-<partition>.log` — after applying
the slug substitution yourself. Such a name is one `installios` would refuse anyway, but the
record is written before the submit, so it exists.

**This record names an attempt, never an outcome.** The submission is detached, so
there is no HMC job to poll and nothing observes whether `installios` accepted the
target. A record means the process reached the point of submitting against that
partition's disks; whether anything started is in the log it names. It is emitted
ahead of the submit deliberately, because a submission that raises cannot tell a
resolution failure from a failed submit — the case where an operator most needs the
partition and the path.

**It is the only record naming the resolved partition and its log path.** There is no HMC
job ([ADR 0069](adr/0069-installlpar-and-installvios-absent-from-hmc-rest.md)), and no
[ADR 0011](adr/0011-multi-agent-lpar-ownership.md) ownership check and so no
`ownership-denied` or `ownership-override`. A served deployment does write an
`authorization` permit for the tool call, and `hmc_install_vios` declares a partition and a
managed-system selector — so that permit's `targets` already
carry both, and at the default audit level the streams can be joined on them. What the
permit cannot give you: it records the selector the caller passed, not the resolved name, so
a UUID selector never names the partition; it has no `log_path`; and `--audit-level WARNING`
drops it, because it is a permit. An `hmcpctl.api` consumer gets no `authorization` record
at all.

**Absence of this record is not proof that no install was submitted**, for the reasons the
lead section gives generally and these, which apply here specifically. Under `hmcpctl serve`
it lands on the bounded sink, which drops under load and reports only a `records-dropped`
count — a number, not an identity, so a reader cannot tell whether a dropped line was an
install; `--audit-level ERROR` or `CRITICAL` silences the reserved logger outright; and a
record that fails to build or write is swallowed rather than failing the call, because a
diagnostic must not abort an operation. Off the serve path the reserved logger is left at
`NOTSET`, and level resolution walks the parent chain whatever `propagate` says — so an
embedder that quiets the package the ordinary way, `logging.getLogger("hmcpctl").setLevel`,
suppresses the record before `logging.lastResort` is ever consulted. Alert on the records
you have, not on their absence.

It carries no `policy`, `decision`, `reason`, `targets`, or `connection`, and not as
nulls: the record is not an access-policy decision, and it is also emitted on the Python
API path, where no policy connection exists to name.

### `event: "install-submitted"`

Emitted immediately after the HMC returns the PID for a detached `installios` process.
Always `WARNING`, so the PID remains visible with only `install_audit_sink` configured and
when the served audit threshold is `WARNING`. The decision is
[ADR 0109](adr/0109-install-submitted-audit-correlation.md).

```json
{"time":"2026-08-29T18:00:01+00:00","event":"install-submitted","system":"sys-a","partition":"vios-01","pid":4321,"log_path":"/tmp/hmcpctl-installios-vios-01.log","host":"hmc-a.example","attribution":{"claim":"agent-7","source":"config:agent_id","verified":false}}
```

`pid` is the remote HMC process identifier needed to abort the detached install. The other
fields have the meanings and shared 128-character string bounds documented for
`install-attempted`. Correlate the records on `system`, `partition`, `log_path`, `host`, and
`attribution`, using stream order when identical submissions overlap. The stream does not
promise one-to-one pairing or an attempt identifier; each success record is independently
actionable because it carries its own PID and target.

A raised submit emits only `install-attempted`, never `install-submitted`, because the SSH
failure cannot prove whether a process started. Absence of this success record therefore
does not prove nothing started. Sink drops and audit filtering also mean absence of either
record is not proof that no submission occurred.

It carries no `policy`, `decision`, `reason`, `targets`, or `connection`, and not as nulls.

### `event: "console-write"`

Emitted immediately **before** a `WritableConsoleSession` queues bytes for a partition console,
one record per write. It is always `WARNING`. No MCP tool or CLI command writes to a console, so
only a Python API consumer produces this record, and no access policy gates it. The decision is
[ADR 0176](adr/0176-console-input-and-exclusive-raw-mode.md).

```json
{"time":"2026-09-23T18:00:00+00:00","event":"console-write","system":"sys-a","lpar":"lp-01","host":"hmc-a.example","mode":"shared","input_kind":"sysrq","length":2,"attribution":{"claim":"agent-7","source":"config:agent_id","verified":false}}
```

`system` and `lpar` are the HMC CLI names the session was opened with, and `host` is the
`HMCConfig.host` of its client. `mode` is `"shared"` for a write made while the collector keeps
reading and `"exclusive"` for a write inside raw mode. `input_kind` is `"raw"` for caller bytes
and `"sysrq"` for a framed SysRq request. `length` is the byte count. **The written bytes are
never recorded**, not even a SysRq key, because console input can hold credentials.
`attribution.claim` is `HMC_AGENT_ID`, or `hmcpctl` when that is unset, as it is for the install
records.

The record is emitted before the write, so a write that then fails is still recorded. The record
proves an attempt, not delivery.

It carries no `policy`, `decision`, `reason`, `targets`, or `connection`, and not as nulls.

### `event: "lpar-power-off"`

Emitted immediately **before** a PowerOff job is submitted for a logical partition, one record
per submission, whichever entry point reached it: `hmc_power_off_lpar`,
`hmc_dump_restart_lpar`, `hmcpctl lpars power-off`, the Python API's `power_lpar`, and the decommission workflow's own
power-off (`hmc_decommission_lpar`, `hmcpctl lpars decommission`, `decommission_lpar`). Always
`WARNING`. No access policy gates it. The decision is
[ADR 0180](adr/0180-lpar-power-off-audit-record.md).

```json
{"time":"2026-09-28T18:00:00+00:00","event":"lpar-power-off","lpar":"11111111-1111-1111-1111-111111111111","host":"hmc-a.example","operation":"dumprestart","immediate":false,"restart":true,"attribution":{"claim":"agent-7","source":"config:agent_id","verified":false}}
```

`operation`, `immediate` and `restart` are the values the submitted job document carries.
`operation` is `shutdown`, `osshutdown` or `dumprestart`; `dumprestart` crashes the partition
and takes a platform dump, so this is the field to filter on to tell a forced crash from a
graceful stop. `restart: true` means the partition is started again after it stops. `lpar` is
the resolved partition UUID the job was submitted against, whatever selector the caller passed,
and `host` is the `HMCConfig.host` of the client. `attribution.claim` is `HMC_AGENT_ID`, or
`hmcpctl` when that is unset, as it is for the install records.

The record is written after argument validation and after the
[ADR 0011](adr/0011-multi-agent-lpar-ownership.md) ownership guard, so a refused call — an
operation outside the vocabulary, a `dumprestart` without `allow_dump_restart`, a guard
refusal — writes no `lpar-power-off` record. It is written before the submit, so a submit that
then fails is still recorded: the record proves an attempt, not that the partition stopped.
The decommission workflow's power-off always sends `operation: "shutdown"` and `restart: false`,
with the `immediate` the caller chose; a partition that is already `not activated` is sent no
PowerOff and writes no record, and neither does a dry run.

It carries no `policy`, `decision`, `reason`, `targets`, or `connection`, and not as nulls.

### `event: "power-ownership-guard"`

Emitted once at `serve` startup for every connection the selected access policy can route.
Always `WARNING`, so the shipped audit threshold retains both the enforced (`true`) and
fail-open (`false`) states even when the policy withholds `hmc_effective_permissions`.

```json
{"time":"2026-08-28T18:00:00+00:00","event":"power-ownership-guard","connection":"lab","authorize_power_operations":true,"source":"environment","detail":null}
```

`connection`, `authorize_power_operations`, `source`, and `detail` are the same closed
per-connection values reported by `hmc_effective_permissions`. An unreadable connection emits
`authorize_power_operations: null`, `source: "unresolved"`, and a bounded exception-class
`detail`; configuration paths, values, and exception messages are not recorded. Resolution and
audit delivery failures do not prevent startup.

On stdio, the launching MCP host owns stderr and can read these records. Withholding
`hmc_effective_permissions` therefore withholds this inventory from MCP protocol calls, not from
the operator-trusted launcher. Use an HTTP deployment with operator-controlled server logs when
the MCP client must not own the audit descriptor.

<!-- The `source` values below are read by tests/test_authorization_audit_doc.py and held
     to `client.VERIFY_SSL_SOURCES`. Keep them a comma-and-`or` run introduced by the
     words "where the effective setting came from"; that clause is the anchor. -->
### `event: "tls-verification-disabled"`

Emitted when an `HMCClient` is constructed with `verify_ssl` off, so the audit stream
can answer "were credentials ever sent over an unverified channel, and to which HMC".
Always `WARNING`. The logon-time `warnings.warn` stays and is the CLI user's channel;
under the default warning filter it renders once per process per location, which is
why this record exists alongside it.

```json
{"time":"2026-08-22T18:00:00+00:00","event":"tls-verification-disabled","host":"hmc-a.example","source":"environment:HMC_VERIFY_SSL"}
```

`host` is `HMCConfig.host`, the HMC the unverified session would reach; an unset
`HMC_HOST` renders as an empty string. `source` names where the effective setting came
from — `explicit-argument`, `environment:HMC_VERIFY_SSL`, or `field-default` — because
that is which knob an operator has to turn.

**One record per client construction**, not per request — which would flood the sink —
and not per process, which would miss a later client built with different settings.
Read the rate accordingly: an MCP tool invocation builds a fresh client, so on a server
left at the insecure default (`HMC_VERIFY_SSL` is `false` until 1.0, per
`docs/environment-variables.md`) this stream carries roughly one record per tool call
rather than one at startup. Deduplicate on `host` and `source` if you are alerting.

It carries no `policy`, `decision`, `reason`, `targets`, or `attribution`. Building a
client is not an access-policy decision, and it happens on the CLI and Python API paths
too, where there is no policy to name.

### `event: "records-dropped"`

Emitted by the sink itself, not by a decision, when lines were lost since the previous
such marker. It appears immediately **before** the next line that lands, so it reads as
"N lines are missing above this point".

```json
{"time":"2026-08-19T22:14:03.881271+00:00","event":"records-dropped","count":37}
```

It carries no `policy`, `attribution`, or anything a caller supplied — it describes the
sink's queue. Because it comes from the sink rather than the `hmcpctl.audit` logger, it is
not affected by the level you set on that logger, and it is not produced at all when you
attach your own handler: your handler has no such queue. See
[ADR 0043](adr/0043-non-blocking-stderr-diagnostics.md).

## Reason codes

| code | decision | meaning |
|---|---|---|
| `permitted` | allow | one grant covered the tool, connection, and targets together |
| `configuration-unreadable` | deny | the configured HMC connections could not be read at all — **your** configuration, not the caller's input |
| `connection-not-granted` | deny | no grant naming the tool allows the selected connection |
| `target-selector-unreadable` | deny | a declared selector carried a value the boundary declines to read |
| `target-unboundable` | deny | the tool's selectors cannot bound it, so no `targets` table can |
| `target-selector-absent` | deny | a declared selector was omitted from the call |
| `target-not-granted` | deny | no grant allowed that combination of targets |

There is deliberately no `connection-selector-unreadable`: `reason` names the
*decision* and `state` names the *input*, and the connection dimension has one denial
template on purpose. To find malformed calls, filter
`connection.state == "unreadable"`.

## Attribution is never identity

Not every record carries `attribution`; each section above says whether its own does.
Where it appears, `verified` is always `false`, and neither value influences an
authorization decision at the dispatch boundary. Where the claim comes from differs, and
the `source` field is what tells you which you are reading.

**`source: "environment:HMC_AGENT_ID"`** — on the `authorization` record. Read straight
from the server process's environment at emission.

**`source: "config:agent_id"`** — on the `ownership-override` and `ownership-denied`
records. This is `HMCConfig.agent_id`, the effective value the ADR 0011 check compared,
and it differs from the other in three ways worth knowing: it may come from a
`config.toml` profile rather than the environment, it *is* validated by
`validate_agent_id`, and it renders the literal `hmcpctl` when no identity is configured
at all rather than `null`. The last of those is what lets an unconfigured deployment's
ownership records be joined on the actor: the guard compares that literal, so the record
names it too.

Two things follow for the `authorization` record that are easy to get wrong:

- **It identifies the process, not the caller.** Under stdio one process serves one
  client and the two coincide. Under streamable HTTP one process serves many clients,
  so every record carries the same claim and the field tells you nothing per-caller.
- **It is the raw environment value, unvalidated.** This applies to the `authorization`
  record only — the ownership records' claims go through `validate_agent_id`.
  `docs/environment-variables.md` documents
  `HMC_AGENT_ID` as 1–64 printable ASCII with a forbidden-character set; that is the
  rule for *configuration*, and this record deliberately bypasses it so a malformed
  value is still reportable. A recorded claim may therefore be wider and stranger than
  that contract allows — bounded at 128 characters and JSON-escaped, but not validated.

ADR 0011's ownership protocol *does* take a real decision from the same value. That is
a separate mechanism, and `verified: false` describes this record's provenance rather
than the value's authority everywhere.

## Routing, levels, and silencing

Records go to the `hmcpctl.audit` logger. A permit is `INFO`; everything else the
logger emits is `WARNING`.

Importing `hmcpctl.audit` does not change logging state: `propagate` stays `True`
until `install_audit_sink()` runs, and only `hmcpctl serve` calls it. On the serve path
it sets `propagate = False`, so no ancestor handler receives audit records, and
attaches a handler writing to **stderr**. An embedder that composes an application with
`create_mcp` and serves it itself never reaches the installer, so audit records still
propagate to the root logger's handlers; attach your own handler to `hmcpctl.audit` and
set `propagate = False` on it, or keep stdout-bound handlers off the root logger. With
neither a handler nor propagation, a `WARNING`
record still reaches `logging.lastResort` on stderr. The CLI coroutine boundary
uses a scoped synchronous fallback instead, with compact TLS-event presentation
on actual terminal stderr and unchanged JSON on redirected stderr. To route them
elsewhere, attach your own
handler to `hmcpctl.audit` **before** calling `main_stdio` / `main_http` — the server
defers to a handler that is already there and will not add a second.

> The non-blocking guarantee is the **shipped sink's**, not the logger's. Your handler is
> called on the dispatch path, synchronously, so if it can block then so can the call.
> `logging.lastResort` and the CLI fallback handler are synchronous
> for the same reason.

> To set the level from the command line, pass `--audit-level LEVEL` to `hmcpctl serve`:
> `DEBUG` and `INFO` keep everything the logger emits, `WARNING` drops permits and keeps
> the rest, and `ERROR` or `CRITICAL` silences the stream. Read `WARNING` as a volume floor
> rather than a quiet
> setting: on a server left at the insecure `HMC_VERIFY_SSL` default it still carries one
> TLS record per tool call, which the deduplication advice above applies to. The
> `records-dropped` marker survives every setting, since it comes from the sink rather
> than the logger. The name is validated — a misspelling is a usage error
> that starts nothing. Omitted, the shipped sink's own `INFO` default stands. An in-process
> caller keeps configuring the logger directly, before calling `main_stdio` / `main_http`.

Three things to know if you consume this stream:

- **Do not put your handler on `sys.stdout` under the stdio transport.** That stream
  carries JSON-RPC framing, and one audit record on it corrupts the protocol.
- **A launcher that merges the descriptors does the same thing.** `serve 2>&1`, or a
  unit file or wrapper doing it, makes stderr the JSON-RPC channel. Nothing inside the
  process can detect that. The same caveat applies to the startup warnings.
- **Skip a line that does not parse rather than failing on it.** `hmcpctl.audit` is
  reserved for these records and that is checked inside this package, but a dependency
  or your own code can still log there, and other writers share stderr. Since
  [ADR 0051](adr/0051-fastmcp-logging-through-the-bounded-sink.md) FastMCP's own records
  arrive on the same queue as these — one concise line for a denial, a plain traceback for
  a genuine handler bug — and its startup banner is written straight to the stream by
  `rich` before serving begins. Since #534 this package's own non-audit diagnostics arrive
  there too, each physical line prefixed `hmcpctl: `, which is a marker chosen so it cannot
  begin a JSON object. `hmcpctl` is a second attachment point on the same terms as
  `hmcpctl.audit` above — attach a handler to it and the server leaves yours in place instead
  of installing its own — with one difference, and it is the difference that matters here:
  `propagate` is left alone on `hmcpctl`, where `hmcpctl.audit` clears it. So a handler you
  already have *above* `hmcpctl` keeps receiving these records after a serve, and if it writes
  to stderr it puts a **second, unmarked and unescaped** copy of each on this stream. That copy
  is the one that can carry a newline out of an interpolated value and place text at column 0.
  It is not new — the same record reached the same handler the same way before #534 — but the
  marker is a property of this package's own handler, not of the stream, and a consumer
  hardening on it must know that. Do not attach a stderr handler above `hmcpctl` in a
  deployment whose stderr is parsed. One writer is unmarked with no operator handler
  involved: `warnings.warn`, which this package uses for the logon-time
  `TLSVerificationDisabledWarning` while TLS verification is off, and which `logging` never
  sees, so it reaches neither the marker nor the queue. Under default warning filters that is
  up to two unmarked lines at column 0, once per HMC host and `verify_ssl` source per process.
  Its text is fixed and carries no HMC-sourced value.

## What this is not

Records are written to a process logging sink. They are not persisted, sequenced, or
signed, and retention and export are yours. A record is written *before* the denial is
raised and *before* a permitted handler runs, so a permitted call is recorded as
**authorized**, not as **succeeded**.

**Server-sink records are droppable, and their delivery is asynchronous.** Since
[ADR 0043](adr/0043-non-blocking-stderr-diagnostics.md) a record is handed to a bounded
in-memory queue (1024 lines) drained by one background thread, so nothing on the dispatch
path waits on the destination. A destination that is absent, broken, closed, or simply not
being read costs records rather than costing the server: the queue fills and further lines
are dropped.

A drop is never silent. The count is carried in-band, ahead of the next line that lands:

```json
{"time": "2026-08-19T22:14:03.881271+00:00", "event": "records-dropped", "count": 37}
```

Read it as *37 items are missing above this point*. Two limits worth knowing: `count` is
items rather than records, because the startup warnings and — since ADR 0051 — FastMCP's own
records share this queue, so a non-zero count does not mean audit records were lost; and reporting
needs a destination that accepts a write again, so a stream that never recovers — or a
process killed with `SIGKILL` — still loses without a marker. An empty stream is therefore
still not evidence of an idle server. A record may also reach stderr slightly after the
tool result reaches the client; order among records is preserved.

**The record is readable by the party it is about, and it says more than the denial does.**
Under stdio the MCP client owns the server's stderr, so a client can read the records describing
its own calls. That matters for one field: `connection.resolved` carries the profile key a token
resolved to, or `<unresolved>` when it named nothing configured. The denial *message* withholds
that distinction on purpose — ADR 0038 makes an unresolvable token and a withheld one deny
identically, so a caller cannot use denials to test membership of `config.toml` — and the record
does not. A caller reading the stream therefore learns which of its guesses name configured
profiles, and a correctly-guessed nickname yields the profile key it targets. The disclosure is
names only, and `hmc_list_configured_hosts` offers a client more; if your profile inventory is
sensitive, withhold that tool by policy and route this stream somewhere the client cannot read.

Who has to do that depends on the transport, and under stdio it is not you. The MCP
client spawns the server and owns fd 2, so whether the stream is read binds the client
rather than the operator deploying it — choose one that reads its child's stderr. Under
`--http` it is whatever supervisor or journal collects the unit's stderr. Since ADR 0041
made a policy mandatory this applies to every deployment, and an ungranted caller can drive
the writes at call rate, because the record precedes the denial. Since ADR 0051 a denied call
puts *two* items on the queue — this record, then FastMCP's one-line denial — so the queue
fills in about half the calls it used to. Size the destination for the permitted path too:
on the insecure `HMC_VERIFY_SSL` default a permitted call also puts a TLS record on the
queue, per the rate noted with that record above. That caller can therefore make records
drop — bounded to the queue, visible as a `records-dropped` count, and never able to stall
a call.
`hmcpctl serve --audit-level WARNING` halves what that caller can produce — permits are gone —
but the denials themselves stay, because an unrecorded probe is worse than a recorded one.
