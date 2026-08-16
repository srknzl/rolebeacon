# Security policy

## Supported versions

The latest `0.2.x` release and the default branch receive security fixes during the initial
development period.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature on the RoleBeacon repository. Include the
affected version, reproduction steps, impact, and any suggested mitigation. Do not open a public
issue until a fix and disclosure timeline have been agreed with the maintainer.

Maintainers triage dependency alerts and private reports as release-blocking when they affect a
reachable RoleBeacon path. Every pull request audits the locked runtime dependency set, and CI
publishes a CycloneDX SBOM artifact. Dependency updates must regenerate `uv.lock`, pass the audit,
and include normal regression coverage; a temporary exception requires a documented reachability
assessment and an upstream remediation plan.

## Security boundaries

- RoleBeacon binds to localhost by default.
- Setup must be activated before any job source is contacted.
- API keys are stored in a local permission-restricted secrets file and are never returned to the
  browser after setup.
- Browser automation prepares forms but cannot submit them.
- External résumé commands run as argument arrays without a shell.
- Gmail OAuth tokens and persistent browser sessions are not imported from legacy installations.

Users remain responsible for reviewing source terms, model endpoints, generated documents, and
every application answer before submission.
