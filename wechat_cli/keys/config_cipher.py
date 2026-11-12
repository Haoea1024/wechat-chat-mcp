"""Windows 4.1 Config.Cipher compatibility.

Adapted from maomao3334/wechat-cli-plus (Apache-2.0), commit
75a322dd09c7c498536d10ff6935c5d52fd5fd2b, keys/scanner_windows.py.
Only read memory; validate every candidate against the database page HMAC.
"""
import re
import struct

from .common import verify_enc_key

NAME = b"com.Tencent.WCDB.Config.Cipher"
MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


def candidates(blob):
    decoded = bytes(b ^ MASK[i % len(MASK)] for i, b in enumerate(blob))
    for match in LITERAL.finditer(decoded):
        run = match[1].decode('ascii').lower()
        starts = {0}
        if len(run) > 96:
            starts.update(range(0, len(run) - 63, 32))
            starts.add(len(run) - 64)
        for start in sorted(starts):
            yield bytes.fromhex(run[start:start + 64])


def scan_config_cipher(read, regions, db_files, key_map, remaining):
    def chunks():
        for base, size in regions:
            for offset in range(0, size, 2 * 1024 * 1024):
                length = min(2 * 1024 * 1024 + 64, size - offset)
                data = read(base + offset, length)
                if data:
                    yield base + offset, data

    addresses = set()
    for base, data in chunks():
        start = data.find(NAME)
        while start >= 0:
            addresses.add(base + start)
            start = data.find(NAME, start + 1)
    if not addresses:
        return
    patterns = [struct.pack('<QQ', address, len(NAME)) for address in addresses]
    pages = {salt: page for _, _, _, salt, page in db_files}
    seen_pointers, seen_keys = set(), set()
    for base, data in chunks():
        for pattern in patterns:
            start = data.find(pattern)
            while start >= 0:
                node = read(base + start, 32)
                start = data.find(pattern, start + 1)
                if not node or len(node) != 32:
                    continue
                pointer = struct.unpack_from('<Q', node, 24)[0]
                if pointer in seen_pointers or not 0x10000 <= pointer < 0x800000000000:
                    continue
                seen_pointers.add(pointer)
                obj = read(pointer + 0x88, 24)
                if not obj or len(obj) != 24:
                    continue
                data_ptr, size = struct.unpack_from('<QQ', obj, 8)
                if not 0 < size <= 1024 or not 0x10000 <= data_ptr < 0x800000000000:
                    continue
                blob = read(data_ptr, size)
                if not blob or len(blob) != size:
                    continue
                for key in candidates(blob):
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    for salt in list(remaining):
                        if verify_enc_key(key, pages[salt]):
                            key_map[salt] = key.hex()
                            remaining.remove(salt)
                if not remaining:
                    return
