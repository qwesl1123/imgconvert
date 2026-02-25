from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename

from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False

bp = Blueprint("imgconvert", __name__, url_prefix="/imgconvert")

# Common formats Pillow can write (you can extend)
OUTPUT_FORMATS = ["PNG", "JPEG", "WEBP", "BMP", "TIFF", "HEIC"]
INPUT_EXT_ALLOWLIST = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".ico", ".heic", ".heif"
}

# Safety limits (tune to your server)
MAX_FILES = 50
MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50MB total upload
MAX_IMAGE_PIXELS = 40_000_000       # helps avoid decompression bomb
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@dataclass
class ConvertResult:
    filename: str
    ok: bool
    message: str


def _flatten_alpha_for_jpeg(im: Image.Image) -> Image.Image:
    # JPEG doesn't support alpha. Flatten onto white.
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
    else:
        im = im.convert("RGB")
    return im


def _save_image_bytes(im: Image.Image, fmt: str, **save_kwargs) -> bytes:
    out = io.BytesIO()
    im.save(out, format=fmt, **save_kwargs)
    return out.getvalue()


def _smallest_lossless_tiff(im: Image.Image) -> bytes:
    """Try multiple lossless TIFF compressions and keep the smallest."""
    candidates = [
        _save_image_bytes(im, "TIFF", compression="tiff_lzw"),
        _save_image_bytes(im, "TIFF", compression="tiff_adobe_deflate"),
    ]

    # PackBits can help for flat-color graphics.
    try:
        candidates.append(_save_image_bytes(im, "TIFF", compression="packbits"))
    except Exception:
        pass

    return min(candidates, key=len)


def _convert_image_bytes(src_bytes: bytes, out_fmt: str, quality: int) -> bytes:
    out_fmt = out_fmt.upper()

    with Image.open(io.BytesIO(src_bytes)) as im:
        # Respect requested quality; do not silently degrade output.
        if out_fmt in ("JPG", "JPEG"):
            base = _flatten_alpha_for_jpeg(im)
            return _save_image_bytes(base, "JPEG", quality=quality, optimize=True, progressive=True)

        if out_fmt == "WEBP":
            return _save_image_bytes(im, "WEBP", quality=quality, method=6)

        if out_fmt == "PNG":
            # Keep PNG visually faithful; only use lossless compression.
            return _save_image_bytes(im, "PNG", optimize=True, compress_level=9)

        if out_fmt == "TIFF":
            # Keep TIFF visually faithful; use best lossless compression.
            return _smallest_lossless_tiff(im)

        if out_fmt == "BMP":
            # BMP is inherently large (mostly uncompressed).
            return _save_image_bytes(im, "BMP")

        if out_fmt == "HEIC":
            if not HEIF_AVAILABLE:
                raise ValueError("HEIC support not available on server")
            return _save_image_bytes(im, "HEIC", quality=quality)

        # Default
        return _save_image_bytes(im, out_fmt)


def _allowed_file(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in INPUT_EXT_ALLOWLIST


def _total_upload_size(files: Iterable) -> int:
    total = 0
    for f in files:
        # Werkzeug FileStorage may not have content_length reliably; measure by seeking.
        pos = f.stream.tell()
        f.stream.seek(0, os.SEEK_END)
        total += f.stream.tell()
        f.stream.seek(pos)
    return total


@bp.route("/", methods=["GET"])
def page():
    return render_template("imgconvert.html", output_formats=OUTPUT_FORMATS)


@bp.route("/convert", methods=["POST"])
def convert():
    """
    Accepts single or multiple files:
      form-data:
        files: (one or many)
        to: PNG|JPEG|WEBP|...
        quality: 1-100 (for JPEG/WEBP)
    Returns:
      - if 1 file: converted file download
      - if >1 file: zip download
    """
    to_fmt = (request.form.get("to") or "WEBP").upper()
    if to_fmt not in OUTPUT_FORMATS and to_fmt not in ("JPG",):
        return "Unsupported output format", 400

    if to_fmt == "HEIC" and not HEIF_AVAILABLE:
        return "HEIC output is not enabled on this server", 400

    try:
        quality = int(request.form.get("quality") or "85")
        quality = max(1, min(100, quality))
    except Exception:
        quality = 85

    files = request.files.getlist("files")
    if not files:
        return "No files uploaded", 400

    if len(files) > MAX_FILES:
        return f"Too many files (max {MAX_FILES})", 400

    total_size = _total_upload_size(files)
    if total_size > MAX_TOTAL_BYTES:
        return f"Total upload too large (max {MAX_TOTAL_BYTES // (1024*1024)}MB)", 400

    # Convert all; collect results
    converted = []
    results: list[ConvertResult] = []

    for f in files:
        original_name = f.filename or "unnamed"
        safe_name = secure_filename(original_name) or "file"
        if not _allowed_file(safe_name):
            results.append(ConvertResult(original_name, False, "Unsupported input type"))
            continue

        try:
            f.stream.seek(0)
            src_bytes = f.read()
            out_bytes = _convert_image_bytes(src_bytes, to_fmt, quality)

            stem = Path(safe_name).stem
            out_ext = ".jpg" if to_fmt in ("JPEG", "JPG") else f".{to_fmt.lower()}"
            out_name = f"{stem}{out_ext}"

            converted.append((out_name, out_bytes))
            results.append(ConvertResult(original_name, True, f"Converted → {to_fmt}"))
        except Exception as e:
            results.append(ConvertResult(original_name, False, f"Failed: {e}"))

    # If only one successful conversion, return the file directly
    ok_items = [(n, b) for (n, b) in converted]
    if len(ok_items) == 1 and len(files) == 1:
        name, data = ok_items[0]
        return send_file(
            io.BytesIO(data),
            as_attachment=True,
            download_name=name,
            mimetype="application/octet-stream",
        )

    # Otherwise return a zip (also include a results.txt)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in ok_items:
            z.writestr(name, data)

        report_lines = []
        for r in results:
            status = "OK" if r.ok else "ERR"
            report_lines.append(f"{status} - {r.filename} - {r.message}")
        z.writestr("results.txt", "\n".join(report_lines))

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        as_attachment=True,
        download_name=f"converted_{to_fmt.lower()}.zip",
        mimetype="application/zip",
    )
