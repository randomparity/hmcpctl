"""C04 strict native fields and source-backed policy admission."""

import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.documents.lpar_security_policy import (
    LparSecurityPolicy,
    SecurityPolicyState,
)
from hmcpctl.operations.lpar.security_policy import (
    SecurityPolicyCapabilities,
    admit_security_policy,
)

FIELDS = (
    "secure_boot",
    "keystore_kbytes",
    "keystore_signed_updates",
    "linux_dynamic_key_secure_boot",
    "keystore_signed_updates_without_verification",
)
CAPS = SecurityPolicyCapabilities(True, True, True, 4, 64, True, (11, 1, 1110), 1110)


@pytest.mark.parametrize("corpus", ["p10", "p11"])
@pytest.mark.parametrize("command", ["mksyscfg", "chsyscfg"])
def test_reference_bindings(corpus, command):
    expected = FIELDS[:4] if command == "mksyscfg" else FIELDS
    source = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "refs"
        / f"hmc-commands-{corpus}"
        / "commands"
        / f"{command}.md"
    )
    if source.exists():
        text = source.read_text()
        section = text[text.index("Attribute names for partitions") :]
        section = section[: section.index("Attribute names for partition profiles")]
        bindings = re.findall(
            r"\*\*((?:secure_boot|keystore_[a-z_]+|linux_dynamic_key_secure_boot))\*\*",
            section,
        )
        assert bindings == list(expected)
    for name in expected:
        value = 0 if name in FIELDS[:2] else False
        assert LparSecurityPolicy(**{name: value}).pairs() == [(name, 0)]


def test_strict_values_and_omission():
    assert LparSecurityPolicy().pairs() == []
    assert LparSecurityPolicy(0, 0, False, False, False).pairs() == [
        (name, 0) for name in FIELDS
    ]
    assert LparSecurityPolicy(9, 64, True, True, True).pairs() == list(
        zip(FIELDS, (9, 64, 1, 1, 1), strict=True)
    )
    for name, value in [
        ("secure_boot", True),
        ("secure_boot", -1),
        ("secure_boot", 10),
        ("keystore_kbytes", -1),
        ("keystore_kbytes", False),
    ]:
        with pytest.raises(ValueError):
            LparSecurityPolicy(**{name: value})
    for name in FIELDS:
        for value in ("sensitive-value", 1.0, [], {}):
            with pytest.raises((ValueError, TypeError)) as error:
                LparSecurityPolicy(**{name: value})
            assert "sensitive-value" not in str(error.value)
            with pytest.raises(ValidationError) as error:
                TypeAdapter(LparSecurityPolicy).validate_python({name: value})
            assert "sensitive-value" not in str(error.value)
    for name in FIELDS[2:]:
        with pytest.raises(ValueError):
            LparSecurityPolicy(**{name: 0})
    with pytest.raises((TypeError, ValueError)):
        LparSecurityPolicy(unexpected=True)
    with pytest.raises(ValidationError) as error:
        TypeAdapter(LparSecurityPolicy).validate_python(
            {"unexpected": "sensitive-value"}
        )
    assert "sensitive-value" not in str(error.value)
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        LparSecurityPolicy().unexpected = True


@pytest.mark.parametrize("value", [False, True])
def test_bypass_is_update_only(value):
    for environment in ("AIX/Linux", "Virtual IO Server", "OS400"):
        with pytest.raises(ValueError, match="update.only"):
            LparSecurityPolicy(
                keystore_signed_updates_without_verification=value
            ).validate_creation(environment)


# Expected outcomes are the reviewed dependency/state matrix, not calculated from admission.
@pytest.mark.parametrize("environment", ["AIX/Linux", "Virtual IO Server", "OS400"])
@pytest.mark.parametrize("value", [None, False, True])
def test_linux_applicability(environment, value):
    settings = LparSecurityPolicy(
        keystore_kbytes=4,
        keystore_signed_updates=True,
        linux_dynamic_key_secure_boot=value,
    )
    if value is not None and environment != "AIX/Linux":
        with pytest.raises(ValueError, match="AIX/Linux"):
            admit_security_policy(settings, CAPS, environment=environment)
    else:
        admit_security_policy(settings, CAPS, environment=environment)


@pytest.mark.parametrize(
    "requested,old,state,cap_changes,allowed",
    [
        (
            {"keystore_kbytes": 0},
            {"keystore_kbytes": 4},
            "running",
            {"power11": False},
            True,
        ),
        (
            {"keystore_kbytes": 4},
            {"keystore_kbytes": 8},
            "running",
            {"power11": False},
            False,
        ),
        ({"keystore_kbytes": 4}, {"keystore_kbytes": 8}, "running", {}, True),
        ({"keystore_kbytes": 4}, {"keystore_kbytes": 0}, "running", {}, False),
        ({"keystore_kbytes": 4}, {"keystore_kbytes": 0}, "not activated", {}, True),
        ({"keystore_kbytes": 64}, {"keystore_kbytes": 4}, "not activated", {}, True),
        ({"keystore_kbytes": 65}, {"keystore_kbytes": 4}, "not activated", {}, False),
        ({"keystore_kbytes": 3}, {"keystore_kbytes": 4}, "not activated", {}, False),
        ({"keystore_kbytes": 4}, {}, "not activated", {}, False),
        ({"secure_boot": 0}, {}, "running", {}, True),
        ({"secure_boot": 9}, {}, "not activated", {}, True),
        ({"secure_boot": 1}, {}, "starting", {}, False),
        ({"secure_boot": 1}, {}, "Unknown", {}, False),
        ({"secure_boot": 1}, {}, "Not Activated", {}, False),
        (
            {"keystore_signed_updates": False},
            {"keystore_signed_updates": True},
            "not activated",
            {},
            False,
        ),
        (
            {"linux_dynamic_key_secure_boot": False},
            {"linux_dynamic_key_secure_boot": True},
            "not activated",
            {},
            False,
        ),
        ({"keystore_signed_updates": False}, {}, "not activated", {}, False),
        ({"linux_dynamic_key_secure_boot": False}, {}, "not activated", {}, False),
        (
            {"keystore_signed_updates": False},
            {"keystore_signed_updates": False},
            "running",
            {},
            True,
        ),
        (
            {"keystore_signed_updates": True},
            {"keystore_kbytes": 4},
            "not activated",
            {},
            True,
        ),
        (
            {"keystore_signed_updates": True},
            {"keystore_kbytes": 4},
            "running",
            {},
            False,
        ),
        ({"keystore_signed_updates": True}, {}, "not activated", {}, False),
        (
            {"keystore_signed_updates": True},
            {"keystore_kbytes": 0},
            "not activated",
            {},
            False,
        ),
        (
            {"linux_dynamic_key_secure_boot": True},
            {"keystore_kbytes": 4, "keystore_signed_updates": True},
            "not activated",
            {},
            True,
        ),
        (
            {"linux_dynamic_key_secure_boot": True},
            {"keystore_kbytes": 4},
            "not activated",
            {},
            False,
        ),
        (
            {"keystore_signed_updates_without_verification": True},
            {"keystore_kbytes": 4, "keystore_signed_updates": True},
            "running",
            {},
            True,
        ),
        (
            {"keystore_signed_updates_without_verification": True},
            {"keystore_kbytes": 4},
            "running",
            {},
            False,
        ),
        (
            {"keystore_signed_updates_without_verification": False},
            {},
            "running",
            {},
            True,
        ),
        (
            {"keystore_signed_updates_without_verification": False},
            {},
            "running",
            {"dynamic_key": False},
            False,
        ),
        (
            {"keystore_signed_updates": False},
            {"keystore_signed_updates": False},
            "running",
            {"dynamic_key": False},
            False,
        ),
        ({"secure_boot": 0}, {}, "running", {"secure_boot": False}, False),
        ({"keystore_kbytes": 0}, {}, "running", {"keystore": False}, False),
    ],
)
def test_admission_matrix(requested, old, state, cap_changes, allowed):
    before = SecurityPolicyState("AIX/Linux", state, LparSecurityPolicy(**old), 0)
    settings = LparSecurityPolicy(**requested)
    caps = replace(CAPS, **cap_changes)
    if allowed:
        admit_security_policy(settings, caps, environment="AIX/Linux", before=before)
    else:
        with pytest.raises(ValueError):
            admit_security_policy(
                settings, caps, environment="AIX/Linux", before=before
            )


@pytest.mark.parametrize(
    "name", ["keystore_signed_updates", "linux_dynamic_key_secure_boot"]
)
def test_irreversible_rejection(name):
    before = SecurityPolicyState(
        "AIX/Linux",
        "not activated",
        LparSecurityPolicy(
            keystore_kbytes=4,
            keystore_signed_updates=True,
            linux_dynamic_key_secure_boot=True,
        ),
        0,
    )
    with pytest.raises(ValueError, match="irreversible"):
        admit_security_policy(
            LparSecurityPolicy(**{name: False}),
            CAPS,
            environment="AIX/Linux",
            before=before,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"hmc_version": None},
        {"hmc_version": (9, 2, 949)},
        {"firmware_level": None},
        {"firmware_level": 949},
    ],
)
def test_pks_release_gates(changes):
    with pytest.raises(ValueError, match="release"):
        admit_security_policy(
            LparSecurityPolicy(keystore_kbytes=4),
            replace(CAPS, **changes),
            environment="AIX/Linux",
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"power11": False},
        {"hmc_version": None},
        {"hmc_version": (11, 1, 1109)},
        {"firmware_level": None},
        {"firmware_level": 1109},
    ],
)
def test_p11_release_gates(changes):
    caps = replace(CAPS, **changes)
    with pytest.raises(ValueError, match="Power11"):
        admit_security_policy(
            LparSecurityPolicy(secure_boot=0), caps, environment="OS400"
        )
    before = SecurityPolicyState(
        "AIX/Linux", "running", LparSecurityPolicy(keystore_kbytes=8), 0
    )
    with pytest.raises(ValueError, match="release"):
        admit_security_policy(
            LparSecurityPolicy(keystore_kbytes=4),
            caps,
            environment="AIX/Linux",
            before=before,
        )


@pytest.mark.parametrize(
    "minimum,maximum", [(None, 64), (4, None), (0, 64), (4, -1), (64, 4)]
)
def test_missing_or_malformed_advertisement_refuses(minimum, maximum):
    with pytest.raises(ValueError, match="bounds"):
        admit_security_policy(
            LparSecurityPolicy(keystore_kbytes=4),
            replace(CAPS, minimum_kbytes=minimum, maximum_kbytes=maximum),
            environment="AIX/Linux",
        )


def test_creation_dependencies_and_environment():
    for settings in (
        LparSecurityPolicy(keystore_signed_updates=True),
        LparSecurityPolicy(keystore_kbytes=4, linux_dynamic_key_secure_boot=True),
    ):
        with pytest.raises(ValueError, match="requires"):
            admit_security_policy(settings, CAPS, environment="AIX/Linux")
    admit_security_policy(
        LparSecurityPolicy(
            keystore_kbytes=4,
            keystore_signed_updates=True,
            linux_dynamic_key_secure_boot=True,
        ),
        CAPS,
        environment="AIX/Linux",
    )
    with pytest.raises(ValueError, match="environment"):
        admit_security_policy(
            LparSecurityPolicy(secure_boot=1), CAPS, environment="untrusted-value"
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"secure_boot": "PRIVATE-POLICY-MARKER"},
        {"PRIVATE-POLICY-MARKER": True},
        "PRIVATE-POLICY-MARKER",
        {"PRIVATE-POLICY-MARKER" + str(i) + "x" * 70000: True for i in range(8)},
    ],
)
def test_nested_validation_details_are_private_and_bounded(invalid):
    from pydantic import BaseModel

    class Arguments(BaseModel):
        settings: LparSecurityPolicy

    with pytest.raises(ValidationError) as caught:
        Arguments.model_validate({"settings": invalid})
    errors = caught.value.errors()
    assert "PRIVATE-POLICY-MARKER" not in str(errors)
    assert "PRIVATE-POLICY-MARKER" not in str(caught.value)
    assert len(errors) <= 6
    assert len(str(errors)) < 3000
    assert all(e["input"] is None for e in errors)


def test_private_validation_retains_compiled_fields_codes_and_counts():
    invalid = {name: "PRIVATE-POLICY-MARKER" for name in FIELDS}
    invalid.update({f"PRIVATE-POLICY-MARKER-{i}": True for i in range(20)})
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(LparSecurityPolicy).validate_python(invalid)
    errors = caught.value.errors()
    assert len(errors) == 6
    assert [error["loc"] for error in errors] == [(name,) for name in FIELDS] + [
        ("unknown_field",)
    ]
    assert [error["type"] for error in errors] == [
        "int_type",
        "int_type",
        "bool_type",
        "bool_type",
        "bool_type",
        "extra_forbidden",
    ]
    assert all(error["input"] is None for error in errors)
    assert all("ctx" not in error for error in errors)
    assert "PRIVATE-POLICY-MARKER" not in str(caught.value)
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(LparSecurityPolicy).validate_python({"secure_boot": 10})
    assert caught.value.errors()[0]["loc"] == ("secure_boot",)
    assert caught.value.errors()[0]["type"] == "value_error"
    assert caught.value.errors()[0]["input"] is None
