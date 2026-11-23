"""Bounded WoWs container decoding, without packet interpretation or pickle.

The Blowfish chaining matches the existing replay reader: each decrypted
8-byte block is XORed with the previous plaintext block. The first 8 bytes of
the body are the declared output length and encrypted payload length.
Only the observed zero alignment padding (0 through 7 bytes) is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import os
from pathlib import Path
import struct
from typing import BinaryIO, Iterable, Iterator
import zlib

from Cryptodome.Cipher import Blowfish


MAGIC = b"\x12\x32\x34\x11"
WOWS_KEY = bytes.fromhex("29b7c909383f8488fa98ec4e131979fb")
MAX_BLOCKS = 64
MAX_JSON_BLOCK_BYTES = 1024 * 1024
MAX_TOTAL_JSON_BYTES = 4 * 1024 * 1024
DECRYPT_CHUNK_BYTES = 64 * 1024
INFLATE_CHUNK_BYTES = 1024 * 1024


class ReplayContainerError(ValueError):
    """Malformed or unsupported container; code is suitable for diagnostics."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ReplayData:
    engine_data: dict
    decrypted_data: bytes
    extra_data: list
    diagnostics: dict
    game: str = "wows"


def _read_exact(stream: BinaryIO, count: int, context: str) -> bytes:
    parts = []
    remaining = count
    while remaining:
        part = stream.read(remaining)
        if not part:
            raise ReplayContainerError("truncated_container", f"Unexpected EOF reading {context}.")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant {value}.")


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def _json_block(raw: bytes, index: int):
    try:
        return json.loads(
            raw.decode("utf-8"), parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ReplayContainerError("invalid_json", f"Block {index}: {exc}") from exc


def _decrypted_chunks(stream: BinaryIO, length: int) -> Iterator[bytes]:
    cipher = Blowfish.new(WOWS_KEY, Blowfish.MODE_ECB)
    previous_plaintext = 0
    remaining = length
    while remaining:
        size = min(DECRYPT_CHUNK_BYTES, remaining)
        encrypted = _read_exact(stream, size, "encrypted payload")
        decoded = cipher.decrypt(encrypted)
        chained = bytearray(size)
        for offset in range(0, size, 8):
            block = struct.unpack_from("<Q", decoded, offset)[0] ^ previous_plaintext
            struct.pack_into("<Q", chained, offset, block)
            previous_plaintext = block
        remaining -= size
        yield bytes(chained)


def _decompress_bounded(chunks: Iterable[bytes], max_output_bytes: int) -> tuple[bytes, dict]:
    """Enforce the output limit inside zlib, including buffered output.

No unbounded decompress() or flush() call is used. At most one extra output
byte is requested to distinguish an exact-size stream from an overflow.
"""
    decompressor = zlib.decompressobj()
    output = BytesIO()
    produced = 0
    padding = bytearray()
    compressed_bytes = 0

    def append_padding(data: bytes):
        if len(padding) + len(data) > 7 or any(data):
            raise ReplayContainerError("invalid_padding", "Only 0-7 zero bytes may follow the zlib stream.")
        padding.extend(data)

    def inflate(data: bytes) -> bytes:
        nonlocal produced
        capacity = min(INFLATE_CHUNK_BYTES, max_output_bytes - produced + 1)
        try:
            decoded = decompressor.decompress(data, capacity)
        except zlib.error as exc:
            raise ReplayContainerError("invalid_zlib", str(exc)) from exc
        produced += len(decoded)
        if produced > max_output_bytes:
            raise ReplayContainerError("output_limit", f"Decompressed data exceeds {max_output_bytes} bytes.")
        output.write(decoded)
        return decoded

    for chunk in chunks:
        compressed_bytes += len(chunk)
        if decompressor.eof:
            append_padding(chunk)
            continue
        pending = chunk
        while pending:
            inflate(pending)
            if decompressor.eof:
                append_padding(decompressor.unused_data)
                break
            pending = decompressor.unconsumed_tail

    # A max_length call may retain output internally even with no input tail.
    while not decompressor.eof:
        if not inflate(b""):
            raise ReplayContainerError("truncated_zlib", "Compressed stream has no complete end/checksum.")
        if decompressor.eof:
            append_padding(decompressor.unused_data)

    return output.getvalue(), {
        "decompressed_bytes": produced,
        "zlib_stream_bytes": compressed_bytes - len(padding),
        "padding_bytes": len(padding),
        "padding_kind": "zero_alignment" if padding else "none",
        "padding_hex": padding.hex(),
        "zlib_eof": True,
    }


def read_replay(
    path: Path,
    max_file_bytes: int = 64 * 1024 * 1024,
    max_output_bytes: int = 256 * 1024 * 1024,
) -> ReplayData:
    """Read a local WoWs replay with strict lengths and bounded decompression.

JSON blocks, encrypted block alignment, declared body lengths, zlib checksum,
EOF and trailing padding are validated. The output remains raw packet bytes.
"""
    for name, value in (("max_file_bytes", max_file_bytes), ("max_output_bytes", max_output_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
    path = Path(path)
    with path.open("rb") as stream:
        file_size = os.fstat(stream.fileno()).st_size
        if file_size > max_file_bytes:
            raise ReplayContainerError("file_limit", f"File size {file_size} exceeds {max_file_bytes} bytes.")
        if _read_exact(stream, 4, "magic") != MAGIC:
            raise ReplayContainerError("invalid_magic", "Not a WoWs replay signature.")
        block_count = struct.unpack("<I", _read_exact(stream, 4, "block count"))[0]
        if not 1 <= block_count <= MAX_BLOCKS:
            raise ReplayContainerError("block_count_limit", f"Block count must be 1-{MAX_BLOCKS}, got {block_count}.")
        decoded_blocks = []
        block_lengths = []
        total_json_bytes = 0
        for index in range(block_count):
            size = struct.unpack("<I", _read_exact(stream, 4, f"block {index} length"))[0]
            if size > MAX_JSON_BLOCK_BYTES:
                raise ReplayContainerError("header_limit", f"JSON block {index} exceeds {MAX_JSON_BLOCK_BYTES} bytes.")
            total_json_bytes += size
            if total_json_bytes > MAX_TOTAL_JSON_BYTES:
                raise ReplayContainerError("total_header_limit", "Combined JSON block bytes exceed the header budget.")
            if size > file_size - stream.tell():
                raise ReplayContainerError("truncated_container", f"Block {index} extends beyond the file.")
            block_lengths.append(size)
            raw = _read_exact(stream, size, f"JSON block {index}")
            if not raw and index:
                decoded_blocks.append(None)
            else:
                decoded_blocks.append(_json_block(raw, index))
        if not isinstance(decoded_blocks[0], dict):
            raise ReplayContainerError("invalid_engine_data", "First JSON block must be an object.")

        body_offset = stream.tell()
        declared_output, declared_encrypted = struct.unpack("<II", _read_exact(stream, 8, "body lengths"))
        encrypted_bytes = file_size - stream.tell()
        if encrypted_bytes < 8 or encrypted_bytes % 8:
            raise ReplayContainerError("encrypted_alignment", "Encrypted payload must contain complete 8-byte blocks.")
        if declared_encrypted != encrypted_bytes:
            raise ReplayContainerError(
                "encrypted_length_mismatch",
                f"Declared {declared_encrypted} encrypted bytes, file contains {encrypted_bytes}.",
            )
        if declared_output > max_output_bytes:
            raise ReplayContainerError("output_limit", f"Declared output {declared_output} exceeds {max_output_bytes} bytes.")
        decrypted_data, diagnostics = _decompress_bounded(
            _decrypted_chunks(stream, encrypted_bytes), max_output_bytes,
        )
        if len(decrypted_data) != declared_output:
            raise ReplayContainerError(
                "output_length_mismatch", f"Declared {declared_output} output bytes, decoded {len(decrypted_data)}.",
            )
        if stream.read(1):
            raise ReplayContainerError("file_changed", "File length increased while reading.")

    diagnostics.update({
        "container_validation": "passed",
        "packet_semantics_validated": False,
        "file_bytes": file_size,
        "header_block_count": block_count,
        "header_block_bytes": block_lengths,
        "body_offset": body_offset,
        "declared_output_bytes": declared_output,
        "encrypted_bytes": encrypted_bytes,
        "max_file_bytes": max_file_bytes,
        "max_output_bytes": max_output_bytes,
    })
    return ReplayData(
        engine_data=decoded_blocks[0], extra_data=decoded_blocks[1:],
        decrypted_data=decrypted_data, diagnostics=diagnostics,
    )
