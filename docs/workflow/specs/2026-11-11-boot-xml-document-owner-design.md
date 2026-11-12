# Boot XML document ownership

## Problem and authority

Boot-order interpretation depends on a private client helper. The operator
approved the prepared three-file relocation and its exclusions. ADR 0367
records the existing document owner selected over duplication.

## Design and transition

Move `_exact_partition_path(lpar: ET.Element, path: str) -> ET.Element | None`
from client_lpars to documents.lpar as `exact_partition_path`. Keep its body
unchanged except the equal namespace binding. Three client edit sites and five
boot-order sites import the canonical helper. Remove the old definition with no
alias. Boot-order root checks use common.UOM_NS directly instead of profile.N.

## Success

The named consumers share one function; document-helper imports do not require
the client layer. Missing paths return None; duplicates and wrong namespaces
raise the same HMCError text. Existing caller root/scalar validation, external
requests, authorization, stable facade and MCP/CLI behavior remain unchanged.

## Failure model

- Actors/deployments: package callers interpreting configured HMC partition XML
  through client editing or boot-order operations, supported Python versions.
- Assets/invariants: exact namespace rejection, optional-path semantics, XML
  nonmutation during traversal and operation request/authorization ordering.
- Accepted: old private helper imports stop working after direct replacement;
  pre-release internal imports have no compatibility alias by approved design.
- Covered elsewhere: caller root/scalar checks and transport/admission remain
  existing client/operation owners, exercised by their existing tests. This
  relocation adds no parser policy or live hardware qualification.

## Validation

One import-blocking subprocess regression enforces owner independence, consumer
identity and obsolete-helper removal; it must fail before the move. Sixty
prepared old/new XML cases cover missing, duplicate and namespace variants with
identical errors and no mutation. Existing boot, edit, document, sharing,
request-path and facade tests cover preserved behavior. Relevant hooks apply.
