"""Bounded ZIP parsing; never extract a member to the filesystem."""
import io
import stat
import struct
import zipfile
import zlib
from pathlib import PurePosixPath

from fastapi import HTTPException

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_CENTRAL_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".heic": "image/heic"}


def _reject(message, code=400):
    raise HTTPException(code, message)


def _preflight(data):
    """Bound actual central-directory entries before ZipFile allocates them."""
    position = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if position < 0 or position + 22 > len(data):
        _reject("ZIP central directory is missing")
    end = struct.unpack_from("<4s4H2LH", data, position)
    if position + 22 + end[7] != len(data) or end[1] or end[2] or end[3] != end[4]:
        _reject("Split or malformed ZIP archives are not supported")
    if end[4] > MAX_ARCHIVE_ENTRIES or end[5] > MAX_CENTRAL_BYTES:
        _reject("ZIP metadata exceeds the archive limit", 413)
    cursor, stop, count = end[6], end[6] + end[5], 0
    if stop > position:
        _reject("ZIP directory bounds are invalid")
    while cursor < stop:
        if count >= MAX_ARCHIVE_ENTRIES:
            _reject("ZIP contains too many entries", 413)
        if cursor + 46 > stop or data[cursor:cursor + 4] != b"PK\x01\x02":
            _reject("ZIP directory is malformed")
        lengths = struct.unpack_from("<3H", data, cursor + 28)
        cursor += 46 + sum(lengths)
        count += 1
    if cursor != stop or count != end[4]:
        _reject("ZIP entry count is inconsistent")


def _member_name(info):
    original = info.orig_filename
    name = original.replace("\\", "/")
    if (original != info.filename or name.startswith("/") or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(part in {".", ".."} for part in name.split("/"))):
        _reject("ZIP contains an unsafe member path")
    mode = stat.S_IFMT(info.external_attr >> 16)
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
        _reject("ZIP links and special files are not supported")
    if info.flag_bits & 1:
        _reject("Encrypted ZIP archives are not supported")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        _reject("ZIP must use Stored or Deflate compression")
    return name


def _matches_type(content, extension):
    if extension == ".pdf":
        return content.startswith(b"%PDF-")
    if extension == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if extension == ".heic":
        return content[4:8] == b"ftyp" and content[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
    return False


def prepare_zip(data, max_files, max_file_bytes, max_batch_bytes):
    _preflight(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members, seen, total = [], set(), 0
            for info in archive.infolist():
                name = _member_name(info)
                if info.is_dir():
                    continue
                total += info.file_size
                if total > max_batch_bytes:
                    _reject("Expanded ZIP exceeds the batch byte limit", 413)
                if info.file_size > max(1024 * 1024, info.compress_size * MAX_COMPRESSION_RATIO):
                    _reject("ZIP compression ratio exceeds the safety limit", 413)
                if any(p.startswith(".") or p == "__MACOSX" for p in name.split("/")):
                    continue
                normalized = str(PurePosixPath(name))
                if not normalized or normalized.casefold() in seen:
                    _reject("ZIP contains duplicate member names")
                seen.add(normalized.casefold())
                members.append((info, normalized))
                if len(members) > max_files:
                    _reject("ZIP contains more than 100 document entries", 413)
            if not members:
                _reject("ZIP contains no visible files")
            prepared, errors = [], []
            for index, (info, name) in enumerate(members):
                if len(name) > 255:
                    error = "Member filename exceeds 255 characters"
                elif info.file_size > max_file_bytes:
                    error = "Member exceeds MAX_UPLOAD_BYTES"
                else:
                    # A bounded read validates supported members' CRC and actual size.
                    with archive.open(info) as stream:
                        content = stream.read(max_file_bytes + 1)
                    if len(content) != info.file_size or len(content) > max_file_bytes:
                        _reject("ZIP member size is inconsistent")
                    extension = PurePosixPath(name).suffix.lower()
                    error = ("Member is empty" if not content else
                             "Member file type is not supported" if extension not in MEDIA else
                             "Member bytes do not match its file extension" if not _matches_type(content, extension) else None)
                    if error is None:
                        prepared.append((name, MEDIA[extension], content))
                if error:
                    errors.append({"index": index, "filename": name[:255], "error": error})
            return len(members), prepared, errors
    except HTTPException:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, ValueError, EOFError, OSError, zlib.error):
        _reject("ZIP is damaged or unsupported; no batch was saved")
