"""Direct contracts for LPAR write-error translation."""

from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.errors import translate_lpar_write_error


def test_lpar_write_error_translation_preserves_406_response_body():
    original = HMCError(
        "original",
        406,
        body="<HttpErrorResponse><Message>detail</Message></HttpErrorResponse>",
    )
    translated = translate_lpar_write_error(original)

    assert translated.status_code == 406
    assert translated.body == original.body
    assert "X-HMC-Schema-Version" in str(translated)
    assert "per call site" in str(translated)
    assert "HMC_SCHEMA_VERSION" not in str(translated)


def test_lpar_write_error_translation_preserves_other_errors_unchanged():
    error = HMCError("original", 409, body="conflict")

    assert translate_lpar_write_error(error) is error
