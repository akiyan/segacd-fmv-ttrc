"""GPU(cupy)で量子化の重い部分(パレット割当・索引)を一括計算する任意モジュール。

差分コーデック sim.py の量子化は「各コマ独立」で実行時間の大半を占める。
中でも重いのは各セル(8x8=64px)を4面×15色パレットへ二乗誤差最小で割り当てる部分
(assign_palette)と、選んだパレットで最近傍量子化する部分(idx_for)＝まさに行列演算。
ここを GPU に載せる。CPU版と結果はビット単位で一致することを確認済み。

GPU実行は既定で有効。CBRSIM_GPU=0/off/false/no のときだけ明示的に無効化する。
cupy が無い/GPU が使えない場合は自動でCPUへフォールバック(enabled()==False)。
CUDA 実行環境は専用venv ~/.config/cbrsim-gpu/venv に隔離(cupy-cuda12x[ctk])。
sim を GPU で回すときはその venv の python で起動する。
"""
import os
import numpy as np

_STATE = {"checked": False, "on": False, "cp": None}


def enabled():
    """GPUエンコードは既定ON、CPUはフォールバック先。cupy/GPUが実際に使えるときだけ True。一度だけ判定。
    明示無効化は CBRSIM_GPU=0/off/false/no（cupy未導入のシステムpythonでは自動でCPUへ退避）。"""
    if _STATE["checked"]:
        return _STATE["on"]
    _STATE["checked"] = True
    if os.environ.get("CBRSIM_GPU", "1").strip().lower() in ("0", "off", "false", "no"):
        print("[gpu_quant] CBRSIM_GPU=0 指定によりCPUで実行します")
        return False
    try:
        import cupy as cp
        # デバイスに一度触れて JIT カーネルまで通ることを確認
        a = cp.arange(4, dtype=cp.int32).reshape(1, 1, 1, 1, 4)
        b = cp.arange(4, dtype=cp.int32).reshape(1, 1, 1, 1, 4)
        int(((a - b) ** 2).sum())
        cp.cuda.Stream.null.synchronize()
        _STATE["cp"] = cp
        _STATE["on"] = True
        name = cp.cuda.runtime.getDeviceProperties(0)["name"].decode()
        print(f"[gpu_quant] GPU 有効(既定): {name}")
    except Exception as e:  # noqa: BLE001 (どんな失敗でもCPUへ退避)
        print(f"[gpu_quant] GPU 使用不可のためCPUへフォールバック: {e}")
        _STATE["on"] = False
    return _STATE["on"]


class PalCache:
    """区間パレットのRGB333二乗誤差/index LUTをGPU上にキャッシュする。"""

    def __init__(self):
        self._d = {}

    def get(self, cp, seg, seg_pals):
        g = self._d.get(seg)
        if g is None:
            pals = cp.asarray(np.asarray(seg_pals[seg], dtype=np.int16))       # (4,15,3)
            key = cp.arange(512, dtype=cp.int16)
            rgb = cp.stack(((key >> 6) & 7, (key >> 3) & 7, key & 7), axis=1)
            distance = ((rgb[None, :, None, :] - pals[:, None, :, :]) ** 2).sum(3)
            index = distance.argmin(2).astype(cp.uint8)                        # (4,512)
            error = cp.take_along_axis(distance, index[:, :, None], axis=2)[:, :, 0]
            g = error.astype(cp.int16), index
            self._d[seg] = g
        return g


def assign_idx_one(flat, seg, seg_pals, cache):
    """1コマ分。flat (C,64,3) uint8 rgb333 -> (assign int8 (C,), pidx uint8 (C,64) 1..15)。

    CPU版 assign_palette/idx_for と同一結果(argmin の最初の最小=同じtie挙動)。
    """
    cp = _STATE["cp"]
    error, index = cache.get(cp, seg, seg_pals)               # each (4,512)
    f = cp.asarray(flat, dtype=cp.uint16)
    keys = ((f[..., 0] << 6) | (f[..., 1] << 3) | f[..., 2])  # (C,64)
    perr = error[:, keys].sum(2).T                             # (C,4)
    a = perr.argmin(1)                                        # (C,) 最良パレット
    p = index[a[:, None], keys] + 1                            # (C,64) 1..15
    return cp.asnumpy(a).astype(np.int8), cp.asnumpy(p).astype(np.uint8)


def _tile_err(cp, keys, pal, T):
    """RGB333 LUTでSTL4のL1タイル誤差をGPU計算する。CPU版と同一。"""
    P = cp.asarray(np.asarray(pal, dtype=np.int16))          # (15,3)
    key = cp.arange(512, dtype=cp.int16)
    rgb = cp.stack(((key >> 6) & 7, (key >> 3) & 7, key & 7), axis=1)
    lut = cp.abs(rgb[:, None, :] - P[None, :, :]).sum(2).min(1)
    return cp.asnumpy(lut[keys].reshape(T, 64).sum(1))       # (T,)


def build_palettes(train_tiles, palette15, n_pal=4, iters=6, edge_w=None):
    """CPU版 build_palettes(quantize_global4_tiles) の GPU 版。重い tile_errors だけ GPU、
    palette15(k-means, 高速)は CPU のまま=出力はCPU版とビット一致。tiles は一度だけGPUへ。
    edge_w(T,64)=画素ごとのエッジ学習重み(CBRSIM_EDGE_WEIGHT)。palette15 に渡す。"""
    cp = _STATE["cp"]
    T = train_tiles.shape[0]

    def _w(mask):
        return None if edge_w is None else edge_w[mask].reshape(-1)
    means = train_tiles.reshape(T, 64, 3).mean(1)
    order = np.argsort(means.sum(1))
    groups = np.array_split(order, n_pal)
    pals = [palette15(train_tiles[g].reshape(-1, 3), weights=_w(g)) for g in groups]
    flat = np.asarray(train_tiles, dtype=np.uint16).reshape(-1, 3)
    keys = cp.asarray((flat[:, 0] << 6) | (flat[:, 1] << 3) | flat[:, 2])
    for _ in range(iters):
        err = np.stack([_tile_err(cp, keys, pl, T) for pl in pals], axis=1)  # (T,n_pal)
        assign = err.argmin(1)
        pals = [pals[p] if not (assign == p).any()
                else palette15(train_tiles[assign == p].reshape(-1, 3), weights=_w(assign == p))
                for p in range(n_pal)]
    del keys
    return pals
