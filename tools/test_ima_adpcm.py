import unittest

import numpy as np

import ima_adpcm


class ImaAdpcmTest(unittest.TestCase):
    def test_checkpoint_chunks_equal_continuous_decode(self):
        rng = np.random.default_rng(20260719)
        pcm = rng.integers(-32768, 32768, size=736 * 7, dtype=np.int16)
        state = ima_adpcm.State()
        chunks = []
        continuous_codes = bytearray()
        checkpoints = []
        for start in range(0, len(pcm), 736):
            part = pcm[start:start + 736]
            checkpoints.append(state)
            chunk, state = ima_adpcm.encode_chunk(part, state)
            chunks.append(chunk)
            continuous_codes += chunk[ima_adpcm.CHECKPOINT_BYTES:]

        continuous, final_continuous = ima_adpcm.decode_samples(
            bytes(continuous_codes), len(pcm))
        independent = []
        final_independent = None
        for chunk in chunks:
            decoded, final_independent = ima_adpcm.decode_chunk(chunk, 736)
            independent.append(decoded)
        np.testing.assert_array_equal(continuous, np.concatenate(independent))
        self.assertEqual(final_continuous, final_independent)

    def test_checkpoint_records_continuous_state(self):
        pcm = np.arange(-2048, 2048, dtype=np.int16)
        first, state = ima_adpcm.encode_chunk(pcm[:2048])
        second, _ = ima_adpcm.encode_chunk(pcm[2048:], state)
        predictor, index, reserved = ima_adpcm.CHECKPOINT.unpack_from(second)
        self.assertEqual((predictor, index), (state.predictor, state.index))
        self.assertEqual(reserved, 0)
        self.assertNotEqual(first[:4], second[:4])

    def test_full_table_layout_and_size(self):
        blob = ima_adpcm.full_tables()
        self.assertEqual(len(blob), 8800)
        self.assertEqual(len(blob[:ima_adpcm.FULL_INDEX_BYTES]), 2848)
        self.assertEqual(
            len(blob[ima_adpcm.FULL_INDEX_BYTES:
                     ima_adpcm.FULL_INDEX_BYTES + ima_adpcm.FULL_DELTA_BYTES]),
            5696)
        self.assertEqual(blob[-256:], ima_adpcm.output_lut())

    def test_sign_magnitude_avoids_stop_marker(self):
        pcm = np.array([-32768, -32512, -32256, -256, 0, 256, 32512], np.int16)
        converted = ima_adpcm.pcm16_to_sign_magnitude(pcm)
        self.assertNotIn(0xFF, converted)
        self.assertEqual(converted[4], 0)
        self.assertEqual(converted[-1], 0x7F)

    def test_sign_magnitude_playback_round_trip(self):
        pcm = np.array(
            [-32768, -32512, -32256, -256, 0, 256, 32512, 32767], np.int16)
        encoded = ima_adpcm.pcm16_to_sign_magnitude(pcm)
        decoded = ima_adpcm.sign_magnitude_to_pcm16(encoded)
        np.testing.assert_array_equal(
            decoded,
            np.array([-32256, -32256, -32256, -256, 0, 256, 32512, 32512],
                     np.int16))

    def test_retime_pcm_s16_has_exact_length_and_endpoints(self):
        source = np.array([-30000, -10000, 10000, 30000], np.int16)
        retimed = ima_adpcm.retime_pcm_s16(source, 11)
        self.assertEqual(len(retimed), 11)
        self.assertEqual((int(retimed[0]), int(retimed[-1])), (-30000, 30000))

    def test_shared_chunk_path_matches_manual_continuous_state(self):
        pcm = np.arange(-2208, 2208, dtype=np.int16)
        controls, reconstructed = ima_adpcm.encode_decode_chunks(pcm, 736)
        self.assertEqual((len(controls), len(reconstructed)), (6, 6))
        state = ima_adpcm.State()
        for chunk, signmag in zip(controls, reconstructed):
            predictor, index, reserved = ima_adpcm.CHECKPOINT.unpack_from(chunk)
            self.assertEqual((predictor, index), (state.predictor, state.index))
            self.assertEqual(reserved, 0)
            decoded, state = ima_adpcm.decode_chunk(chunk, 736)
            self.assertEqual(signmag, ima_adpcm.pcm16_to_sign_magnitude(decoded))

    def test_bad_checkpoint_is_rejected(self):
        chunk = bytearray(ima_adpcm.encode_chunk(np.zeros(736, np.int16))[0])
        chunk[3] = 1
        with self.assertRaisesRegex(ValueError, "reserved"):
            ima_adpcm.decode_chunk(bytes(chunk), 736)


if __name__ == "__main__":
    unittest.main()
