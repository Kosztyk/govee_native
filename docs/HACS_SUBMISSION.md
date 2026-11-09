# HACS catalog submission

This repository uses the `govee_native` domain and the integration layout
`custom_components/govee_native`. It can already be added to the Marketplace
as a custom repository; a catalog PR makes it discoverable without adding a link.

## Required repository files

| File or setting | Purpose |
|---|---|
| `hacs.json` | Display name and minimum Home Assistant version. |
| `custom_components/govee_native/manifest.json` | Unique domain, version, categories, owners, documentation, issue tracker and dependencies. |
| `custom_components/govee_native/translations/en.json` | Complete English configuration and options translations. |
| `custom_components/govee_native/brand/icon.png` | Local Govee brand image for the integration. |
| `README.md` and `docs/` | Features, installation, setup, actions, troubleshooting and verification limits. |
| `LICENSE.txt` | The repository's existing MIT license. |
| Description, topics and enabled issues | GitHub repository metadata required by the catalog. |
| Full GitHub release | A published version, rather than a tag alone. |

## Automated checks

Every push and pull request runs these workflows. They can also be started
from the Actions tab using Run workflow.

- [HACS](https://github.com/Kosztyk/govee_native/actions/workflows/hacs.yaml):
  `hacs/action@main`, category `integration`, with no ignored checks.
- [Hassfest](https://github.com/Kosztyk/govee_native/actions/workflows/hassfest.yaml):
  Home Assistant's official manifest/translation/integration validation action.
- [Tests](https://github.com/Kosztyk/govee_native/actions/workflows/tests.yaml):
  Python regression suite, runtime lint and compilation.

Before submitting, open the successful runs for the exact revision being
released. Include direct links to the HACS and hassfest runs in the catalog PR.
The local Python suite supplements these checks; it does not replace them.

## Prepare the catalog PR

1. Confirm the repository description, topics and issue tracker are present.
2. Check that HACS and hassfest pass without skipping checks.
3. Publish a full release of the passing files. Its version must match the
   integration manifest; include the existing license in distribution archives.
4. Fork [hacs/default](https://github.com/hacs/default) and create a branch from
   `master`. Add `Kosztyk/govee_native` to the JSON list in `integration`, in
   alphabetical order.
5. Open the PR from the owner's account and complete the current upstream
   template with the release and passing-run links. Do not request individual
   reviewer assignments unless a reviewer explicitly asks you to re-request review.

## Review corrections in 0.5.2

- Removed credential lookup from the separate `govee` integration, including
  its option, translations and configuration documentation. Existing Native
  entries discard the retired flag and retain their other options.
- Official MQTT events use Home Assistant's cached `client_context()` with
  `tls_set_context()`, retaining certificate and hostname verification.
- Reauthentication catches verification-email API failures and shows the
  translated `cannot_send_code` error with a working retry path.

The six review-specific regressions are in `tests/test_hacs_review.py`.
The 0.5.2 suite contains 184 passing tests, including the 0.5.1 selection fixes.

Passing automation establishes repository/metadata readiness. It does not
guarantee catalog acceptance or physical compatibility for every Govee model.
The protocol references and hardware-validation limits remain documented in
[Protocol notes](PROTOCOL_NOTES.md) and [Testing](TESTING.md).

Official requirements:
[Catalog submission](https://contributors.home-assistant.io/marketplace/guides/catalog/)
and [Validation checks](https://contributors.home-assistant.io/marketplace/reference/validation/).
