# Private release-window orchestration diagnostics

## Problem
Execution.run converts operating-system and wait-timeout failures to code 125 but
loses their cause. The phase log may be absent or empty, making the current
instruction to inspect that output insufficient. The operator approved a bounded
orchestration-error diagnostic in private phase records.

## Scope
Track the concrete operation being attempted: log open, child spawn, selector
setup/wait, child output read, log write/close, or child wait. On the existing
OSError/TimeoutExpired catch, retain operation, exception type and detail in one
orchestration_error string of at most 1024 characters in the phase record; success
carries None. Keep synthetic exit 125, signal escalation, reaping, stdout closure,
safe flag and recovery dispatch unchanged. Never depend on writing the phase log
to retain this diagnostic. Generic capture failure names both the private phase
record and output; raw exception details never enter public console messages.
No subprocess arguments or target data are added to public output. Private
summary writing retains its existing filesystem/error policy. Root owns scope.

## Success
Log-open, spawn, read/write and wait failures remain distinguishable even when
no useful log exists. Diagnostic size is bounded; cleanup and recovery still run.

## Failure model
Actors: operator-run process orchestration and child processes; private output
may be unwritable, a process may refuse spawning, and output/waits may fail.
Assets: retained cause, bounded private evidence and no unreaped child. Accepted
change: private phase JSON gains orchestration_error. No hardware is invoked.

## Validation
focused-test: fault-inject each OS boundary and wait timeout, asserting context,
exception type, bounded detail, exit 125 and cleanup. Retain the existing spawn
failure/recovery, interrupted-child, truncation and descendant-output tests.
Prove new diagnostics red before implementation. Run release-window offline tests,
lint, format and type checking; private errors must not appear in public output.
