import os, sys, importlib
import numpy as np
sys.path.insert(0, "tools")
os.environ["CBRSIM_W"]="320"; os.environ["CBRSIM_H"]="224"; os.environ["CBRSIM_MODE"]="H40"
RING_CAP_KB = os.environ.get("RCAP","300")
os.environ["CBRSIM_RING_CAP_KB"]=RING_CAP_KB

import pack_stream as P
log = P.load_log("tmp/machi_ed_dec/decisions.pkl")
POOL = int(log["vram_tiles"])
PAT_PER_SEC = P.PAT_PER_SEC
AUDIO = "tmp/machi_ed_dec/audio_13k3_s8.raw"

def analyze(cap):
    os.environ["CBRSIM_PACK_MAXCOLD"]=str(cap)
    importlib.reload(P)
    per, n_load, n_upd, pal_w, Plist, tearing = P.resolve(log, POOL, "contig")
    blocks = P.build_control(log, per, n_upd, pal_w, AUDIO)
    sc = P.schedule(per, n_load, blocks)
    nps = sc["n_pay_sec"]; nc = sc["n_ctrl_sec"]; B = sc["prebuf_pat"]
    d = np.cumsum(n_load)
    A = np.cumsum(nps) + B//PAT_PER_SEC   # cumulative delivered sectors (incl prebuf)
    occ = A*PAT_PER_SEC - d               # ring occupancy in patterns
    occ_kb = occ*P.PAT/1024
    # jitter exposure: frames below thresholds
    below = {kb:int((occ_kb < kb).sum()) for kb in [5,10,20,40,60]}
    # sustained: longest run of frames with occ_kb < 40
    run=mx=0
    for v in occ_kb:
        if v < 40: run+=1; mx=max(mx,run)
        else: run=0
    print(f"cap={cap} RCAP={RING_CAP_KB}: cold={int(d[-1])} avg={d[-1]/len(per):.0f}/f "
          f"under={sc['under']} ring_min={occ_kb.min():.1f}KB peak={occ_kb.max():.0f}KB")
    print(f"   frames occ<KB: {below}  longest_run(<40KB)={mx}f ({mx/15:.1f}s)")
    print(f"   ctrl_sec avg={nc.mean():.2f} max={nc.max()}  pay_sec avg={nps.mean():.2f}")
    # occ around opening (frames 0..300 sampled)
    samp = [0,30,60,90,120,150,180,210,240,270,300,340,380]
    print("   occ@f: " + " ".join(f"{f}:{occ_kb[f]:.0f}" for f in samp if f<len(occ_kb)))
    return occ_kb

for cap in [192, 220, 260]:
    analyze(cap)
    print()
