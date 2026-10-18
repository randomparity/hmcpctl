# Retain safe snapshot failure diagnostics

Charter: interactive; owner root; quality finding in best-effort template snapshot
logging. S100/full-spec (diagnostic privacy boundary). Existing body privacy
test is normative: raw HMC body must never enter warning logs.

Keep deployment, ownership-stamping refusal and returned warning strings intact.
Enrich both baseline and post-snapshot warning records with exception class and
chained cause class, alongside existing phase/system/status. Those closed source
type names distinguish transport timeouts, connection failures and parse causes
without logging str(exc), exc_info, traceback text, bodies or credentials.
Do not add generic logging/redaction infrastructure or modify HMCError storage.

Failure model: absent HTTP status remains distinguishable by exception/cause type;
no cause is a deliberate None value. Best-effort snapshot failure never masks
deployment success or invokes ownership stamping without snapshots.
Verification: extend actual waited-deploy baseline/post failure cases with
transport/timeout and parse cause types, sensitive messages/body absent, no
stamping and unchanged result. New diagnostic expectations must fail before
implementation. Focused template suites plus lint/types/format, one commit.
