"""Strict parser-only UTF-8 views; all public evidence refers to original bytes."""
from __future__ import annotations

from array import array
from bisect import bisect_right
import codecs
from dataclasses import dataclass

INPUT_POLICY_VERSION = '1'
POLICIES = ('utf8-overlay', 'native')
# Only stateless, round-trippable source codecs have an offset contract here.
LEGACY_CODECS = {'cp932', 'shift_jis', 'cp1252', 'iso8859-1', 'ascii', 'utf-8'}


def encoding_name(name: str) -> str:
    canonical = codecs.lookup(name).name
    if canonical not in LEGACY_CODECS:
        raise ValueError(f'Unsupported parser fallback encoding: {name}')
    return canonical


def policy_version(version: str, encoding: str, policy: str) -> str:
    if policy not in POLICIES:
        raise ValueError(f'Unsupported parser input policy: {policy}')
    return f'{version};input={policy}:{INPUT_POLICY_VERSION}:{encoding_name(encoding)}'


@dataclass
class SourceBuffer:
    original: bytes
    parser: bytes
    encoding: str
    # Character boundaries, compact native arrays, absent for identical UTF-8.
    parser_starts: array | None
    original_starts: array | None
    line_starts: tuple[int, ...]

    @classmethod
    def create(cls, raw: bytes, fallback: str) -> 'SourceBuffer':
        from .discovery import compute_line_byte_offsets
        codec = encoding_name(fallback)
        bom = 0
        for marker, name in (
            (b'\xff\xfe\x00\x00', 'utf-32-le'),
            (b'\x00\x00\xfe\xff', 'utf-32-be'),
            (b'\xef\xbb\xbf', 'utf-8'),
            (b'\xff\xfe', 'utf-16-le'),
            (b'\xfe\xff', 'utf-16-be'),
        ):
            if raw.startswith(marker):
                bom, codec = len(marker), name
                break
        else:
            try:
                raw.decode('utf-8', errors='strict')
                codec = 'utf-8'
            except UnicodeDecodeError:
                pass
        text = raw[bom:].decode(codec, errors='strict')
        # Never silently switch codecs or invent a reversible source view.
        if text.encode(codec, errors='strict') != raw[bom:]:
            raise ValueError(f'Non-round-trippable source encoding: {codec}')
        normalized = text.encode('utf-8')
        ps = os = None
        if normalized != raw:
            ps, os = array('Q', [0]), array('Q', [bom])
            p, o = 0, bom
            for ch in text:
                p += len(ch.encode('utf-8'))
                o += len(ch.encode(codec))
                ps.append(p)
                os.append(o)
        lines = compute_line_byte_offsets(raw, codec)
        if raw and raw.endswith('\n'.encode(codec)):
            lines = (*lines, len(raw))
        return cls(raw, normalized, codec, ps, os, lines)

    def original_offset(self, offset: int) -> int:
        offset = max(0, min(offset, len(self.parser)))
        if self.parser_starts is None:
            return offset
        # Interior bytes map to the start of their original character.
        return int(self.original_starts[bisect_right(self.parser_starts, offset) - 1])

    def position(self, offset: int) -> tuple[int, int]:
        line = max(0, bisect_right(self.line_starts, offset) - 1)
        return line + 1, offset - self.line_starts[line] + 1

    def text_slice(self, start: int, end: int) -> str:
        text = self.original[start:end].decode(self.encoding, errors='strict')
        return text.removeprefix('\ufeff') if start == 0 else text
