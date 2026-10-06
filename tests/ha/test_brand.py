"""Local vendor-owned app icons and HA's native brand fallbacks, offline.

Artwork is adapted from the supplied Android app's 192px icon, not MIT code.
Only dimensions are changed (Lanczos upscaling); no logo is invented or cropped.
"""

import struct
import zlib
from pathlib import Path

import pytest
from homeassistant.components.brands import BrandsIntegrationView, _read_brand_file
from homeassistant.components.brands.const import ALLOWED_IMAGES
from homeassistant.loader import async_get_integration

BRAND_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "omoda_jaecoo" / "brand"
)


@pytest.mark.parametrize("filename,size", [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_png_dimensions_and_metadata(filename, size):
    """Ship only valid, opaque RGB PNGs without copied app metadata."""
    data = (BRAND_DIR / filename).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    chunks = []
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert zlib.crc32(chunk_type + payload) & 0xFFFFFFFF == crc
        chunks.append(chunk_type)
        if chunk_type == b"IHDR":
            assert struct.unpack(">IIBBBBB", payload) == (size, size, 8, 2, 0, 0, 0)
        offset += length + 12
    assert offset == len(data)
    assert chunks[0] == b"IHDR"
    assert chunks[-1] == b"IEND"
    assert b"IDAT" in chunks
    assert set(chunks) <= {b"IHDR", b"IDAT", b"IEND"}


def test_only_native_icon_assets_are_shipped():
    """Square app artwork doubles as the logo through HA's native fallback."""
    assert {path.name for path in BRAND_DIR.iterdir()} == {
        "icon.png",
        "icon@2x.png",
        "NOTICE.md",
    }
    for filename in ALLOWED_IMAGES:
        result = _read_brand_file(BRAND_DIR, filename)
        assert result is not None, filename
        assert result.startswith(b"\x89PNG\r\n\x1a\n")


async def test_ha_discovers_and_serves_local_brand_without_cdn(hass):
    """Real installed HA recognizes the directory and serves local PNG bytes."""
    integration = await async_get_integration(hass, "omoda_jaecoo")
    assert integration.has_branding
    view = BrandsIntegrationView(hass)
    for filename in ALLOWED_IMAGES:
        response = await view._serve_from_custom_integration("omoda_jaecoo", filename)
        assert response is not None
        assert response.content_type == "image/png"
        assert response.body == _read_brand_file(BRAND_DIR, filename)
