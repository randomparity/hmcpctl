"""End-to-end operation contracts over a stateful native transport boundary."""

import xml.etree.ElementTree as ET
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from lpar.test_partition_profiles import A, field, profile

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.partition_profile import N, ProfilePatch, compare_profiles
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.profiles import (
    delete_partition_profile,
    list_partition_profiles,
    read_partition_profile,
    save_current_partition_profile,
    set_default_partition_profile,
    update_partition_profile,
)

S = "22222222-2222-4222-8222-222222222222"
P = "11111111-1111-4111-8111-111111111111"
F = "33333333-3333-4333-8333-333333333333"
G = "44444444-4444-4444-8444-444444444444"


class NativeService:
    def __init__(self, kind="LogicalPartition"):
        self.kind = kind
        self.docs = {F: profile(), G: profile()}
        field(self.docs[F], "ProfileName").text = "source"
        field(self.docs[G], "ProfileName").text = "other"
        for key, root in self.docs.items():
            field(root, "Metadata/Atom/AtomID").text = key
            field(root, "AssociatedPartition").set(
                "href", f"https://hmc.test:443/rest/api/uom/{kind}/{P}"
            )
        self.etags = {F: "e1", G: "e1"}
        self.parent = {
            "PartitionName": "partition",
            "PartitionID": "1",
            "Description": "",
            "DefaultProfileName": "other",
            "LastActivatedProfile": "other",
            "CurrentProfileSync": "Disabled",
        }
        self.calls = []
        self.add_field = False
        self.post_status = 200
        self.job_error = False
        self.copy_drift = False
        self.readback_error = False
        self.wrote = False
        self.hmc = MagicMock(spec=HMCClient)
        self.hmc.config = HMCConfig.from_mapping({"host": "hmc.test", "user": "test"})
        self.hmc.list_system_summaries = AsyncMock(
            return_value=[{"UUID": S, "SystemName": "system"}]
        )
        self.hmc.get_managed_system = AsyncMock(
            return_value={"UUID": S, "Resource": {"SystemName": "system"}}
        )
        self.hmc.list_logical_partitions = AsyncMock(
            side_effect=lambda system: (
                [{"UUID": P, "Resource": deepcopy(self.parent)}]
                if self.kind == "LogicalPartition"
                else []
            )
        )
        self.hmc.list_vios = AsyncMock(
            side_effect=lambda system: (
                [{"UUID": P, "Resource": deepcopy(self.parent)}]
                if self.kind == "VirtualIOServer"
                else []
            )
        )
        self.hmc.get_uom = AsyncMock(
            side_effect=lambda kind, uuid, **kwargs: {
                "UUID": P,
                "Resource": deepcopy(self.parent),
            }
        )
        self.hmc.list_child = AsyncMock(
            side_effect=lambda *args: [
                {
                    "UUID": key,
                    "Resource": {"ProfileName": field(root, "ProfileName").text},
                }
                for key, root in self.docs.items()
            ]
        )
        self.hmc._request_with_uuid_path_arguments = AsyncMock(side_effect=self.request)
        self.hmc.submit_job = AsyncMock(side_effect=self.submit)
        self.hmc.wait_for_job_entry = AsyncMock(
            side_effect=lambda *a, **k: deepcopy(self.job)
        )
        self.hmc.get_console_info = AsyncMock(
            return_value={
                "Resource": {
                    "VersionInfo": {
                        "Version": "10",
                        "Release": "3",
                        "ServicePackName": "1060",
                    }
                }
            }
        )
        self.hmc.delete_child = AsyncMock(side_effect=self.delete)

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        key = path.rsplit("/", 1)[-1]
        if method == "GET":
            if self.readback_error and self.wrote:
                raise HMCError("readback unavailable")
            root = self.docs[key]
            raw = (
                f'<entry xmlns="{A}"><id>urn:uuid:{key}</id><published>stable</published><content>'
                + ET.tostring(root, encoding="unicode")
                + "</content></entry>"
            )
            return httpx.Response(200, text=raw, headers={"ETag": self.etags[key]})
        if method == "POST":
            if self.post_status == 412:
                return httpx.Response(412)
            self.docs[key] = ET.fromstring(kwargs["content"])
            self.etags[key] = "e2"
            self.wrote = True
            if self.add_field:
                ET.SubElement(
                    field(self.docs[key], "ProfileMemory"),
                    N + "ServerAdded",
                    {"kb": "CUD"},
                ).text = "0"
            return httpx.Response(self.post_status)
        if method == "PUT":
            key = "55555555-5555-4555-8555-555555555555"
            root = ET.fromstring(kwargs["content"])
            source = self.docs[F]
            # Native reconstruction of omitted identity/readonly fields, observed only on sparse hardware copies.
            root = deepcopy(source)
            new_name = ET.fromstring(kwargs["content"]).find(N + "ProfileName").text
            field(root, "ProfileName").text = new_name
            field(root, "SettingID").text = "new"
            field(root, "Metadata/Atom/AtomID").text = key
            field(root, "Metadata/Atom/AtomCreated").text = "later"
            if self.copy_drift:
                field(root, "ProfileMemory/DesiredMemory").text = "999"
            self.docs[key] = root
            self.etags[key] = "new-etag"
            return httpx.Response(201)
        raise AssertionError(method)

    def submit(self, path, document):
        self.calls.append(("JOB", path, {}))
        root = ET.fromstring(document)
        ns = "{http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/}"
        params = {
            n.find(ns + "ParameterName").text: n.find(ns + "ParameterValue").text or ""
            for n in root.iter(ns + "JobParameter")
        }
        if path.endswith("/ChangeDefaultProfileName"):
            self.parent["DefaultProfileName"] = params["DefaultProfileName"]
        if path.endswith("/SaveCurrentConfig") and not self.job_error:
            name = params["PartitionProfileName"]
            matches = [
                k for k, r in self.docs.items() if field(r, "ProfileName").text == name
            ]
            if matches:
                field(
                    self.docs[matches[0]], "ProfileMemory/DesiredMemory"
                ).text = "1024"
                self.etags[matches[0]] = "saved"
            else:
                key = "66666666-6666-4666-8666-666666666666"
                self.docs[key] = deepcopy(self.docs[F])
                field(self.docs[key], "ProfileName").text = name
                field(self.docs[key], "Metadata/Atom/AtomID").text = key
                self.etags[key] = "saved"
        self.job = {
            "Resource": {
                "JobID": "job-1",
                "Status": "COMPLETED_WITH_ERROR" if self.job_error else "COMPLETED_OK",
                "Results": {
                    "JobParameter": [
                        {"ParameterName": k, "ParameterValue": v}
                        for k, v in {
                            "returnCode": "1" if self.job_error else "0",
                            "result": "",
                            "detailedStatus": "",
                        }.items()
                    ]
                },
            }
        }
        return deepcopy(self.job)

    def delete(self, kind, parent, child, uuid):
        self.calls.append(("DELETE", uuid, {}))
        del self.docs[uuid]


@pytest.fixture
def service(monkeypatch):
    state = NativeService()
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.profiles.run_hmc_command",
        AsyncMock(return_value="No results were found.\n"),
    )
    return state


@pytest.mark.asyncio
async def test_list_and_exact_read_cover_names_and_preserve_saved_fields(service):
    result = await list_partition_profiles(service.hmc, S, P)
    assert {r.name for r in result.profiles} == {"source", "other"}
    record = await read_partition_profile(service.hmc, S, P, "source")
    assert record.uuid == F and record.etag == "e1"
    assert record.fields["ProfileMemory"]["DesiredMemory"] == "768"


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_patch_requested_change_and_preservation_are_independent(service, drift):
    service.add_field = drift
    result = await update_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        "e1",
        ProfilePatch(desired_memory=1024),
        exclusive_writer_window=True,
    )
    assert result.execution == "completed" and result.requested_change == "verified"
    assert result.preservation == ("drifted" if drift else "verified")
    assert result.before_etag == "e1" and result.after_etag == "e2"
    assert bool(result.differences) == drift


@pytest.mark.asyncio
async def test_http_error_after_possible_effect_never_claims_rejection(service):
    service.post_status = 500
    result = await update_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        "e1",
        ProfilePatch(desired_memory=1024),
        exclusive_writer_window=True,
    )
    assert result.execution == "unknown" and result.requested_change == "unverified"
    assert len([c for c in service.calls if c[0] == "POST"]) == 1


@pytest.mark.asyncio
async def test_412_is_known_rejection_without_retry(service):
    service.post_status = 412
    result = await update_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        "e1",
        ProfilePatch(desired_memory=1024),
        exclusive_writer_window=True,
    )
    assert result.execution == "rejected"
    assert field(service.docs[F], "ProfileMemory/DesiredMemory").text == "768"


@pytest.mark.asyncio
async def test_successful_write_with_unavailable_readback_is_not_applied(service):
    service.readback_error = True
    result = await update_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        "e1",
        ProfilePatch(desired_memory=1024),
        exclusive_writer_window=True,
    )
    assert (
        result.execution == "completed"
        and result.requested_change == "unverified"
        and result.preservation == "unverified"
    )


@pytest.mark.asyncio
async def test_prewrite_etag_and_exclusive_window_refuse_before_effect(service):
    with pytest.raises(PermissionError):
        await update_partition_profile(
            service.hmc, S, P, "source", "e1", ProfilePatch(name="new")
        )
    with pytest.raises(ValueError):
        await update_partition_profile(
            service.hmc,
            S,
            P,
            "source",
            "wrong",
            ProfilePatch(name="new"),
            exclusive_writer_window=True,
        )
    assert not [c for c in service.calls if c[0] != "GET"]


@pytest.mark.asyncio
async def test_foreign_ownership_is_checked_before_any_profile_write(service):
    service.parent["Description"] = "[hmcpctl owner:another-agent created:2026-08-14]"
    with pytest.raises(PermissionError):
        await update_partition_profile(
            service.hmc,
            S,
            P,
            "source",
            "e1",
            ProfilePatch(name="new"),
            exclusive_writer_window=True,
        )
    assert not [c for c in service.calls if c[0] != "GET"]


@pytest.mark.asyncio
async def test_default_job_and_readback_use_selected_profile_name(service):
    result = await set_default_partition_profile(
        service.hmc, S, P, "source", exclusive_writer_window=True
    )
    assert result.execution == "completed" and result.requested_change == "verified"
    assert service.parent["DefaultProfileName"] == "source"
    assert service.hmc.submit_job.call_args.args[0].endswith(
        f"/LogicalPartition/{P}/do/ChangeDefaultProfileName"
    )


@pytest.mark.asyncio
async def test_new_profile_save_does_not_require_overwrite_or_recovery(service):
    result = await save_current_partition_profile(
        service.hmc, S, P, "new", exclusive_writer_window=True
    )
    assert (
        result.execution == "completed"
        and result.requested_change == "verified"
        and result.recovery_profile is None
    )


@pytest.mark.asyncio
async def test_existing_save_requires_explicit_overwrite_and_etag(service):
    with pytest.raises(ValueError):
        await save_current_partition_profile(
            service.hmc, S, P, "source", exclusive_writer_window=True
        )
    with pytest.raises(ValueError):
        await save_current_partition_profile(
            service.hmc, S, P, "source", overwrite=True, exclusive_writer_window=True
        )
    assert not [c for c in service.calls if c[0] != "GET"]


@pytest.mark.asyncio
async def test_force_save_has_verified_retained_copy_before_job(service):
    baseline = deepcopy(service.docs[F])
    result = await save_current_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        overwrite=True,
        expected_etag="e1",
        exclusive_writer_window=True,
    )
    assert result.execution == "completed" and result.requested_change == "verified"
    assert result.recovery_verified and result.recovery_profile
    copy = service.docs[result.recovery_profile.uuid]
    assert compare_profiles(baseline, copy, recovery=True).equal
    assert field(service.docs[F], "ProfileMemory/DesiredMemory").text == "1024"
    assert [c[0] for c in service.calls if c[0] != "GET"] == ["PUT", "JOB"]


@pytest.mark.asyncio
async def test_drifted_recovery_copy_prevents_force_job_and_retains_copy(service):
    service.copy_drift = True
    result = await save_current_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        overwrite=True,
        expected_etag="e1",
        exclusive_writer_window=True,
    )
    assert result.requested_change == "unverified" and not result.recovery_verified
    assert result.recovery_profile and result.recovery_profile.uuid in service.docs
    service.hmc.submit_job.assert_not_called()
    assert field(service.docs[F], "ProfileMemory/DesiredMemory").text == "768"


@pytest.mark.asyncio
async def test_failed_native_overwrite_retains_verified_recovery_and_uncertain_destination(
    service,
):
    service.job_error = True
    result = await save_current_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        overwrite=True,
        expected_etag="e1",
        exclusive_writer_window=True,
    )
    assert result.execution == "unknown" and result.requested_change == "unverified"
    assert result.recovery_verified and result.recovery_profile.uuid in service.docs
    service.hmc.delete_child.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protected", ["default", "current", "sync", "last", "system-profile"]
)
async def test_delete_refuses_each_protected_case(service, monkeypatch, protected):
    if protected == "default":
        service.parent["DefaultProfileName"] = "source"
    if protected == "current":
        service.parent["LastActivatedProfile"] = "source"
    if protected == "sync":
        service.parent["CurrentProfileSync"] = "On"
    if protected == "last":
        del service.docs[G]
    if protected == "system-profile":
        monkeypatch.setattr(
            "hmcpctl.operations.lpar.profiles.run_hmc_command",
            AsyncMock(return_value="name\nexisting\n"),
        )
    with pytest.raises((ValueError, PermissionError)):
        await delete_partition_profile(
            service.hmc, S, P, "source", "e1", exclusive_writer_window=True
        )
    service.hmc.delete_child.assert_not_called()


@pytest.mark.asyncio
async def test_delete_confirms_absence_and_preserves_other_profiles(service):
    result = await delete_partition_profile(
        service.hmc, S, P, "source", "e1", exclusive_writer_window=True
    )
    assert result.execution == "completed" and result.requested_change == "verified"
    assert set(service.docs) == {G}


@pytest.mark.asyncio
async def test_vios_paths_and_jobs_are_not_lpar_aliases(monkeypatch):
    state = NativeService("VirtualIOServer")
    result = await set_default_partition_profile(
        state.hmc,
        S,
        P,
        "source",
        partition_type="VirtualIOServer",
        exclusive_writer_window=True,
    )
    assert result.requested_change == "verified"
    assert state.hmc.submit_job.call_args.args[0].endswith(
        f"/VirtualIOServer/{P}/do/ChangeDefaultProfileName"
    )
    state.hmc.list_logical_partitions.assert_not_called()


@pytest.mark.asyncio
async def test_staleness_after_copy_keeps_its_identity_in_outcome(service):
    def parent_read(*args, **kwargs):
        resource = deepcopy(service.parent)
        if len(service.docs) > 2:
            resource["DefaultProfileName"] = "changed-by-independent-writer"
        return {"UUID": P, "Resource": resource}

    service.hmc.get_uom.side_effect = parent_read
    result = await save_current_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        overwrite=True,
        expected_etag="e1",
        exclusive_writer_window=True,
    )
    assert result.recovery_profile is not None
    assert result.recovery_profile.uuid in service.docs
    service.hmc.submit_job.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_copy_reports_retained_recovery_name(service):
    import asyncio

    original = service.request

    def cancel_after_put(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == "PUT":
            raise asyncio.CancelledError()
        return result

    service.hmc._request_with_uuid_path_arguments.side_effect = cancel_after_put
    with pytest.raises(asyncio.CancelledError) as caught:
        await save_current_partition_profile(
            service.hmc,
            S,
            P,
            "source",
            overwrite=True,
            expected_etag="e1",
            exclusive_writer_window=True,
        )
    recovery = next(root for key, root in service.docs.items() if key not in {F, G})
    assert any(
        field(recovery, "ProfileName").text in n
        for n in getattr(caught.value, "__notes__", [])
    )
    service.hmc.submit_job.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing-code",
        "duplicate-code",
        "stderr",
        "wrong-job",
        "not-terminal",
        "missing-result",
    ],
)
async def test_job_normalization_does_not_promote_malformed_or_partial_results(
    service, failure
):
    def terminal(*args, **kwargs):
        job = deepcopy(service.job)
        parameters = job["Resource"]["Results"]["JobParameter"]
        if failure == "missing-code":
            parameters[:] = [
                p for p in parameters if p["ParameterName"] != "returnCode"
            ]
        elif failure == "duplicate-code":
            parameters.append({"ParameterName": "returnCode", "ParameterValue": "0"})
        elif failure == "stderr":
            next(p for p in parameters if p["ParameterName"] == "detailedStatus")[
                "ParameterValue"
            ] = "warning"
        elif failure == "wrong-job":
            job["Resource"]["JobID"] = "different-job"
        elif failure == "not-terminal":
            job["Resource"]["Status"] = "RUNNING"
        else:
            parameters[:] = [p for p in parameters if p["ParameterName"] != "result"]
        return job

    service.hmc.wait_for_job_entry.side_effect = terminal
    result = await set_default_partition_profile(
        service.hmc, S, P, "source", exclusive_writer_window=True
    )
    assert result.execution == "unknown" and result.requested_change == "unverified"
    assert service.hmc.submit_job.call_count == 1
    assert result.job is None or result.job.job is None


@pytest.mark.asyncio
async def test_stale_parent_snapshot_before_patch_refuses_write(service):
    calls = 0

    def parent(*args, **kwargs):
        nonlocal calls
        calls += 1
        resource = deepcopy(service.parent)
        if calls > 1:
            resource["CurrentProfileSync"] = "On"
        return {"UUID": P, "Resource": resource}

    service.hmc.get_uom.side_effect = parent
    with pytest.raises(ValueError):
        await update_partition_profile(
            service.hmc,
            S,
            P,
            "source",
            "e1",
            ProfilePatch(name="new"),
            exclusive_writer_window=True,
        )
    assert not [c for c in service.calls if c[0] != "GET"]


@pytest.mark.asyncio
async def test_save_full_system_profile_refuses_before_copy_or_force(service):
    field(service.docs[F], "ProfileType").text = "FULL_SYSTEM_LPAR_PROFILE_TYPE"
    with pytest.raises(ValueError):
        await save_current_partition_profile(
            service.hmc,
            S,
            P,
            "source",
            overwrite=True,
            expected_etag="e1",
            exclusive_writer_window=True,
        )
    assert not [c for c in service.calls if c[0] != "GET"]


@pytest.mark.asyncio
async def test_existing_recovery_copy_is_revalidated_and_reused(service):
    original = service.docs[F]
    recovery = deepcopy(original)
    field(recovery, "ProfileName").text = "retained"
    field(recovery, "SettingID").text = "copy-id"
    field(
        recovery, "Metadata/Atom/AtomID"
    ).text = "55555555-5555-4555-8555-555555555555"
    field(recovery, "Metadata/Atom/AtomCreated").text = "later"
    service.docs["55555555-5555-4555-8555-555555555555"] = recovery
    service.etags["55555555-5555-4555-8555-555555555555"] = "copy"
    result = await save_current_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        overwrite=True,
        expected_etag="e1",
        recovery_profile_selector="retained",
        exclusive_writer_window=True,
    )
    assert result.recovery_verified and result.requested_change == "verified"
    assert not [c for c in service.calls if c[0] == "PUT"]


@pytest.mark.asyncio
async def test_overwrite_identity_drift_is_not_verified_save(service):
    def terminal(*args, **kwargs):
        field(service.docs[F], "SettingID").text = "unexpected-new-identity"
        return deepcopy(service.job)

    service.hmc.wait_for_job_entry.side_effect = terminal
    result = await save_current_partition_profile(
        service.hmc,
        S,
        P,
        "source",
        overwrite=True,
        expected_etag="e1",
        exclusive_writer_window=True,
    )
    assert result.requested_change == "unverified"
    assert result.recovery_verified and result.recovery_profile.uuid in service.docs


@pytest.mark.asyncio
async def test_top_level_job_error_data_prevents_verified_completion(service):
    def terminal(*args, **kwargs):
        job = deepcopy(service.job)
        job["Resource"]["ErrorData"] = {"Message": "synthetic job error"}
        return job

    service.hmc.wait_for_job_entry.side_effect = terminal
    result = await set_default_partition_profile(
        service.hmc, S, P, "source", exclusive_writer_window=True
    )
    assert result.execution == "unknown" and result.requested_change == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "name:lpar_ids:profile_names\n",
        "No results were found.\n[stderr]\nwarning",
        "malformed",
    ],
)
async def test_empty_or_malformed_system_profile_read_never_proves_absence(
    service, monkeypatch, raw
):
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.profiles.run_hmc_command", AsyncMock(return_value=raw)
    )
    with pytest.raises(ValueError):
        await delete_partition_profile(
            service.hmc, S, P, "source", "e1", exclusive_writer_window=True
        )
    service.hmc.delete_child.assert_not_called()
