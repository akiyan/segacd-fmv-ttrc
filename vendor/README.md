# Vendor Reference Sources

This directory contains third-party reference source snapshots used while
building the Mega-CD PCM playback path. These files are not part of the game
runtime yet; keep local modifications separate unless intentionally vendoring a
patch.

## MegaDev PCM Reference

Path: `vendor/megadev-pcm`

Source: https://github.com/drojaazu/megadev

Snapshot: `7a7246c MEGADEV 1.2.0 release merge`

License: MIT, see `vendor/megadev-pcm/LICENSE`.

Included subset:

- `examples/pcm_playback`: Mega-CD PCM playback example.
- `lib/sub/pcm.s`: PCM RAM clear and channel register setup helpers.
- `lib/sub/pcm.h`, `lib/sub/pcm.def.h`: RF5C164 register definitions and C wrappers.
- `lib/sub/memmap.def.h`: referenced by the PCM helper assembly.

Most relevant files:

- `vendor/megadev-pcm/examples/pcm_playback/src/spx.c`
- `vendor/megadev-pcm/examples/pcm_playback/src/pcmplay_v2.s`
- `vendor/megadev-pcm/lib/sub/pcm.s`
- `vendor/megadev-pcm/lib/sub/pcm.def.h`

## SegaCDMode1PCM

Path: `vendor/SegaCDMode1PCM`

Source: https://github.com/viciious/SegaCDMode1PCM

Snapshot: `54863cd Use a more compact PCM RAM layout for channels`

License: MIT, see `vendor/SegaCDMode1PCM/LICENSE.md`.

This is a complete Mode 1 PCM driver and demo snapshot, excluding its `.git`
directory and devcontainer metadata. It is useful as a practical reference for
RF5C164 register writes, PCM RAM writes, channel double buffering, playback
position reads, frequency conversion, and ADPCM decode-to-PCM-RAM flow.

Most relevant files:

- `vendor/SegaCDMode1PCM/cd/pcm.c`
- `vendor/SegaCDMode1PCM/cd/pcm-io.s`
- `vendor/SegaCDMode1PCM/cd/s_channels.c`
- `vendor/SegaCDMode1PCM/cd/s_sources.c`
- `vendor/SegaCDMode1PCM/scd_pcm.c`

## SCDTools scdwav2pcm

Path: `vendor/scdtools`

Source: https://github.com/classiccoding/scdtools

Snapshot: `653b45e added -wolfteam options and extacted pcm/wav use better filenames by default`

License: `scdwav2pcm` declares GPL-2.0-or-later in its source header.

Included subset:

- `scdwav2pcm`: WAV to Sega CD PCM conversion script.
- `README.TXT`: upstream tool list.

The script is useful for comparing sign/magnitude byte conversion behavior and
FD register value calculation against our local packer.
