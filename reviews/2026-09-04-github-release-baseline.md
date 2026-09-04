# GitHub release baseline

- Date: 2026-09-04
- Target branch: `16.0`
- Repository: `soloztech/contact-center`
- Cross-repository candidate: `16.0.20260904.3-rc1`

## Applied laboratory baseline

The five addons were released atomically to SERVIDOR05 with tree
`83b88b22773067c34e370d3784ec05f1bd0b2b09f08a478230475bbe16bb1f9e`:

| Addon                   |       Version |
| ----------------------- | ------------: |
| `contact_center_base`   | `16.0.1.43.5` |
| `contact_center_wuzapi` | `16.0.1.29.2` |
| `contact_center_meta`   |  `16.0.2.1.1` |
| `contact_center_crm`    |  `16.0.2.4.6` |
| `contact_center_ui`     | `16.0.1.26.5` |

The release passed Base 505/505, WuzAPI 199/199 and integrated 895/895 tests. QUnit
passed Base 4/4 with 15/15 assertions and UI 144/144 with 1,248/1,248 assertions in both
minified and `debug=assets` modes. The offline upgrade returned zero, all temporary
resources were removed, the test route was restored byte-identically and the public
laboratory endpoint returned HTTP 200.

Canonical evidence:
`scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904T124751394613Z/summary.json`.

The shared Meta dependencies were verified at the same boundary: each resolves once
under `/mnt/outros/marketing-center`, at the exact version installed in the database.

## Publication candidate

The final publication-only pass regenerated the OCA addon descriptions from their
authoritative fragments and added the pinned private-CI workflow. It did not change any
Python, XML data/view, access, JavaScript or SCSS runtime source. The canonical release
dry-run then returned `dry_run_ready` for 261 deployable files with tree
`8dfdaabd5926687f09d755798032910946d4353ff406753ae1a007aee5382c77`; evidence:
`scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904T131728919318Z/summary.json`.

The exact publication tree also passed the complete pinned pre-commit suite. Its Odoo
tests remain the applied results above; the GitHub workflows test the exact tagged tree
again before this candidate is declared green.

## Sanitized publication history

The current source tree contains no known real-person fixtures or repository credential.
The historical development branch did contain laboratory identifiers, so it will not be
pushed. GitHub receives a new root commit containing exactly this final tree. The
complete prior history remains recoverable only from the verified private bundle with
SHA-256 `48692ef85b7b65047576a0628850978559cb06eceee0e98fd7827dce4a94eaf2`, also
recorded in the cross-repository baseline.

No historical local branch or `*-lab` tag is part of the GitHub release.

The Contact Center CI resolves its private Marketing Center dependency at the matching
immutable candidate tag instead of the moving `16.0` branch. Both tags must therefore
exist before Actions is enabled and the workflows are dispatched.

## Scope

This release candidate is the first production-target source baseline. It does not claim
that the production go-live gates for retention/LGPD, edge rate limiting and controlled
provider failure exercises are complete. Production was not accessed or changed by this
validation.

## GitHub publication result

The private repository and coordinated prerelease are available at:

- repository: <https://github.com/soloztech/contact-center>;
- release:
  <https://github.com/soloztech/contact-center/releases/tag/16.0.20260904.3-rc1>;
- pre-commit: <https://github.com/soloztech/contact-center/actions/runs/33879183325>;
- dependency gate and Odoo tests:
  <https://github.com/soloztech/contact-center/actions/runs/33879183066>.

All checks completed successfully. Actions has read-only workflow permissions, mandatory
SHA pinning and an exact four-action allowlist. Vulnerability alerts and automated
security fixes are enabled. GitHub rejected branch protection/rulesets for this private
repository under the current organization plan; making the source public is not an
acceptable workaround. The immutable release tag and restricted write access are the
current compensating controls.
