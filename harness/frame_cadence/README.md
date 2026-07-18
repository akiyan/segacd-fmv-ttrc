# Strict playback-frame cadence verifier

This harness proves that every movie frame in a native DEBUG playback recording
first appears at one exact VBlank interval.  It is intended for fixed-cadence
paths such as the 30 fps player's two-VBlank schedule.

`verify.py` decodes the recording sequentially at its native 256x224 or 320x224
geometry.  It sends only the top-left 40x24 pixels to
`tools/read_frameno.py:read_frameno`, so no other DEBUG field influences the
result.  A plausible exact sequence beginning at `F0000` anchors the movie.
From that point the verifier rejects:

- a skipped F value;
- a high-confidence F value that moves backwards;
- a first appearance that is earlier or later than the required number of
  capture frames;
- a recording that ends before the requested final F value.

The complete proof uses the frame count and default VBlank interval (`N`) from
the matching packed `HEADER.DAT`:

```sh
tools/python.sh harness/frame_cadence/verify.py \
  videos/BadApple_H40_320x224_pcm_emu_lossless.mkv \
  --header out/bad-apple-h40/HEADER.DAT
```

Override the required interval explicitly when diagnosing a different target:

```sh
tools/python.sh harness/frame_cadence/verify.py RECORDING.mkv \
  --header out/PROFILE/HEADER.DAT --vblanks 2
```

For a short capture, stop the proof at an inclusive decimal or hexadecimal
movie-frame number:

```sh
tools/python.sh harness/frame_cadence/verify.py RECORDING.mkv \
  --header out/bad-apple-h40/HEADER.DAT --through-frame 0x0386
```

Once the requested final F first appears, later recording frames are outside
the proof.  This deliberately excludes the normal held tail after the movie or
after a bounded diagnostic capture.  The initial anchor still uses four movie
frames by default so an isolated startup OCR match cannot impersonate `F0000`.

The default confidence threshold is `0.90`.  `--crop-x 32` supports an old H32
image centered in a 320-pixel capture.  Do not run this against the enlarged
upload compilation: the verifier requires the approximately 60 fps native
lossless recording so one capture frame corresponds to one emulator VBlank.

Run the unit tests with the pinned project environment:

```sh
tools/python.sh -m unittest discover -s harness/frame_cadence -p 'test_*.py'
```

This DEBUG HUD is a diagnostic signal only.  It must not be used to trim an
upload or to place YouTube chapters.
