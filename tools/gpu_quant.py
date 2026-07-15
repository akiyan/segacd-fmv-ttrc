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
    """区間パレット(4,15,3)を GPU 上に (1,1,4,15,3) int32 でキャッシュする。"""

    def __init__(self):
        self._d = {}

    def get(self, cp, seg, seg_pals):
        g = self._d.get(seg)
        if g is None:
            g = cp.asarray(np.asarray(seg_pals[seg], dtype=np.int32)).reshape(1, 1, 4, 15, 3)
            self._d[seg] = g
        return g


def assign_idx_one(flat, seg, seg_pals, cache):
    """1コマ分。flat (C,64,3) uint8 rgb333 -> (assign int8 (C,), pidx uint8 (C,64) 1..15)。

    CPU版 assign_palette/idx_for と同一結果(argmin の最初の最小=同じtie挙動)。
    """
    cp = _STATE["cp"]
    pals = cache.get(cp, seg, seg_pals)                       # (1,1,4,15,3)
    f = cp.asarray(flat.astype(np.int32)).reshape(-1, 64, 1, 1, 3)  # (C,64,1,1,3)
    d = ((f - pals) ** 2).sum(4)                             # (C,64,4,15) 各色との二乗誤差
    perr = d.min(3).sum(1)                                    # (C,4) パレット毎の最近傍誤差合計
    a = perr.argmin(1)                                        # (C,) 最良パレット
    dsel = cp.take_along_axis(d, a.reshape(a.shape[0], 1, 1, 1), axis=2)[:, :, 0, :]  # (C,64,15)
    p = dsel.argmin(2) + 1                                    # (C,64) 1..15
    return cp.asnumpy(a).astype(np.int8), cp.asnumpy(p).astype(np.uint8)


def _tile_err(cp, px, pal, T, chunk=3_000_000):
    """GPU上の px (T*64,3) int32 と 15色パレット pal から タイル毎の最小量子化誤差和 (T,)。
    CPU版 tile_errors と同一(L1距離・最小・タイル内合計)。巨大中間を避けチャンク分割。"""
    P = cp.asarray(np.asarray(pal, dtype=np.int32))          # (15,3)
    N = px.shape[0]
    emin = cp.empty(N, cp.int32)
    for s in range(0, N, chunk):
        g = px[s:s + chunk]                                  # (c,3)
        emin[s:s + chunk] = cp.abs(g[:, None, :] - P[None, :, :]).sum(2).min(1)
    return cp.asnumpy(emin.reshape(T, 64).sum(1))            # (T,)


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
    px = cp.asarray(train_tiles.reshape(-1, 3).astype(np.int32))   # (T*64,3) 常駐
    for _ in range(iters):
        err = np.stack([_tile_err(cp, px, pl, T) for pl in pals], axis=1)   # (T,n_pal)
        assign = err.argmin(1)
        pals = [pals[p] if not (assign == p).any()
                else palette15(train_tiles[assign == p].reshape(-1, 3), weights=_w(assign == p))
                for p in range(n_pal)]
    del px
    return pals
