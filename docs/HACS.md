# HACS distribution and catalogue submission

## Current status

This repository is prepared as a **custom integration**, not an HA app/add-on. It remains **private** and is **not in the HACS default catalogue**. Ordinary HACS installation requires a public GitHub repository; adding metadata or passing validation does not make a private repository installable for other users.

Configured here:

- A single integration at `custom_components/omoda_jaecoo/`.
- `hacs.json` with name, minimum HA `2026.7.1`, and initial `GB` country metadata.
- UK-tested scope on the EU OMODA JAECOO backend. The country metadata is a discovery/support indication, not an API-region switch. Other EU accounts are not claimed as live-validated; expand this list when they are tested.
- Local native HA brand assets, manifest documentation/issue URLs and code owner.
- Repository description, topics and issue tracker.
- `.github/workflows/validate.yml`: HACS validation and Hassfest, **without ignored checks**, on push/PR/manual dispatch and weekly.
- A tested component ZIP builder and **draft-only** preview-release workflow.

The existing `v0.6.0` draft was prepared before the HACS metadata/validation changes. It is not a published release and must not be used to claim completion of the catalogue prerequisites. Rebuild the intended release from the final validated commit before publication; do not overwrite a published version.

## Validation evidence

- Hassfest passed on the initial preparation: https://github.com/raldred/ha-omoda-jaecoo/actions/runs/37534396155/job/112511551541
- That HACS run correctly did **not** pass: the modified MIT header prevented SPDX identification, and its public raw-file downloads returned no manifest/HACS JSON for the private repository. Canonical MIT text plus a separate manufacturer-artwork notice resolves the licence-identification issue without relicensing the artwork. Public-file accessibility remains a visibility prerequisite, not a check to ignore.
- Re-run both checks on the final public candidate and replace these preparation links with the successful final runs before submitting to the catalogue.

## Decisions required before public distribution

1. Obtain the owner's explicit approval to make the repository public. This preparation does not change visibility.
2. Review the manufacturer-owned icon rights, or replace it with artwork cleared for redistribution. The app icon is excluded from the MIT software license; private inclusion is not proof of permission to publish it. Review Git history too: removing an image only from the latest commit does not remove it from earlier commits.
3. Review release-readiness and limitations. Email/password and core telemetry have live validation; alternate auth routes and model-specific/optional functionality do not have equivalent coverage. Experimental controls remain opt-in.
4. Run HACS and Hassfest successfully on the final intended source **with no ignored checks**. Record links to the actual runs, not merely the workflow files.
5. Publish a full GitHub release after those checks pass. A draft or a standalone tag is not sufficient for default-catalogue submission.

## Custom-repository installation (after public publication)

A default-catalogue entry is **not** needed for users to install a public repository:

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/raldred/ha-omoda-jaecoo` with type **Integration**.
3. Download **Omoda / Jaecoo**, then restart HA Core.
4. Add it through **Settings → Devices & services → Add integration**.

Direct HA redirect:

https://my.home-assistant.io/redirect/hacs_repository/?owner=raldred&repository=ha-omoda-jaecoo&category=integration

Do not install alongside a different custom integration using the same `omoda_jaecoo` domain/folder. HACS source installation uses `custom_components/omoda_jaecoo/`; the separately supplied release ZIP is for manual installation, not HACS `zip_release` mode.

## Default-catalogue submission (only after all prerequisites are met)

The owner/major contributor can submit a PR to https://github.com/hacs/default:

- Fork `hacs/default`, create a new branch from its `master` branch.
- Add `"raldred/ha-omoda-jaecoo"` in alphabetical order to the root `integration` JSON list.
- Use its current PR template, allow maintainer edits, and include links to the published release, successful HACS validation and successful Hassfest run.
- Do not check off unmet prerequisites or claim that HACS/HA has reviewed the integration before it has.
- Inclusion is reviewed by HACS maintainers and can take months; it is not automatic.

No catalogue PR has been created by this preparation.

## Authoritative references

- General requirements: https://www.hacs.xyz/docs/publish/start/
- Integration requirements: https://www.hacs.xyz/docs/publish/integration/
- Validation action: https://www.hacs.xyz/docs/publish/action/
- Default inclusion: https://www.hacs.xyz/docs/publish/include/
- Hassfest action: https://github.com/home-assistant/actions#hassfest
