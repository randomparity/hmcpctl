# Preserve the normalized MCP job inspection outcome
Full-spec M denominator250; userapproved MCP output replacement scope3.

## Scope and success
C1: hmc_get_job returns the existing operations.jobs.get_job JobOutcome directly,
like hmc_wait_for_job/CLI; raw HMC entry remains in outcome.job. Missingjobs use
foundfalse/jobnull, not top-levelnull. No new model or changes to polling/read.
C2: corrected job_href remains available after stale-link fallback; raw entry may
still advertise stale href and must remain unchanged. Preserve response-derived
job_id/mispair warnings and authorization/all-targets policy/decorators/arguments.
C3: migrate scripts live_test.metrics/bare_cec job.get readers using their existing
_as_outcome path as for job.wait, and direct tests. Keep evidenceassertionsstrict;
do not wrap normalized outcome again with job_outcome(raw).
C4: actual FastMCP wire schema is JobOutcome, all required nullable fields retained;
regenerate docs with just tool-docs, update currenttool docs and maintainedexamples.
Historical ADR0093 and old specs remain historical; ADR0239 refines its presentation
carve-out with explicituserauthority, retaining its polling/handle safety rules.

## Failure model
Actors: MCPjobget callers and live metrics/barecec consumers, valid/missing,
stale and mispaired handles, generated-model or dictionary MCP data.
Assets: corrected persistablehandle/rawentry identity, true foundstatus,
existing conservative evidence grading, authorization and URIvalidation.
Accepted classes: deliberate pre-release rawdict/null->JobOutcome wirebreak
explicitlyapproved; oldexternalcallersmigrate, no compatibilityoutputalias.
Other owners: operations.jobs/JobOutcome ownfallback/disappearance semantics,
sharedlive _as_outcome owns model/dictreading; no policyrelocation.

## Validation
Focused-test red oldadapter: stalehref404/globalfallback returns outcome with
correctedjob_href while rawjob's stale SELF remains; missing/normal/HTTPerrors
preserved; realFastMCPschema and callserialization expose fulloutcome/rawjob.
Migrate oldrawshape assertions to approvednormalizedcontract without weakening
identifier/error/found/authorization expectations. Live metrics/barecec mocks use
realoutcomes and existinggrading, generatedmodels accepted/invalidreadsrefused.
Controlled returnoutcome.job restoration mustfailhandle/wiretest. Run joboperation,
servertools/capability/security and relevantliveconsumer tests --no-cov;
justtool-docs thenlint/format/types/generatedguards/all14actualhooks.
Root final fullverify/independentreview; no liveHMC run or dependencychanges.
