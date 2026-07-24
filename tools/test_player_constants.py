import struct
import tempfile
import unittest
from pathlib import Path

import player_constants
import ttrc_routing
import pattern_supply
import av_config


def make_header(*, mode=0, fps=30, features=None, audio_bytes=None, audio_fd=0x345,
                supply_counts=(0, 0, 0), pool=1400, base=1,
                tcols=None, trows=28):
    if features is None:
        features = ttrc_routing.FEATURE_COLD_RUNS
        if av_config.uses_fixed_n_cadence(fps):
            features |= ttrc_routing.FEATURE_FIXED_N
    if tcols is None:
        tcols = 32 if mode == 0 else 40
    cells = tcols * trows
    frames = 2714
    if audio_bytes is None:
        audio_bytes = 736 if fps == 30 else 1472
    prefix = struct.pack(
        ">4s9H4LBB3L6H",
        b"TTRC", ttrc_routing.VERSION, frames, tcols, trows, cells,
        pool, base, ttrc_routing.FRAME_SECTORS, 13,
        12416, ttrc_routing.routing_sector_count(frames), 194, 12416,
        mode, 0, 2, 14, 1, av_config.vsync_n_for_fps(fps),
        audio_bytes, fps, audio_fd, 30, features,
    )
    sector = bytearray(prefix + bytes(128) + bytes(player_constants.SECTOR - 192))
    if features & ttrc_routing.FEATURE_PATTERN_SUPPLY:
        wr0, wr1, dic = supply_counts
        player_constants.PATTERN_SUPPLY_STRUCT.pack_into(
            sector, player_constants.PATTERN_SUPPLY_OFFSET,
            player_constants.PATTERN_SUPPLY_MAGIC,
            player_constants.PATTERN_SUPPLY_VERSION, 0,
            wr0, wr1, dic,
            (wr0 + 63) // 64, (wr1 + 63) // 64, (dic + 63) // 64,
        )
    return player_constants.stamp_header_sector(sector)


class PlayerConstantsTest(unittest.TestCase):
    def test_pool_may_fill_up_to_the_movie_name_table(self):
        # The HUD font now lives in the 0xD000-0xDFFF gap, so the pool may run
        # right up to the first movie name table at tile 1536 (base 1 + 1535).
        values = player_constants.parse_header_sector(make_header(pool=1535))
        self.assertEqual(values.pool, 1535)
        self.assertEqual(values.font_vtile, 0xD000 // 32)
        self.assertEqual(values.font_addr, 0xD000)

        with self.assertRaisesRegex(ValueError, "overlaps"):
            player_constants.parse_header_sector(make_header(pool=1536))

    def test_sonic_h32_current_values(self):
        values = player_constants.parse_header_sector(make_header())
        self.assertEqual(values.bmbytes, 112)
        self.assertEqual(values.col0, 0)
        self.assertEqual(values.row0, 0)
        self.assertEqual(values.vbudget, 2800)
        self.assertEqual(values.audio_bytes, 736)
        self.assertEqual(values.audio_fd, 0x345)
        self.assertEqual((values.sec_num, values.sec_mod), (1001, 400))
        self.assertEqual((values.sec_base, values.sec_rem), (2, 201))
        self.assertEqual(values.pump_mask, 0x03FF)
        self.assertEqual(values.wave_pump_mask, 0x01FF)

    def test_h40_15fps_uses_fixed_n4_sector_accumulator(self):
        values = player_constants.parse_header_sector(
            make_header(
                mode=1, fps=15,
                features=(ttrc_routing.FEATURE_COLD_RUNS
                          | ttrc_routing.FEATURE_FIXED_N)))
        self.assertEqual(values.screen_cols, 40)
        self.assertEqual(values.vsync_n, 4)
        self.assertEqual(values.vbudget, 3400)
        self.assertEqual(values.audio_bytes, 1472)
        self.assertEqual((values.sec_num, values.sec_mod), (1001, 200))
        self.assertEqual((values.sec_base, values.sec_rem), (5, 1))
        self.assertEqual(values.pump_mask, 0x003F)
        self.assertEqual(values.wave_pump_mask, 0x00FF)
        self.assertEqual(values.prg_buf_cap_patterns, 382 * 1024 // 32)
        self.assertEqual(values.prg_delivery_cap_patterns, 422 * 1024 // 32)
        self.assertEqual(values.jitter_headroom_kb, 40)

    def test_h40_centers_a_36x25_stream_without_expanding_its_grid(self):
        values = player_constants.parse_header_sector(
            make_header(mode=1, tcols=36, trows=25))
        self.assertEqual((values.tcols, values.trows, values.cells), (36, 25, 900))
        self.assertEqual((values.screen_cols, values.screen_rows), (40, 28))
        self.assertEqual((values.col0, values.row0), (2, 1))
        self.assertEqual(values.bmbytes, 113)

    def test_prg_jitter_constants_follow_content_fps(self):
        expected = {
            15: (382, 40),
            24: (397, 25),
            30: (402, 20),
        }
        for fps, (normal_kb, jitter_kb) in expected.items():
            with self.subTest(fps=fps):
                values = player_constants.parse_header_sector(
                    make_header(fps=fps))
                self.assertEqual(
                    values.prg_buf_cap_patterns, normal_kb * 1024 // 32)
                self.assertEqual(
                    values.prg_delivery_cap_patterns, 422 * 1024 // 32)
                self.assertEqual(values.jitter_headroom_kb, jitter_kb)

    def test_changed_fixed_header_rejects_stale_signature(self):
        sector = bytearray(make_header())
        sector[54:56] = struct.pack(">H", 445)
        with self.assertRaisesRegex(ValueError, "signature"):
            player_constants.parse_header_sector(bytes(sector))

    def test_adpcm_derives_control_and_table_sizes(self):
        values = player_constants.parse_header_sector(make_header(
            features=(ttrc_routing.FEATURE_COLD_RUNS
                      | ttrc_routing.FEATURE_FIXED_N),
            audio_bytes=736,
        ))
        self.assertEqual(values.audio_bytes, 736)
        self.assertEqual(values.audio_control_bytes, 372)
        self.assertEqual(values.adpcm_table_sectors, 5)

    def test_removed_audio_feature_bit_is_reserved(self):
        with self.assertRaisesRegex(ValueError, "reserved feature bits"):
            player_constants.parse_header_sector(make_header(
                features=(ttrc_routing.FEATURE_COLD_RUNS | 0x0004),
            ))

    def test_pattern_supply_extension(self):
        values = player_constants.parse_header_sector(make_header(
            features=(ttrc_routing.FEATURE_COLD_RUNS
                      | ttrc_routing.FEATURE_FIXED_N
                      | ttrc_routing.FEATURE_PATTERN_SUPPLY
                      | ttrc_routing.FEATURE_DICBUF_INDEXED_RUNS),
            supply_counts=(880, 879, 256),
        ))
        self.assertEqual(values.wr0_patterns, pattern_supply.WORD_BUF_PATTERNS)
        self.assertEqual(values.wr1_patterns, 879)
        self.assertEqual(values.dic_patterns, pattern_supply.DIC_BUF_PATTERNS)
        self.assertEqual((values.wr0_sectors, values.wr1_sectors, values.dic_sectors),
                         (14, 14, 4))

    def test_pattern_supply_uses_fixed_n4_and_low_rate_polls_at_15fps(self):
        values = player_constants.parse_header_sector(make_header(
            mode=1,
            fps=15,
            features=(ttrc_routing.FEATURE_COLD_RUNS
                      | ttrc_routing.FEATURE_FIXED_N
                      | ttrc_routing.FEATURE_PATTERN_SUPPLY
                      | ttrc_routing.FEATURE_DICBUF_INDEXED_RUNS),
            supply_counts=(880, 880, 256),
        ))
        self.assertEqual((values.sec_num, values.sec_mod), (1001, 200))
        self.assertEqual(values.pump_mask, 0x003F)
        self.assertEqual(values.wave_pump_mask, 0x00FF)
        self.assertEqual(values.wr0_patterns, 880)
        self.assertEqual(values.wr1_patterns, 880)

    def test_fixed_n_rejects_a_stale_vsync_hint(self):
        header = bytearray(make_header(fps=15))
        header[52:54] = struct.pack(">H", 2)
        header = player_constants.stamp_header_sector(header)
        with self.assertRaisesRegex(ValueError, "fixed-N header"):
            player_constants.parse_header_sector(header)

    def test_pattern_supply_requires_indexed_dicbuf_feature(self):
        with self.assertRaisesRegex(ValueError, "indexed DicBuf"):
            player_constants.parse_header_sector(make_header(
                features=(ttrc_routing.FEATURE_COLD_RUNS
                          | ttrc_routing.FEATURE_FIXED_N
                          | ttrc_routing.FEATURE_PATTERN_SUPPLY),
                supply_counts=(1, 1, 1),
            ))

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
            self.assertIn(".equ PC_AUDIO_BYTES, 0x02E0", text)
            self.assertIn(".equ PC_AUDIO_CONTROL_BYTES, 0x0174", text)
            self.assertIn(".equ PC_AUDIO_FD, 0x0345", text)
            self.assertIn(".equ PC_SEC_REM, 0x00C9", text)
            self.assertIn(
                ".equ PC_PRG_BUF_CAP_PATTERNS, 0x3240", text)
            self.assertIn(
                ".equ PC_PRG_DELIVERY_CAP_PATTERNS, 0x34C0", text)
            self.assertIn(".equ PC_JITTER_HEADROOM_KB, 0x0014", text)


if __name__ == "__main__":
    unittest.main()
