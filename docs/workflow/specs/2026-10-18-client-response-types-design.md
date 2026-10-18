# Preserve client response typing

## Problem
Four internal Protocol request methods erase httpx.Response to Any despite
matching concrete implementations and neighboring protocols returning Response.
S denominator100; trivial annotation-only correction with unchanged runtime behavior.

## Scope and success
C1: in client/client_contracts.py, change return annotations of LparsClient and
UpdatesClient _request_with_uuid_path_arguments, StorageClient _request and
StorageClient _request_with_uuid_path_arguments to existing imported httpx.Response.
Do not change **kwargs Any, JSON payload boundaries, other protocol signatures,
concrete runtime methods, dependencies or caller policy. No ownership transition.
User-authorized precise-typing cleanup, no new interface behavior or architecture.

## Validation
C1 Mode: focused-test via private temporary static type probe using typing.assert_type
on the awaited four protocol calls, requiring httpx.Response. Existing Any annotations
must fail the installed ty check on the probe; corrected ones pass. Remove private
probe after evidence, do not commit mirror annotation tests. AST comparison with
annotations removed confirms no runtime statement/signature-default change.
Run just typecheck and uv run --no-sync pytest tests/unit/test_client_domain_mixins.py
tests/unit/test_transport_contracts.py -q --no-cov plus affected updates/storage tests
if type errors or discovered contract changes justify. Run lint/format/all14 hooks.
Conventional commit, no new tests mirroring implementation, no dependencies/live.
