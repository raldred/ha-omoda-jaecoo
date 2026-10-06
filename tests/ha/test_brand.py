"""User-supplied manufacturer artwork and native HA brand serving, offline.

The square monogram and cropped wordmark are distinct supplied designs, not MIT
code. Black/white glyphs share alpha masks; no replacement lettering is drawn.
"""

import struct
import zlib
from pathlib import Path

import pytest
from homeassistant.components.brands import BrandsIntegrationView, _read_brand_file
from homeassistant.components.brands.const import ALLOWED_IMAGES
from homeassistant.loader import async_get_integration
from PIL import Image

BRAND_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "omoda_jaecoo" / "brand"
)
ASSET_SIZES = {
    f"{theme}{kind}{scale}.png": dimensions
    for theme in ("", "dark_")
    for kind, sizes in (
        ("icon", ((256, 256), (512, 512))),
        ("logo", ((256, 91), (512, 182))),
    )
    for scale, dimensions in zip(("", "@2x"), sizes, strict=True)
}


@pytest.mark.parametrize("filename,dimensions", ASSET_SIZES.items())
def test_brand_png_dimensions_and_metadata(filename, dimensions):
    """Ship valid RGBA PNGs without copied image or application metadata."""
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
            assert struct.unpack(">IIBBBBB", payload) == (*dimensions, 8, 6, 0, 0, 0)
        offset += length + 12
    assert offset == len(data)
    assert chunks[0] == b"IHDR"
    assert chunks[-1] == b"IEND"
    assert b"IDAT" in chunks
    assert set(chunks) <= {b"IHDR", b"IDAT", b"IEND"}


@pytest.mark.parametrize("kind", ["icon", "logo"])
@pytest.mark.parametrize("scale", ["", "@2x"])
def test_theme_variants_preserve_shape_and_clear_space(kind, scale):
    """Theme inversion changes only glyph color, with no clipping or white box."""
    with (
        Image.open(BRAND_DIR / f"{kind}{scale}.png") as light,
        Image.open(BRAND_DIR / f"dark_{kind}{scale}.png") as dark,
    ):
        assert light.mode == dark.mode == "RGBA"
        assert light.size == dark.size
        for channel in light.split()[:3]:
            assert channel.getextrema() == (0, 0)
        for channel in dark.split()[:3]:
            assert channel.getextrema() == (255, 255)
        alpha = light.getchannel("A")
        assert alpha.tobytes() == dark.getchannel("A").tobytes()
        assert alpha.getextrema() == (0, 255)
        assert len(set(alpha.tobytes())) > 2  # Preserve antialiased contours.
        left, top, right, bottom = alpha.getbbox()
        assert 0 < left < right < light.width
        assert 0 < top < bottom < light.height
        if kind == "icon":
            # The original 400px square's artwork bounds are (64,40,328,328).
            expected = (0.16, 0.10, 0.82, 0.82)
            actual = (
                left / light.width,
                top / light.height,
                right / light.width,
                bottom / light.height,
            )
            assert actual == pytest.approx(expected, abs=0.011)
        else:
            # Trim only whitespace: 880x312 source crop plus rounding to pixels.
            assert light.width / light.height == pytest.approx(880 / 312, rel=0.003)


def test_only_native_brand_assets_are_shipped():
    """All native HA requests resolve directly to the correct theme and scale."""
    assert {path.name for path in BRAND_DIR.iterdir()} == {
        *ASSET_SIZES,
        "NOTICE.md",
    }
    assert set(ASSET_SIZES) == ALLOWED_IMAGES
    for filename in ALLOWED_IMAGES:
        assert (
            _read_brand_file(BRAND_DIR, filename) == (BRAND_DIR / filename).read_bytes()
        )


def test_native_brand_fallbacks_remain_available(tmp_path):
    """HA can still fall back locally if a theme or logo variant is absent."""
    for filename in ("icon.png", "icon@2x.png", "dark_icon.png", "dark_icon@2x.png"):
        (tmp_path / filename).write_bytes((BRAND_DIR / filename).read_bytes())
    for requested, expected in {
        "logo.png": "icon.png",
        "logo@2x.png": "icon.png",
        "dark_logo.png": "dark_icon.png",
        "dark_logo@2x.png": "dark_icon@2x.png",
    }.items():
        assert (
            _read_brand_file(tmp_path, requested) == (tmp_path / expected).read_bytes()
        )
    for path in tmp_path.iterdir():
        if path.name != "icon.png":
            path.unlink()
    for filename in ALLOWED_IMAGES:
        assert (
            _read_brand_file(tmp_path, filename) == (tmp_path / "icon.png").read_bytes()
        )


async def test_ha_discovers_and_serves_local_brand_without_cdn(hass):
    """Real installed HA recognizes the directory and serves local PNG bytes."""
    integration = await async_get_integration(hass, "omoda_jaecoo")
    assert integration.has_branding
    view = BrandsIntegrationView(hass)
    for filename in ALLOWED_IMAGES:
        response = await view._serve_from_custom_integration("omoda_jaecoo", filename)
        assert response is not None
        assert response.content_type == "image/png"
        assert response.body == (BRAND_DIR / filename).read_bytes()
