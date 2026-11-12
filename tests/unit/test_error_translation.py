from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hmcpctl.errors import HMCError
from hmcpctl.operations.error_translation import (
    translate_pcm_error,
    translate_template_error,
    translate_virtual_network_create_error,
)
from hmcpctl.operations.metrics import pcm


@pytest.mark.parametrize(
    ("translator", "status", "message"),
    [
        (translate_pcm_error, 403, "PCM authority"),
        (translate_pcm_error, 406, "refused the media type"),
        (translate_template_error, 406, "refused the media type"),
        (translate_virtual_network_create_error, 406, "virtual network create"),
    ],
)
def test_error_translators_return_translated_errors(translator, status, message):
    body = "<HttpErrorResponse><Message>sensitive response body</Message></HttpErrorResponse>"
    original = HMCError("raw failure", status, body)

    translated = translator(original)

    assert translated.status_code == status
    assert translated.body == original.body
    assert "sensitive response body" in translated.body
    assert message in str(translated)


def test_virtual_network_create_406_does_not_advise_the_schema_env_var():
    translated = translate_virtual_network_create_error(HMCError("raw", 406))

    assert "X-HMC-Schema-Version" in str(translated)
    assert "per call site" in str(translated)
    assert "HMC_SCHEMA_VERSION" not in str(translated)


@pytest.mark.parametrize(
    "translator",
    [
        translate_pcm_error,
        translate_template_error,
        translate_virtual_network_create_error,
    ],
)
def test_error_translators_return_unmatched_errors_unchanged(translator):
    original = HMCError("raw failure", 500)

    assert translator(original) is original


@pytest.mark.asyncio
async def test_untranslated_pcm_error_is_reraised_without_a_cause(monkeypatch):
    original = HMCError("raw failure", 500)
    hmc = AsyncMock()
    hmc.get_pcm_preferences.side_effect = original
    monkeypatch.setattr(
        pcm, "resolve_pcm_resource", AsyncMock(return_value=pcm.PcmResource("id"))
    )

    with pytest.raises(HMCError) as raised:
        await pcm.get_pcm_preferences(hmc, "ManagedSystem", "system")

    assert raised.value is original
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_translated_pcm_error_retains_the_original_as_its_cause(monkeypatch):
    original = HMCError("raw failure", 406)
    hmc = AsyncMock()
    hmc.get_pcm_preferences.side_effect = original
    monkeypatch.setattr(
        pcm, "resolve_pcm_resource", AsyncMock(return_value=pcm.PcmResource("id"))
    )

    with pytest.raises(HMCError) as raised:
        await pcm.get_pcm_preferences(hmc, "ManagedSystem", "system")

    assert raised.value is not original
    assert raised.value.__cause__ is original
