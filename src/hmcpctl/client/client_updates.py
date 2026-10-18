"""PlatformUpdate client operations and response normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NotRequired, TypedDict, cast
from urllib.parse import quote

from ..errors import HMCError
from .client_contracts import UpdatesClient

_MEDIA_WEB_JSON = "application/vnd.ibm.powervm.web+json"


class PlatformUpdateJobParameter(TypedDict):
    ParameterName: str
    ParameterValue: str


class PlatformUpdateJobResults(TypedDict):
    JobParameter: list[PlatformUpdateJobParameter]


class PlatformUpdateJobResource(TypedDict):
    Status: str
    Results: NotRequired[PlatformUpdateJobResults]


class PlatformUpdateJobEntry(TypedDict):
    UUID: str
    Resource: PlatformUpdateJobResource
    link: NotRequired[str]


def _platform_response_error(field: str) -> HMCError:
    return HMCError(f"Malformed PlatformUpdate response: invalid {field}")


def _normalize_platform_update_result(entry: Any) -> PlatformUpdateJobParameter:
    if not isinstance(entry, dict):
        raise _platform_response_error("Result entry")
    name, value = entry.get("ParameterName"), entry.get("ParameterValue")
    if not isinstance(name, str) or not name.strip():
        raise _platform_response_error("Result ParameterName")
    if not isinstance(value, str):
        raise _platform_response_error("Result ParameterValue")
    return {"ParameterName": name, "ParameterValue": value}


def _normalize_platform_update_results(response: dict[str, Any]) -> dict[str, Any]:
    resource = dict(response)
    if "Result" in resource:
        results = resource.pop("Result")
        if not isinstance(results, list):
            raise _platform_response_error("Result")
        resource["Results"] = {
            "JobParameter": [
                _normalize_platform_update_result(entry) for entry in results
            ]
        }
    elif "Results" in resource:
        results = resource["Results"]
        if not isinstance(results, dict):
            raise _platform_response_error("Results")
        parameters = results.get("JobParameter")
        if not isinstance(parameters, list):
            raise _platform_response_error("Results JobParameter")
        resource["Results"] = {
            "JobParameter": [
                _normalize_platform_update_result(entry) for entry in parameters
            ]
        }
    return resource


def _normalize_platform_update_response(payload: Any) -> PlatformUpdateJobEntry:
    if not isinstance(payload, dict):
        raise _platform_response_error("root")
    job_id, content = payload.get("id"), payload.get("content")
    if not isinstance(job_id, str) or not job_id.strip():
        raise _platform_response_error("id")
    if not isinstance(content, dict) or not isinstance(
        response := content.get("JobResponse"), dict
    ):
        raise _platform_response_error(
            "content" if not isinstance(content, dict) else "JobResponse"
        )
    status = response.get("Status")
    if not isinstance(status, str) or not status.strip():
        raise _platform_response_error("Status")
    self_link = payload.get("selfLink")
    if self_link is not None and (
        not isinstance(self_link, str) or not self_link.strip()
    ):
        raise _platform_response_error("selfLink")
    normalized: PlatformUpdateJobEntry = {
        "UUID": job_id.strip(),
        "Resource": cast(
            PlatformUpdateJobResource, _normalize_platform_update_results(response)
        ),
    }
    if isinstance(self_link, str):
        normalized["link"] = self_link.strip()
    return normalized


class UpdatesMixin:
    async def submit_platform_update(
        self: UpdatesClient, system_uuid: str, job_request: Mapping[str, Any]
    ) -> PlatformUpdateJobEntry | None:
        path = f"/rest/api/uom/ManagedSystem/{quote(system_uuid, safe='')}/do/PlatformUpdate"
        response = await self._request_with_uuid_path_arguments(
            "PUT",
            path,
            uuid_path_arguments={"system_uuid": system_uuid},
            json=job_request,
            headers={
                "Content-Type": f"{_MEDIA_WEB_JSON}; type=JobRequest",
                "Accept": "application/json",
            },
        )
        if response.status_code not in (200, 201, 202, 204):
            # Rejections can echo private update inputs; omit the body from both
            # rendered diagnostics and HMCError.body (as for malformed successes).
            raise HMCError(f"PUT {path} failed", response.status_code)
        if not response.content or not response.text.strip():
            return None
        try:
            return _normalize_platform_update_response(response.json())
        except ValueError as exc:
            raise HMCError(
                f"PUT {path}: Malformed PlatformUpdate response: body is not valid JSON"
            ) from exc
        except RecursionError as exc:
            # json.loads recurses per nesting level; RecursionError carries
            # no message, hence the fixed clause.
            raise HMCError(
                f"PUT {path}: Malformed PlatformUpdate response: "
                "document nesting is too deep"
            ) from exc
