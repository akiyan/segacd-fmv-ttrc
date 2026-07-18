import struct
import tempfile
import unittest
from pathlib import Path

import player_constants
import ttrc_routing


def make_header(*, mode=0, fps=30, features=None):
    if features is None:
        features = ttrc_routing.FEATURE_COLD_RUNS | ttrc_routing.FEATURE_FIXED_N2
    tcols = 32 if mode == 0 else 40
    trows = 28
    cells = tcols * trows
    frames = 2714
    prefix = struct.pack(
        ">4s9H4LBB3L6H",
        b"TTRC", ttrc_routing.VERSION, frames, tcols, trows, cells,
        1400, 1, ttrc_routing.FRAME_SECTORS, 13,
        12416, ttrc_routing.routing_sector_count(frames), 194, 12416,
        mode, 0, 2, 14, 1, 2 if fps >= 24 else 4,
        444 if fps == 30 else 888, fps, 0, 30, features,
    )
    sector = prefix + bytes(128) + bytes(player_constants.SECTOR - 192)
    return player_constants.stamp_header_sector(sector)


class PlayerConstantsTest(unittest.TestCase):
    def test_sonic_h32_current_values(self):
        values = player_constants.parse_header_sector(make_header())
        self.assertEqual(values.bmbytes, 112)
        self.assertEqual(values.col0, 0)
        self.assertEqual(values.row0, 0)
        self.assertEqual(values.vbudget, 2800)
        self.assertEqual(values.audio_bytes, 444)
        self.assertEqual((values.sec_num, values.sec_mod), (1001, 400))
        self.assertEqual((values.sec_base, values.sec_rem), (2, 201))
        self.assertEqual(values.pump_mask, 0x03FF)
        self.assertEqual(values.wave_pump_mask, 0x01FF)

    def test_h40_15fps_uses_legacy_sector_accumulator(self):
        values = player_constants.parse_header_sector(
            make_header(mode=1, fps=15, features=ttrc_routing.FEATURE_COLD_RUNS))
        self.assertEqual(values.screen_cols, 40)
        self.assertEqual(values.vbudget, 3400)
        self.assertEqual(values.audio_bytes, 888)
        self.assertEqual((values.sec_num, values.sec_mod), (75, 15))
        self.assertEqual((values.sec_base, values.sec_rem), (5, 0))
        self.assertEqual(values.pump_mask, 0x003F)
        self.assertEqual(values.wave_pump_mask, 0x00FF)

    def test_changed_fixed_header_rejects_stale_signature(self):
        sector = bytearray(make_header())
        sector[54:56] = struct.pack(">H", 445)
        with self.assertRaisesRegex(ValueError, "signature"):
            player_constants.parse_header_sector(bytes(sector))

    def test_generation_is_deterministic_and_preserves_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            header = Path(td) / "HEADER.DAT"
            output = Path(td) / "player_constants.inc"
            header.write_bytes(make_header())
            player_constants.generate_include(header, output)
            first = output.read_bytes()
            first_mtime = output.stat().st_mtime_ns
            player_constants.generate_include(header, output)
            self.assertEqual(output.read_bytes(), first)
            self.assertEqual(output.stat().st_mtime_ns, first_mtime)
            text = first.decode()
            self.assertIn(".equ PC_AUDIO_BYTES, 0x01BC", text)
            self.assertIn(".equ PC_SEC_REM, 0x00C9", text)


if __name__ == "__main__":
    unittest.main()
