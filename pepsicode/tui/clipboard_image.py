"""Read a local Windows clipboard bitmap without launching a process."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import struct
import sys
import zlib

from pepsicode.tui.image_input import image_from_path, image_payload

MAX_DIB_BYTES = 64 * 1024 * 1024


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _dib_to_png(data: bytes) -> bytes:
    if len(data) < 40:
        raise ValueError("Clipboard bitmap is incomplete")
    header_size, width, height, planes, depth, compression = struct.unpack_from("<IiiHHI", data)
    if header_size < 40 or header_size > len(data) or planes != 1 or width <= 0 or height == 0:
        raise ValueError("Unsupported clipboard bitmap")
    if depth not in (24, 32) or compression not in (0, 3):
        raise ValueError("Unsupported clipboard bitmap format")
    if compression == 3:
        masks_offset = 40 if header_size >= 52 else header_size
        if len(data) < masks_offset + 12:
            raise ValueError("Clipboard bitmap has no color masks")
        masks = struct.unpack_from("<III", data, masks_offset)
        pixel_offset = header_size + (12 if header_size == 40 else 0)
    else:
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF)
        pixel_offset = header_size
    if any(mask == 0 for mask in masks):
        raise ValueError("Clipboard bitmap has invalid color masks")
    row_size = ((width * depth + 31) // 32) * 4
    if width * abs(height) > 20_000_000 or pixel_offset + row_size * abs(height) > len(data):
        raise ValueError("Clipboard bitmap is too large or incomplete")

    def channel(pixel: int, mask: int) -> int:
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        return ((pixel & mask) >> shift) * 255 // maximum

    rows = bytearray()
    for y in range(abs(height)):
        source_y = y if height < 0 else abs(height) - 1 - y
        row = memoryview(data)[pixel_offset + source_y * row_size : pixel_offset + (source_y + 1) * row_size]
        rows.append(0)
        for x in range(width):
            pixel = int.from_bytes(row[x * (depth // 8) : (x + 1) * (depth // 8)], "little")
            rows.extend(channel(pixel, mask) for mask in masks)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, abs(height), 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def read_clipboard_image() -> dict[str, str] | None:
    """Return an image payload, or None when the clipboard has no image."""
    if sys.platform != "win32":
        raise ValueError("Image clipboard paste is currently available on Windows")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    if not user32.OpenClipboard(None):
        raise ValueError("Clipboard is busy; try pasting again")
    try:
        for format_id, is_png in ((user32.RegisterClipboardFormatW("PNG"), True), (17, False), (8, False)):
            if not user32.IsClipboardFormatAvailable(format_id):
                continue
            handle = user32.GetClipboardData(format_id)
            if not handle:
                continue
            size = kernel32.GlobalSize(handle)
            if not 0 < size <= MAX_DIB_BYTES:
                raise ValueError("Clipboard image is too large")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                data = ctypes.string_at(pointer, size)
            finally:
                kernel32.GlobalUnlock(handle)
            return image_payload(data if is_png else _dib_to_png(data))
        if user32.IsClipboardFormatAvailable(15):  # CF_HDROP: copied image file
            handle = user32.GetClipboardData(15)
            if handle and shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0) == 1:
                length = shell32.DragQueryFileW(handle, 0, None, 0)
                if 0 < length < 32768:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(handle, 0, buffer, length + 1)
                    if buffer.value.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        return image_from_path(buffer.value, "")
        return None
    finally:
        user32.CloseClipboard()
