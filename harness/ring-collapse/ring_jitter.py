import os, sys, importlib
import numpy as np
sys.path.insert(0, "tools")
os.environ["CBRSIM_W"]="320"; os.environ["CBRSIM_H"]="224"; os.environ["CBRSIM_MODE"]="H40"
import pack_stream as P
log = P.load_log("tmp/machi_ed_dec/decisions.pkl")
POOL = int(log["vram_tiles"]); PPS = P.PAT_PER_SEC
AUD = "tmp/machi_ed_dec/audio_13k3_s8.raw"

def run(cap, rcap):
    os.environ["CBRSIM_RING_CAP_KB"]=str(rcap)
    os.environ["CBRSIM_PACK_MAXCOLD"]=str(cap)
    importlib.reload(P)
    per, n_load, n_upd, pal_w, Plist, tearing = P.resolve(log, POOL, "contig")
    blocks = P.build_control(log, per, n_upd, pal_w, AUD)
    sc = P.schedule(per, n_load, blocks)
    nps = sc["n_pay_sec"]; B = sc["prebuf_pat"]
    d = np.cumsum(n_load)
    occ = (np.cumsum(nps) + B//PPS)*PPS - d          # patterns in ring after frame i
    deliv = nps*PPS                                   # patterns delivered at frame i
    pop = n_load                                       # patterns popped at frame i
    # jitter-survival: if delivery stalls for k frames starting at i, can the ring
    # feed the next k frames' pops? worst deficit = max over window of (sum pop - occ_before)
    def worst_stall(k):
        # occ_before[i] = occ[i-1] (ring before frame i's delivery+pop). approx occ[i]+pop[i]-deliv[i]
        worst = 1e9
        cumpop = np.concatenate([[0], np.cumsum(pop)])
        for i in range(1, len(occ)):
            # ring available before frame i (patterns) ~ occ[i-1]
            avail = occ[i-1]
            need = cumpop[min(i+k, len(pop))] - cumpop[i]   # pops over next k frames (no delivery)
            worst = min(worst, avail - need)
        return worst
    m1 = float(min(occ[j-1]-pop[j] for j in range(1,len(occ))))   # occ before minus this pop
    print(f"cap={cap} RCAP={rcap}: cold_avg={d[-1]/len(per):.0f} ring_min={occ.min()*P.PAT/1024:.1f}KB "
          f"peak={occ.max()*P.PAT/1024:.0f}KB")
    for k in [1,2,3,5,8]:
        w = worst_stall(k)
        print(f"    stall{k}f survive_margin = {w:8.0f} pat ({w*P.PAT/1024:7.1f}KB)")

for cap,rcap,label in [(192,300,"SAFE"),(260,300,"COLLAPSE"),(260,350,"SAFE")]:
    print(f"--- {label} ---")
    run(cap,rcap); print()
