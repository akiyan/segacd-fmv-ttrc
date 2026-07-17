#!/usr/bin/env python3
"""Small regression checks for the H32/H40 source geometry helper."""

from tools.video_geometry import endpoint_snap_filter, geometry_plan, source_filter


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
    direct_vf = source_filter(
        "H40", 320, 224, 576, 400, denoise=False,
        resize_filter="lanczos")
    assert "hqdn3d" not in direct_vf and "gblur" not in direct_vf
    assert direct_vf.count("flags=lanczos") == 1
    assert "flags=area" in source_filter(
        "H40", 320, 224, 576, 400, denoise=False,
        resize_filter="area")
    assert endpoint_snap_filter() == ""
    endpoint_vf = endpoint_snap_filter(2, 253)
    assert endpoint_vf.startswith("format=rgb24,lutrgb=")
    assert endpoint_vf.count(
        "if(lte(val,2),0,if(gte(val,253),255,val))") == 3
    try:
        endpoint_snap_filter(253, 2)
    except ValueError as exc:
        assert "black_max must be below" in str(exc)
    else:
        raise AssertionError("unordered endpoint snap limits were accepted")
    print("video geometry checks OK")


if __name__ == "__main__":
    main()
