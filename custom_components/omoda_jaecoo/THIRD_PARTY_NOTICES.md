# Third-party notices

The EU password encoding, BFF header/signature format, token-refresh grant, TSP realtime request signature, and endpoint configuration in `custom_components/omoda_jaecoo/{api,commands,otp,captcha}.py` were implemented with reference to:

- Project: chery-connect-ha/omoda9-ha (originally Caslinovich/omoda9-ha)
- Source: https://github.com/chery-connect-ha/omoda9-ha
- Revision: 7d80cd6a7215168f58d147cbd475c82e52cd3944
- Relevant files: `core/prova_token.py`, `core/omoda_auth.py`, `core/tsp_sign.py`, `core/wake.py`, `core/provision.py`, `core/commands.py`, `core/permessi.py`, `core/login_omoda.py`, `core/captcha_solver.py` under `custom_components/omoda9/`.

No mobile-app archive, decompiled source, certificate or client private key is included. The upstream software license does not grant rights to redistribute third-party app binaries or certificate material.

Read-only telemetry names and candidate unit mappings were also reviewed against the upstream `sensor.py`, `binary_sensor.py`, `device_tracker.py` and `coordinator.py`. Model-specific uncertainties are documented in README.md; SDK-only schedule/depth query availability is not represented as live verification.

The captcha outline-matching approach follows the upstream solver, reimplemented with Pillow/standard Python and bounded image/work limits instead of NumPy/OpenCV. Code requests are explicit, single-attempt operations. The `phonenumbers` dependency is obtained separately from PyPI under its own Apache-2.0 license; its source/database is not vendored here.

## Manufacturer branding (not MIT-licensed)

The PNGs in `custom_components/omoda_jaecoo/brand/` adapt two brand images supplied by the project owner for this integration: a square OJ monogram with OMODA/JAECOO lettering and a larger wordmark. The current assets are not extracted from a mobile-app binary. White background was converted to transparency, with black/white glyph variants for light/dark themes; resizing preserves the supplied designs and removes source image metadata. See `brand/NOTICE.md` in the component for details.

The artwork and trademarks remain their respective owners' property and are **excluded from this repository's MIT software license**. They identify the products supported by this unofficial integration and do not imply affiliation, endorsement or an official integration. The contributors do not grant manufacturer trademark or artwork redistribution rights.

Earlier development commits contain the previously supplied manufacturer app icon. Its manufacturer ownership and exclusion from MIT also apply to that historical artwork. No app archive, decompiled tree, account capture or private key is part of the repository or release.

## Upstream MIT license

Copyright (c) 2026 Caslinovich

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
