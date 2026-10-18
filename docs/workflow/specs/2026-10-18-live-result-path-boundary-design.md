# Literal Git path arguments for private live results

## Problem
The live runner checks operator-selected result paths with git check-ignore.
Relative paths beginning with a dash are interpreted as Git options. Git usage
errors return a code other than 1, which the existing outside-repository policy
considers safe. Both result and observation paths can therefore bypass the
unignored-output startup refusal before credentials or hardware work begins.

## Scope
Root owns a bounded argument-boundary repair under the quality task. Add the Git
option terminator before the result path in _destination_is_ignored. Keep CLI
signatures, accepted filename syntax, Git-based ignore evaluation, messages,
outside-repository policy and file-write protections unchanged. Do not reject
all dash-prefixed filenames: genuinely ignored ones remain usable. The capture
helper already normalizes its path to an absolute argument and is not affected.
No shell, data model, dependency, architecture or hardware behavior changes.

## Validation
Use the actual installed Git and temporary test repositories. Before the fix,
prove dash-prefixed unignored result/observation paths reach the credential
boundary. Afterward require startup refusal before that boundary, no output
files, and normal acceptance when the same literal paths are explicitly ignored.
Use argument forms that cannot invoke a pager or wait for stdin. Run the runner
tests, lint, format and type checks, commit hooks, then final verification. No
real HMC operation or public write is authorized by this repair.
