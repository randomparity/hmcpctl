# Shared private-result live entry

The runner already owns _validate_private_results and imports the existing
memory_statistics.private_destination validator. Keep that owner/path; avoid another module.
Add lpar-memory-move/ST54, lpar-processor-move/ST61 and lpar-accelerator-qos/ST64 to the two
existing private named groups lpar-paging-switchover/ST66 and lpar-memory-statistics/ST67.
The same _prepare_selection gate must refuse direct numbered dispatch of these tasks and
unsafe results before client/submission work, and before configuration on the CLI entry.
No task/group/argument changes.

Add one private runner _run_private_group(group, description, argv=None) for the five wrappers.
It parses only required --results-file, selects the fixed named group, calls existing common
admission, translates validation errors to parser.error/exit2, then saves/install077umask,
calls _run_from_arguments with the fixed group/path, and restores the savedmask in finally.
Each wrapper retains its main(argv) entry and delegates there. Remove local path/stat/mask
copies and imports. Keep unknown-option/missing-argument argparse errors and returned status.
The existing validator rejects noncanonical/nonabsolute paths, result and sidecar symlinks,
existing files, absent/exposed parents, and repository ancestors. Do not resolve aliases first.
This intentionally closes the weaker relative/symlink alias acceptance in three old copies.
The two established canonical-only wrappers keep their existing accepted-input set.

## Failure model
Actors are local operators and offline tests using wrappers, runner CLI or runner.main.
Assets are private result custody, pre-HMC refusal, fixed opt-in tasks and restored process mask.
Existing atomic evidence.write_results already guarantees0600 report and observation publication.
Concurrent hostile filesystem replacement after admission is not solved by this consolidation;
existing atomic writer and private0700 parent remain the boundary; no new transaction promised.
No hardware run occurs; actual arm mutation/admission/cleanup semantics remain their owners.

## Validation
focused-test: parameterized runner direct tasks54/61/64 refuse unnamed execution before Client;
new parameterized memory/processor wrapper dangling result and sidecar symlink refusals fail
on baseline; existing five wrapper tests retain forwarding, exit2 and077/restoration assertions.
Existing success/failure tests across all five wrappers exercise actual commonhelper by mocking
only _run_from_arguments (the downstream run); do not weaken those assertions after extraction.
Run five tests/scripts/test_live_lpar_* wrapper modules and tests/test_live_runner.py, plus
memory/processor/accelerator arm tests when selection gate affects their caller. Run static/hooks.
No wording snapshot for human-readable design; meaningful trust-boundary tests are mandatory.
