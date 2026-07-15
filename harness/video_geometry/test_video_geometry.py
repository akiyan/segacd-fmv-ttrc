#!/usr/bin/env python3
"""Small regression checks for the H32/H40 source geometry helper."""

from tools.video_geometry import geometry_plan, source_filter


def main() -> None:
    h32 = geometry_plan("H32", 256, 224, 576, 400)
    h40 = geometry_plan("H40", 320, 224, 576, 400)
    assert h32["har"] == "8:7"
    assert h40["har"] == "32:35"
    assert h32["crop"] == [522, 400, 27, 0]
    assert h40["crop"] == [522, 400, 27, 0]
    assert h32["fit"] == h40["fit"] == "pad"
    assert h32["fit_size"] == [256, 202]
    assert h40["fit_size"] == [320, 202]
    # A non-square source must be measured in displayed pixels, not coded pixels.
    ntsc = geometry_plan("H32", 256, 224, 640, 480, 8, 9)
    assert ntsc["crop"] == [640, 434, 0, 23]
    wide = geometry_plan("H32", 256, 224, 720, 480, 8, 9)
    assert wide["crop"] == [704, 480, 8, 0]
    assert abs(h32["display_aspect"] - h40["display_aspect"]) < 1e-12
    h32_vf = source_filter("H32", 256, 224, 576, 400)
    h40_vf = source_filter("H40", 320, 224, 576, 400)
    assert "scale=512:404" in h32_vf and "pad=256:224" in h32_vf
    assert "scale=640:404" in h40_vf and "pad=320:224" in h40_vf
    assert source_filter("H32", 256, 224, 576, 400, fit="crop").startswith(
        "setsar=1,crop=522:400:27:0")
    print("video geometry checks OK")


if __name__ == "__main__":
    main()
