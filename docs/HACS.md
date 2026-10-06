# HACS installation and catalogue submission

## Custom repository

This is a Home Assistant **custom integration**, not an HA app/add-on. It can be installed as a HACS custom repository once the public publication/validation step is complete. It is **not yet included in the default HACS catalogue**.

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/raldred/ha-omoda-jaecoo` with type **Integration**.
3. Download **Omoda / Jaecoo**, then restart HA Core.
4. Add it through **Settings → Devices & services → Add integration**.

[Open the repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=raldred&repository=ha-omoda-jaecoo&category=integration)

Do not install alongside a different integration using the same `omoda_jaecoo` domain. HACS installs from `custom_components/omoda_jaecoo/`; the separate release ZIP is for manual installation, not HACS `zip_release` mode.

## Repository preparation

- One integration directory with native local brand assets.
- `hacs.json`: minimum HA `2026.7.1`, initial `GB` country metadata. This indicates UK-tested scope on the EU app backend, not an API region switch or a claim that other countries cannot work.
- Manifest documentation/issue URLs and code owner; repository description/topics/issues enabled.
- HACS and Hassfest workflows with **no ignored checks**, on push/PR/manual dispatch and weekly.
- Canonical MIT software license, with separately attributed manufacturer artwork excluded from MIT.
- Component-only release ZIP and checksums. Automated tests are retained as synthetic source for CI but excluded from the installable ZIP. The original prototype probe is retired from the current branch.
- App archives, decompiled sources, local analysis, captures, environment files, account data and private keys are excluded. The pre-publication history audit found no personal secret or private vehicle/HA artifacts in reachable history. Historical development code and previous manufacturer branding remain ordinary Git history, not private account captures.

## Validation

Private preparation runs passed Hassfest. HACS passed licence/brand/repository checks but could not retrieve the private raw manifest files; this was a visibility failure, not a reason to disable validation.

After public publication, both validators must pass on the intended release commit, followed by a published GitHub release. See [validation runs](https://github.com/raldred/ha-omoda-jaecoo/actions/workflows/validate.yml). A draft release or standalone tag is not sufficient for catalogue submission.

## Default catalogue

The owner/major contributor can submit a PR to https://github.com/hacs/default:

1. Fork `hacs/default`, create a branch from its `master` branch.
2. Add `"raldred/ha-omoda-jaecoo"` alphabetically to the root `integration` JSON list.
3. Use its current PR template, allow maintainer edits, and provide links to a published release plus successful HACS/Hassfest runs without ignores.
4. Do not claim unmet prerequisites or that HA/HACS has reviewed the software before it has.

Catalogue inclusion requires maintainer review and can take months. Custom-repository installation does not need to wait for it.

## References

- https://www.hacs.xyz/docs/publish/start/
- https://www.hacs.xyz/docs/publish/integration/
- https://www.hacs.xyz/docs/publish/action/
- https://www.hacs.xyz/docs/publish/include/
- https://github.com/home-assistant/actions#hassfest
