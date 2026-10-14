"""Synthetic timestamp boundary tests; no recorded device data."""
import importlib.util
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('timestamp_test', ROOT / 'tools/timestamp_test.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class TimestampTests(unittest.TestCase):
    def test_actual_c_conversion_boundaries(self):
        source = (ROOT / 'kernel/cancasexl.c').read_text()
        function = re.search(r'static u64 xl_timestamp_ns\(const u8 \*r\)\n\{.*?\n\}', source, re.S).group()
        cases = [0, 1, 2, 3, (1 << 32) - 1, 1 << 32, (1 << 48) - 1]
        c = '''#include <stdint.h>
#include <assert.h>
typedef uint8_t u8; typedef uint64_t u64;
static uint32_t get_unaligned_le32(const u8 *p) {
 return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24;
}
static uint16_t get_unaligned_le16(const u8 *p) { return p[0] | p[1]<<8; }
''' + function + '\nint main(void) {\n'
        for ticks in cases:
            raw = [0] * 4 + list(ticks.to_bytes(6, 'little')) + [0xFF] * 14
            c += '{ const u8 r[24] = {' + ','.join(map(str, raw)) + '}; '
            c += f'assert(xl_timestamp_ns(r) == {ticks * 125 // 2}ULL); }}\n'
        c += 'return 0; }\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'test.c').write_text(c)
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test')], check=True)

    def test_raw_slot_used_instead_of_software(self):
        value = struct.pack('=6q', 1700000000, 123, 0, 0, 42, 625)
        self.assertEqual(tool.timestamp([(socket.SOL_SOCKET, 65, value)]), 42_000_000_625)

    def test_software_only_rejected(self):
        value = struct.pack('=6q', 1700000000, 123, 0, 0, 0, 0)
        with self.assertRaises(RuntimeError):
            tool.timestamp([(socket.SOL_SOCKET, 65, value)])

    def test_missing_or_truncated_rejected(self):
        for anc in ([], [(socket.SOL_SOCKET, 65, b'123')]):
            with self.assertRaises(RuntimeError):
                tool.timestamp(anc)


if __name__ == '__main__':
    unittest.main()
