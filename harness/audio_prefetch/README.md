# Persistent PCM prefetch proof

This harness verifies the audio queue built from a real `HEADER.DAT` and
`BODY.DAT`. It walks the routing table and every control block, reproduces the
player's startup preload and legacy skip behavior, independently reproduces the
packer's even full-length PCM retiming, and compares the resulting PCM sample
sequence against the encoder WAV.

Current streams keep a persistent reserve: startup sectors queue source chunks
0-29, control frame 0 queues chunk 30, frame 1 queues chunk 31, and so on. The
first `nframes * audio_bytes` queued samples must match the source byte for byte.

Run from the repository root:

```sh
python3 harness/audio_prefetch/verify.py \
  out/movieplay/HEADER.DAT out/movieplay/BODY.DAT \
  videos/BadApple_H32_256x224_pcm13/audio_13k3_u8_mono.wav
```

This is a data-layer proof. Dynamic confirmation still uses the full-row DEBUG
recording and `harness/startup_resync/analyze.py`; `L` should remain near the
prefetch depth and `R` should stay zero.
