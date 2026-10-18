# Name PowerOn's activated-state outcome accurately
Full-spec S denominator100; explicituserapproval scope4 for outputrename.

## Scope and success
C1: rename LparPowerOnOutcome.already_running and internalalready_running marker
to already_activated, migrate exactfield consumers/CLI/MCP/live lparpower/tests.
No compatibilityoutputalias. Preserve otherfields/defaults/returntypes and rawjob.
C2: ACTIVATED_STATES remains running/starting/open firmware; no force/wait/boot/
profile/affinity/ownership/state policy change. Existingmessages/case labels need
not change when they describe a concrete running case rather than fieldname.
C3: actualFastMCPschema/wire/CLI JSON expose newkey, oldkey absent. Regenerate
currentdocs with justtool-docs; historicalADR/spec prose stayshistorical.
Durable power.py already_in_state and journalfields are distinct and unchanged;
its start effect submits directly, never persisting this ephemeral marker.

## Failure model
Actors: operation andCLI/MCP/livescenario callers for running/starting/openfirmware,
inactive partition, force andwait/affinity branches. Assets: correctmarkermeaning,
no duplicateactivation, unchanged guards/options/data/journal/authbehavior.
Accepted classes: approvedpre-releaseboolean-keywirebreak, repositorycallers
migrateddirectly andoldexternalcallersmustmigrate. Nopersisteddatafieldrenames.
Otherowners: activatedstate policy/enginejournal/JobOutcome remain existing.

## Validation
Focused-test redoldfield: running/starting/openfirmware produce newboolTrue/nojob;
submittedfalse retainsjob, actualMCPschema/callserialization requiresnewkey only.
Preserve existingforce/wait/affinity/ownership tests, updatebindingsfieldonly;
existinglivepowercasesretainstrictactivation/bootmode/noduplicatejobassertions.
Controlledoldkeyrestoration failswire/consumer tests, restore. Audit occurrences
individually: oldhistoricalprose not runtimealiases. Run power/lifecycle/schema/
CLI andlivepower/barecec tests --no-cov; lint/format/types/generatedguards/14hooks.
Rootfinalfullverify; no livehardware/dependencies/auth/persistedformat change.
