# ADR 0237: Baseline package ownership

## Status
Accepted (2026-10-18), operator-approved code-quality cleanup.

## Context
Package inventory and provisioning import the executable baseline script for
shared capture and validation. Recovery and release-window also share this code.

## Decision
Move baseline implementation, including main, into live_test/baseline.py.
Keep scripts/live_test_baseline.py as the existing executable wrapper.
Consumers import the package owner directly; no compatibility helper re-exports.

## Consequences
Capture, comparison, private snapshot data and CLI behavior remain unchanged.
Tests patch the implementation owner and separately verify executable dispatch.

## Considered & rejected
- **Retain implementation in the executable.** judgment: leaves the package
  depending on its command entrypoint.
- **Split main from its implementation globals.** judgment: adds patch and import
  boundaries without a separate CLI behavior or reuse requirement.
