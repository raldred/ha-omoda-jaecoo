"""Bounded Pillow/stdlib AJ-Captcha shape matching; no I/O or retry logic.

Protocol/shape reference: chery-connect-ha/omoda9-ha core/captcha_solver.py.
Image decoding and matching must be called through asyncio.to_thread.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import warnings

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageChops, ImageFilter, UnidentifiedImageError

from .api import CaptchaError

MAX_ENCODED_BYTES = 512_000
MAX_WIDTH = 640
MAX_HEIGHT = 400
MAX_PIXELS = 256_000
MAX_WORK = 20_000_000


def aes_b64(plaintext: str, key: str) -> str:
    """AES ECB/PKCS7 for pointJson and captchaVerification, not passwords."""
    try:
        key_bytes = key.encode("utf-8") if isinstance(key, str) else b""
        plain_bytes = plaintext.encode("utf-8")
    except UnicodeError:
        raise CaptchaError("Backend captcha is unusable.") from None
    if len(key_bytes) not in (16, 24, 32):
        raise CaptchaError("Backend captcha is unusable.")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain_bytes) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key_bytes), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode(
        "ascii"
    )


def _image(encoded: str, mode: str) -> Image.Image:
    if not isinstance(encoded, str) or not 0 < len(encoded) <= MAX_ENCODED_BYTES:
        raise CaptchaError("Backend captcha is unusable.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if (
                    not 0 < width <= MAX_WIDTH
                    or not 0 < height <= MAX_HEIGHT
                    or width * height > MAX_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise CaptchaError("Backend captcha exceeds safe limits.")
                return image.convert(mode)
    except (
        ValueError,
        binascii.Error,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise CaptchaError("Backend captcha is unusable.") from None


def solve_gap(original: str, jigsaw: str) -> int:
    """Binary normalized cross-correlation, matching the upstream outline.

    Integral-image sums normalize each white window; only outline pixels are
    visited for the numerator. Bounds apply before the expensive sliding work.
    Ambiguous or blank shapes fail closed rather than guessing an x coordinate.
    """
    background = _image(original, "RGB")
    piece = _image(jigsaw, "RGBA")
    alpha = piece.getchannel("A").point(lambda value: 255 if value > 128 else 0)
    bbox = alpha.getbbox()
    if bbox is None:
        raise CaptchaError("Backend captcha has no usable outline.")
    x0, _, right, bottom = bbox
    silhouette = alpha.crop(bbox)
    width, height = silhouette.size
    outline = ImageChops.subtract(
        silhouette.filter(ImageFilter.MaxFilter(3)),
        silhouette.filter(ImageFilter.MinFilter(3)),
    )
    edges = [
        (i % width, i // width) for i, value in enumerate(outline.tobytes()) if value
    ]
    bw, bh = background.size
    columns, rows = bw - width + 1, bh - height + 1
    if (
        not edges
        or columns <= width
        or rows <= 0
        or columns * rows * len(edges) > MAX_WORK
    ):
        raise CaptchaError("Backend captcha exceeds safe matching limits.")
    rgb = background.tobytes()
    white = bytearray(
        int(r > 185 and g > 185 and b > 185)
        for r, g, b in zip(rgb[::3], rgb[1::3], rgb[2::3])
    )
    stride = bw + 1
    integral = [0] * (stride * (bh + 1))
    for y in range(bh):
        running = 0
        for x in range(bw):
            running += white[y * bw + x]
            integral[(y + 1) * stride + x + 1] = integral[y * stride + x + 1] + running
    # Store only the best score for each horizontal position: multiple y matches
    # at the same x are not ambiguous for the protocol's fixed y=5.
    scores = [0.0] * columns
    for gy in range(rows):
        for gx in range(max(1, width), columns):
            left, right = gx, gx + width
            top, bottom = gy * stride, (gy + height) * stride
            count = (
                integral[bottom + right]
                - integral[bottom + left]
                - integral[top + right]
                + integral[top + left]
            )
            if count:
                overlap = sum(white[(gy + ey) * bw + gx + ex] for ex, ey in edges)
                score = overlap / math.sqrt(count * len(edges))
                scores[gx] = max(scores[gx], score)
    best_x = max(range(columns), key=scores.__getitem__)
    best = scores[best_x]
    competitor = max(
        (score for x, score in enumerate(scores) if abs(x - best_x) > 2), default=0
    )
    if best < 0.55 or best - competitor < 0.02 or best_x - x0 <= 0:
        raise CaptchaError("Backend captcha could not be solved unambiguously.")
    return best_x - x0


def solve_challenge(rep: dict) -> tuple[str, str, str]:
    """Return token, encrypted point, and verification; all remain ephemeral."""
    fields = ("token", "secretKey", "originalImageBase64", "jigsawImageBase64")
    if not isinstance(rep, dict) or any(
        not isinstance(rep.get(k), str) or not rep[k] for k in fields
    ):
        raise CaptchaError("Backend captcha is unusable.")
    token, secret = rep["token"], rep["secretKey"]
    if len(token) > 4096 or len(secret) > 128:
        raise CaptchaError("Backend captcha is unusable.")
    x = solve_gap(rep["originalImageBase64"], rep["jigsawImageBase64"])
    point = json.dumps({"x": x, "y": 5}, separators=(",", ":"))
    return token, aes_b64(point, secret), aes_b64(token + "---" + point, secret)
