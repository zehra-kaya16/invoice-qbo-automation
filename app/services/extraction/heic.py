"""HEIC conversion shared by AI input and the browser preview.

The stored source is never replaced. Multi-image containers fail explicitly.
"""
import io
from PIL import Image, ImageOps, UnidentifiedImageError
from app.core.config import settings


class HEICImageError(ValueError):
    pass


BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}


def is_heic(content: bytes) -> bool:
    if not isinstance(content, bytes) or len(content) < 12 or content[4:8] != b"ftyp":
        return False
    # A bounded ISO BMFF ftyp check, including compatible brands.
    end = min(int.from_bytes(content[:4], "big"), len(content), 256)
    return content[8:12] in BRANDS or any(content[i:i + 4] in BRANDS for i in range(16, end - 3, 4))


def heic_to_png(content: bytes) -> bytes:
    if not is_heic(content):
        raise HEICImageError("Invalid HEIC image; upload a readable HEIC, PNG, JPG or PDF")
    if len(content) > settings.max_upload_bytes:
        raise HEICImageError("HEIC source exceeds MAX_UPLOAD_BYTES")
    try:
        # Importing this module registers the Pillow plugin once per process.
        from pillow_heif import HeifImagePlugin  # noqa: F401
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "HEIF":
                raise HEICImageError("Source is not a readable HEIC image")
            if getattr(image, "n_frames", 1) != 1:
                raise HEICImageError("HEIC contains multiple images; upload them separately or as a multi-page PDF")
            if image.width * image.height > settings.max_heic_pixels:
                raise HEICImageError("HEIC image exceeds MAX_HEIC_PIXELS")
            oriented = ImageOps.exif_transpose(image)
            if "A" in oriented.getbands():
                rgba = oriented.convert("RGBA")
                normalized = Image.new("RGB", rgba.size, "white")
                normalized.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
            else:
                normalized = oriented.convert("RGB")
            try:
                # Do not forward EXIF/GPS or other source metadata to the AI/preview.
                normalized.info.clear()
                buffer = io.BytesIO()
                normalized.save(buffer, format="PNG")
                result = buffer.getvalue()
            finally:
                normalized.close()
                if oriented is not image:
                    oriented.close()
            if len(result) > settings.max_upload_bytes:
                raise HEICImageError("Converted HEIC exceeds MAX_UPLOAD_BYTES; reduce image size before uploading")
            return result
    except HEICImageError:
        raise
    except ImportError:
        raise HEICImageError("HEIC decoder is unavailable; install the project requirements and restart API/workers") from None
    except (OSError, ValueError, SyntaxError, RuntimeError, EOFError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HEICImageError("HEIC image could not be decoded; upload a readable HEIC, PNG, JPG or PDF") from None
