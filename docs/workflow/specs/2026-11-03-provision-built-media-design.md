# Built installer media in `hmc_provision_lpar`

**Issue:** #1843 (piece 2 of #1227, epic #1215) · **Decisions:** ADR 0191 (D3, D4),
[ADR 0324](../../adr/0324-built-install-media-runs-one-configured-build-entry.md), ADR 0319 ·
**Builds on:** [prepared media](2026-11-03-provision-prepared-media-design.md) (#1842) ·
**Branch:** `feat/built-install-media-1843` · **Guardrails:** `just verify`;
`uv run --no-sync prek run --all-files`

## Problem

`install.media.mode: built` is refused (`provision.py` `validate_provision_install`, naming
#1843). No configuration names a build entry. Nothing runs it, writes its request, or parses its
result.

## Scope

In scope: provision accepts built media with `boot: deferred`. It runs the configured build entry
once the adapter's actual MAC is recorded, binds the result, and continues on the prepared-mode
path to `ready_to_boot`. Out of scope, with owners:

- power-on and boot → #1849/#1230;
- the CLI `install` option → orchestrator follow-up (operator D2);
- media repository create or delete → not authorized;
- a live installation, a power operation or a release → not authorized.

**Components.**

- **`config.py`**: `iso_build_entry: str` and `iso_build_arguments: str`, a JSON array (ADR 0324
  D1), validated when the config is built by `parse_build_arguments`, and
  `BUILD_TARGET = "{target}"`. The property
  `iso_build_command -> tuple[str, ...] | None` returns the entry followed by the template, or
  `None` when unset.
- **`operations/lpar/media_build.py` (new)**:
  - `run_build_entry(command, request, *, timeout=BUILD_TIMEOUT_SECONDS) -> bytes`, with ADR 0324
    D2–D3;
  - `BuildFailed(Exception)`, whose message names the cause (request file, spawn, exit status,
    timeout, output bound) and never the output. It is raised only once no child was started or
    the child has been reaped;
  - `child_environment() -> dict[str, str]`.
- **`operations/lpar/install_media.py`**:
  - `check_built_name(name, install)` refuses a built install whose name is not an `lpar`
    identifier (ADR 0324 D6);
  - `target_request(install, name, mac, operation_id) -> bytes` returns canonical JSON of
    `{format: "iso-chain-target-v1", profile, lpar, mac, network, operation_binding,
    ssh_authorized_keys, login_user}`;
  - `parse_result(output) -> dict`: one object, duplicate keys refused, at most 64 KiB. Any
    decoding error, `RecursionError` included, is a `MediaMismatch`;
  - `bind_built(install, result, mac, operation_id, allowlist) -> MediaBinding`. It checks the
    URL's type, scheme, length and allowlist itself, so the build identity never records an
    unlisted URL. The upload's own allowlist check stays;
  - `bind_prepared` and `bind_built` share one field check, `_check_result`.
- **`operations/lpar/provision.py`**:
  - `validate_provision_install` drops the built refusal and calls `check_built_name`. `check_mac`
    already refuses `adapters.mac` with built media.
  - `preflight_install`, for built media, requires `iso_build_command`, an entry that is an
    executable regular file, and a non-empty `HMC_ISO_URL_ALLOWLIST`. Each refusal names the
    variable.
  - `_provision_step_names` adds `build_media` before `bind_media` for built media.
- **`operations/lpar/provision_operation.py`**: effect `build` (*Body*), its classifier, and
  `EFFECT_KINDS["build"] = "media.iso_build"`.
- **`operations/lpar/plan.py`**:
  - `_check_install` calls `check_built_name`;
  - `_request_blockers` adds `build_entry_unconfigured`, with check `build_entry`, when the
    command is unset or the allowlist is empty. Executability is checked at preflight only;
  - the `bind_media` detail for built media reads "run the build entry, then bind its result".
- **`server_tools/lpar/provision.py`**: the docstring describes built media.
- **Docs:**
  - `docs/environment-variables.md` gains two rows;
  - `AGENTS.md`'s leak probe counts fifteen fields;
  - `tests/conftest.py` unsets both variables;
  - `just tool-docs`;
  - `maturity.json` moves `built-install-media` to implemented;
  - `CHANGELOG.md`.

**Body.** In `_Run._media`, the disk check runs first. For built media, effect `build` runs next:

1. It reads `iso_build_command` from the configuration in force. If the command is unset, it
   raises `EffectNotApplied` naming `HMC_ISO_BUILD_ENTRY`.
2. It requires the `network` effect's MAC.
3. It writes `target_request` and runs `run_build_entry`.
4. It parses and binds the result with `bind_built`.
5. Every `Exception` from steps 1–4 becomes `EffectNotApplied`, through `_leg(..., definite=True)`,
   so the run ends `failed`. The runner returns or raises only once its child is reaped. A
   `BaseException` such as cancellation still settles `uncertain`.
6. The identity is `{url, iso_sha256, iso_size, manifest_sha256, mac}`. `manifest_sha256` is taken
   from the result; `MediaBinding` is unchanged.

The binding comes from that identity. Prepared media binds with `bind_prepared` as today. The
`network` MAC check, step `bind_media`, upload, mount, persist and boot order are unchanged. A
`build` that does not apply skips `bind_media`, the media steps and the persist steps. The skip
lists add `build_media` only for built media, so a prepared result never lists it.

**Classifier.** `media.iso_build` → `needs_attention`, with the reason "the build entry's run was
interrupted and its result is unrecorded; hmcpctl does not run it twice: abandon, decommission the
partition, remove any ISO it published, then provision again".

## Failure model

1. **Actors and deployments:**
   - an MCP agent on a policy-bounded server with one ADR 0190 store;
   - the operator, who installs the build entry and sets its configuration;
   - the build entry (iso-chain-loader), a local program;
   - an HTTP origin on `HMC_ISO_URL_ALLOWLIST`.
2. **Invariants and assets:**
   - hmcpctl runs only the configured command, never a shell. A caller cannot name an executable or
     an argument; a caller's values reach the builder only inside the request file;
   - the server's `HMC_*` names never reach the builder;
   - media is uploaded only after its result binds this operation, its actual MAC and the request;
   - one recorded build per operation;
   - effect kind `media.iso_build` is a persisted contract (ADR 0195).
3. **Accepted:**
   - Orphan ISOs on the media host after a failed or interrupted build (ADR 0324 Consequences).
   - An interrupted build pauses for `abandon`; the partition is then decommissioned (ADR 0324
     Consequences).
   - A timeout kills only the process group. A containerized build may go on publishing.
   - An HMC session that expires during the build fails the upload. The run pauses, and a resume
     reuses the applied build.
   - stderr from the builder reaches the server's stderr log unfiltered. The log is the operator's.
   - The worker thread is a daemon. A server exit mid-build leaves the child running in its own
     session. The operator's process supervisor owns that.
   - The result's `manifest_sha256` is recorded, not re-derived. hmcpctl does not read the
     manifest.
4. **Covered elsewhere:**
   - upload, mount, readback, boot order and their classifiers → #1842 (ADR 0319);
   - power-on → #1849;
   - deleting media from the VIOS → #1848.

## Threat model

- **Boundaries added:**
  - subprocess execution of the configured command;
  - the builder's stdout, which is untrusted JSON;
  - the request file on local disk.
- **Boundary widened:** caller install fields reach the builder. These are `profile`, `network`,
  `ssh_authorized_keys`, `login_user` and the partition name.
- **Actors:**
  - an MCP client allowed to provision, who may send any install values;
  - a builder that misbehaves or is compromised, which is trusted to run but not to be believed;
  - a local user on the server host.
- **Controls:**
  - argv comes only from configuration. Caller values are JSON-encoded into a 0600 file in a 0700
    directory. Install fields keep their existing checks (`check_install`), and the name is
    checked against the identifier rule;
  - stdout is bounded at 64 KiB, has an exact key set, refuses duplicate keys, and every field is
    bound. The URL is allowlisted and at most 2048 characters, which keeps the identity under
    `store.MAX_IDENTITY_BYTES`. Refusals never echo values;
  - a timeout kills the process group;
  - an environment with no `HMC_*` names.
- **Out of scope:**
  - a malicious build entry — the operator installed it, and it runs with the server's authority;
  - a local user who can write the configuration or the store.

## Success

1. With built media and a configured entry, a clean run journals `build`, `upload`, `mount`,
   `profile`, `default_profile` and `boot_order` in that order and ends `paused`/`ready_to_boot`.
   The upload receives the result's URL, digest and size.
2. The entry receives exactly `[entry, *arguments]`, with `{target}` replaced by a 0600 file. That
   file holds the request with the adapter's actual MAC and the `operation_id`. The file is gone
   afterwards. The child's environment has no `HMC_*` name.
3. Each of these ends `failed` with no upload: a non-zero exit, a timeout, output over 64 KiB,
   invalid JSON, a duplicate key, an unknown or missing key, a wrong `operation_binding`, a wrong
   MAC, and a URL that is unlisted, non-http(s) or too long.
4. Each of these refuses before anything is recorded: built media with an unset or non-executable
   entry, with an empty allowlist, with `adapters.mac`, or with a non-identifier name. A request
   file that cannot be written, a result nested past the recursion limit, a non-string `url`, and
   a command unset by the time the build leg runs each end `failed`.
5. Config: a relative entry, an argument value that is not a JSON array of strings, more than 64
   elements, an element that is not printable or is over 4096 characters, zero or two `{target}` elements, or a field without its pair raises at construction.
6. The `media.iso_build` classifier returns `needs_attention`. A resume after an applied `build`
   does not run the entry again.
7. `hmc_plan_lpar` reports `build_entry_unconfigured` for an unset command or an empty allowlist.
   The dry-run and plan media order still match. A prepared run that stops early lists no
   `build_media`. Existing prepared and non-install tests pass unchanged.
8. `just tool-docs-check`, `just env-vars`, `just capability-inventory` and `just verify` are
   green.

## Validation

- `tests/lpar/test_media_build.py`: the runner against small executable scripts in `tmp_path`
  (Success 2, 3).
- `tests/lpar/test_install_media.py`: `parse_result`, `bind_built`, `target_request` and
  `check_built_name` (Success 3, 4).
- `tests/lpar/test_provision_install.py`: the built body through `engine.submit` with
  `run_build_entry` patched, the classifier, refusals and resume (Success 1, 3, 4, 6, 7).
- `tests/unit/test_config.py`: the build-entry fields (Success 5).
- `tests/unit/test_lpar_plan.py`: the blocker (Success 7).
