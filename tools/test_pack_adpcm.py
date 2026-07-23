import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

import ima_adpcm
import pack_stream


class PackAdpcmTest(unittest.TestCase):
    def setUp(self):
        pack_stream.AUDIO_RATE = 22_050
        pack_stream.AUDIO_PCM = 736
        pack_stream.AUDIO_CONTROL = 372

    def test_chunks_preserve_continuous_state_and_reconstructed_pcm(self):
        count = 736 * 5
        x = np.arange(count, dtype=np.float64)
        samples = np.rint(
            22000 * np.sin(2 * np.pi * 997 * x / 22_050)
        ).astype(np.int16)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22_050)
                wav.writeframes(samples.astype("<i2").tobytes())
            chunks, pcm_chunks = pack_stream.build_audio_chunks(path, 5)

        self.assertTrue(all(len(chunk) == 372 for chunk in chunks))
        self.assertTrue(all(len(chunk) == 736 for chunk in pcm_chunks))
        state = ima_adpcm.State()
        expected_pcm = []
        for frame, chunk in enumerate(chunks):
            predictor, index, reserved = ima_adpcm.CHECKPOINT.unpack_from(chunk)
            self.assertEqual((predictor, index), (state.predictor, state.index))
            self.assertEqual(reserved, 0)
            decoded, state = ima_adpcm.decode_chunk(chunk, 736)
            expected_pcm.append(ima_adpcm.pcm16_to_sign_magnitude(decoded))
            self.assertEqual(expected_pcm[-1], pcm_chunks[frame])

    def test_audio_layout_matches_codec_chunk(self):
        rate, pcm_bytes, control_bytes = pack_stream.av_config.audio_frame_layout(30)
        self.assertEqual((rate, pcm_bytes, control_bytes), (22_050, 736, 372))
        chunk, _ = ima_adpcm.encode_chunk(np.zeros(pcm_bytes, np.int16))
        self.assertEqual(len(chunk), control_bytes)
        playback_fps = pack_stream.av_config.playback_fps_for_content(30)
        self.assertEqual(
            pack_stream.av_config.rf5c164_fd(pcm_bytes, playback_fps), 0x56C)


if __name__ == "__main__":
    unittest.main()
