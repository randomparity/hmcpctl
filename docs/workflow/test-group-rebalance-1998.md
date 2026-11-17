# Four-group timing evidence (#1998)

## Source and completeness

Two successful ordinary CI runs after #2004 were profiled. No benchmark or failed run
was used. Each supplied 20 nonexpired JUnit diagnostic artifacts (four groups × five
Python versions), complete job metadata and successful group conclusions. Requests were
bounded to 30 seconds, multi-fetch operations to 120 seconds; each full artifact download
batch completed in under seven seconds. Baseline inspection stopped after these two runs.

### Run [44039914716](https://github.com/randomparity/hmcpctl/actions/runs/44039914716)

Run head `3f4a390c70beeb36946eee205ec0b5bfe97185a5`; actual checkout `3f4a390c70beeb36946eee205ec0b5bfe97185a5`.
Complete checkout tree `5547af187cc82b8038c2e36befa60afd9813184e`.

Seconds: **job wall / Run test group step**. Job wall includes setup and artifact
upload, excludes queue time; neither is the sum of testcase durations.

| Python | domain-and-unit | application | procedure-arms | tooling |
|---|---:|---:|---:|---:|
| 3.11 | 211 / 194 | 482 / 467 | 348 / 327 | 281 / 252 |
| 3.12 | 211 / 188 | 415 / 395 | 249 / 226 | 266 / 248 |
| 3.13 | 270 / 249 | 318 / 292 | 182 / 166 | 259 / 234 |
| 3.14 | 142 / 116 | 252 / 235 | 150 / 132 | 287 / 265 |
| 3.15 | 128 / 111 | 319 / 300 | 181 / 163 | 220 / 204 |

Summed JUnit testcase seconds by existing group and selected file. Class-method cases
are counted in their containing file; durations include pytest fixture/test phases,
and two workers can overlap them. These are cost signals, not wall-time predictions.

| Python | domain-and-unit | application | procedure-arms | tooling | CLI commands | CLI HTTPS e2e |
|---|---:|---:|---:|---:|---:|---:|---:|
| 3.11 | 325.2 | 863.0 | 590.8 | 467.5 | 79.4 | 90.5 |
| 3.12 | 308.1 | 725.3 | 396.4 | 457.3 | 69.5 | 77.8 |
| 3.13 | 411.4 | 530.2 | 282.7 | 426.2 | 47.0 | 67.5 |
| 3.14 | 193.3 | 426.3 | 221.1 | 455.2 | 34.5 | 70.7 |
| 3.15 | 182.5 | 549.5 | 276.3 | 349.4 | 45.5 | 85.2 |

Retained testcase counts by Python: 3.11: 28406, 3.12: 28406, 3.13: 28406, 3.14: 28406, 3.15: 28406.

### Run [44038622015](https://github.com/randomparity/hmcpctl/actions/runs/44038622015)

Run head `e0f8c1fa4d609e833dbaa217e147e9f3169209e8`; actual checkout `1ec4e2c4943b28c9c768e6939413a4cc3b930d81`.
Complete checkout tree `5547af187cc82b8038c2e36befa60afd9813184e`.

Seconds: **job wall / Run test group step**. Job wall includes setup and artifact
upload, excludes queue time; neither is the sum of testcase durations.

| Python | domain-and-unit | application | procedure-arms | tooling |
|---|---:|---:|---:|---:|
| 3.11 | 212 / 186 | 336 / 319 | 268 / 253 | 257 / 242 |
| 3.12 | 237 / 219 | 372 / 344 | 378 / 359 | 366 / 345 |
| 3.13 | 191 / 160 | 335 / 318 | 212 / 187 | 216 / 200 |
| 3.14 | 175 / 155 | 306 / 283 | 133 / 115 | 304 / 283 |
| 3.15 | 138 / 120 | 343 / 323 | 144 / 116 | 300 / 285 |

Summed JUnit testcase seconds by existing group and selected file. Class-method cases
are counted in their containing file; durations include pytest fixture/test phases,
and two workers can overlap them. These are cost signals, not wall-time predictions.

| Python | domain-and-unit | application | procedure-arms | tooling | CLI commands | CLI HTTPS e2e |
|---|---:|---:|---:|---:|---:|---:|---:|
| 3.11 | 315.2 | 580.6 | 454.1 | 450.9 | 51.7 | 67.8 |
| 3.12 | 361.0 | 630.1 | 645.0 | 635.8 | 59.9 | 70.7 |
| 3.13 | 267.4 | 580.7 | 323.0 | 363.0 | 49.7 | 73.7 |
| 3.14 | 256.9 | 509.6 | 188.1 | 509.9 | 40.2 | 78.3 |
| 3.15 | 203.7 | 586.5 | 190.8 | 513.1 | 51.9 | 86.1 |

Retained testcase counts by Python: 3.11: 28406, 3.12: 28406, 3.13: 28406, 3.14: 28406, 3.15: 28406.

## Source equality and proposed cut

`git rev-parse <checkout>^{tree}` verified both actual checkout trees equal current main
`3f4a390c70beeb36946eee205ec0b5bfe97185a5` (tree
`5547af187cc82b8038c2e36befa60afd9813184e`). The PR-run checkout identity comes from
the checkout step log, rather than assuming the run head was the tested object. This
establishes equal tracked inputs, not equal virtual environments, host resources or caches.

The cut moves exactly `tests/app/test_cli_commands.py` and `tests/app/test_cli_e2e.py`
from application to domain-and-unit. These exercise the broad direct-command and HTTPS
CLI paths. Feature-specific CLI presentation files remain in application. In the two
3.11 samples this transfers 169.9/119.5 summed seconds: application would account for
693.1/461.1 and domain-and-unit for 495.1/434.7 before accounting for scheduling,
new regression-test cost and different runner conditions. This calculation is a workload
comparison, not a measured after result. Procedure-arms and tooling selectors are unchanged.

## Limits and after-cut evidence

Two baseline samples are insufficient for a stable median or causal speedup claim. These
runs varied substantially even with identical tracked trees. Hosted runners, CPU scheduling,
uv/dependency caches, setup and artifact-upload cost, package SCM metadata and shared account
load were not controlled. `loadfile` also imposes indivisible-file costs and ordering effects.

The candidate adds a real repository collection-partition regression: its own cost is part
of the measured candidate, rather than subtracted to make the cut look faster. Local
arm64 verification does not establish amd64 or the other Python versions. The normal
candidate CI run supplies those legs and the after-cut job/step sample. Its exact run,
checkout/head identities, actual times and partition evidence are reported in the PR body
after completion; no full matrix is rerun solely to manufacture cleaner timing statistics.

Four names/checks, the two-worker cap, artifact topology and the per-version combined
coverage gate stay unchanged. No runtime SLA is claimed.
