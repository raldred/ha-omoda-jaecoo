# HACS installation and releases

## Install through HACS

Omoda / Jaecoo is included in the HACS default catalogue. This is a Home Assistant **custom integration**, not an HA app/add-on.

1. Open **HACS** and search for **Omoda / Jaecoo**.
2. Open the integration and select **Download**.
3. Restart Home Assistant Core.
4. Open **Settings → Devices & services → Add integration → Omoda / Jaecoo**.

[Open Omoda / Jaecoo in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=raldred&repository=ha-omoda-jaecoo&category=integration)

If you're updating an existing installation, keep your current integration entry; you don't need to add it again. Do not install alongside a different integration using the same `omoda_jaecoo` domain.

### Custom repository fallback

If it doesn't appear in HACS search:

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/raldred/ha-omoda-jaecoo` with type **Integration**.
3. Find **Omoda / Jaecoo** in HACS, then follow the download, restart and setup steps above.

The full release doesn't require enabling beta versions. HACS installs from `custom_components/omoda_jaecoo/`; the separate release ZIP is for manual installation, not HACS `zip_release` mode.

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

Both validators must pass on the intended release commit, followed by a published **full (non-prerelease) GitHub release**. See [validation runs](https://github.com/raldred/ha-omoda-jaecoo/actions/workflows/validate.yml). Drafts, prereleases and standalone tags are not sufficient for catalogue submission: HACS hides prereleases unless users opt in to beta versions.

The release workflow deliberately creates only a draft prerelease. After reviewing it, publish it and clear the prerelease flag before requesting catalogue review. A normal `0.x` release is fine; version `1.0.0` is not required.

## Default catalogue

[HACS catalogue PR #11646](https://github.com/hacs/default/pull/11646) was merged on 24 October 2026. HACS advises that new entries can take up to eight hours to appear after merging; use the custom repository fallback above if the listing isn't visible yet.

## References

- https://www.hacs.xyz/docs/publish/start/
- https://www.hacs.xyz/docs/publish/integration/
- https://www.hacs.xyz/docs/publish/action/
- https://www.hacs.xyz/docs/publish/include/
- https://github.com/home-assistant/actions#hassfest
