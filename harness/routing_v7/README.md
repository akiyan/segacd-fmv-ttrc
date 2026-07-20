# TTRC v7 routing migration proof

`verify.py` reads a complete v6 `HEADER.DAT` and `BODY.DAT`, independently
converts every two-byte `[n_pay_sec, n_ctrl_sec]` route into the v7 one-byte
`(total << 3) | n_ctrl_sec` form, decodes it again, and walks every BODY frame.

The proof requires all payload/control pairs, frame boundaries, continuous
control bytes, continuous payload bytes, and rate-padding bytes to remain
identical. It also checks the v7 frame limit, frame-0 zero entry, reserved bits,
and zero sector padding.

```sh
tools/python.sh harness/routing_v7/verify.py out/PROFILE/HEADER.DAT out/PROFILE/BODY.DAT
```
