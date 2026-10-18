# Normalize public ISO download failures
Full-spec S denominator100; operator-approved ISO error boundary, no newarchitecture.

## Scope and success
C1: upload_iso translates httpx.HTTPError from its existing download stage into
HMCError with ISO-download context and chained original cause. Keep existing
InvalidURL branch/message. HTTP-status failures may report numeric status in
bounded text; other HTTP errors use a fixed message without exception/body/URL.
C2: _download_iso_from_url stays the raw private download primitive. Existing
allowlist/redirect/size ValueError, FileExistsError, local OSError, cancellation
and upload/release behavior remain unchanged. Do not catch BaseException.
C3: actual failed download never starts HMC web File upload, and partial local
file cleanup/HTTP context closure still runs. Public Raises documentation states
normalized HTTP-error contract; original cause available for private diagnosis.

## Failure model
Actors: upload_iso callers with status failure, connect/read/stream failure,
invalidURL or refusedsource; existingmock transports/offline tests.
Assets: error-family predictability, sourceURL/response-body privacy, localtemp
ownership, upload ordering, allowlist/redirect/size controls.
Accepted classes: existing non-HTTP filesystem/runtime failures keep original
classes; private raw helper retains httpx errors for direct callers.
Other owners: download helper owns context/tempcleanup; HMC upload owns its own
release reconciliation, no changes to either policy.

## Verification
Focused-test: real httpx.MockTransport HTTP404/500/connecttimeout/readtimeout/
streamerror through upload_iso produces HMCError (oldred), matching cause,
no remoteURL/token/body in publicmessage and no webFilecreate/upload. Tempfixture
shows partial file removed afterstreamfault. Existing privatehelperHTTPStatusError,
cancellation, InvalidURL/redirect/size/security and uploadcleanup tests retained.
Controlled removal of translation makes newcasefail, thenrestore. Run ISOupload/
web-file/media relevanttests --no-cov; lint/format/types and all14actualhooks.
Root final assembled fullverify andblindassessment, no hardware/externalwrites.
