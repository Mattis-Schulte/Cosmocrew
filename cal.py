#!/usr/bin/env python3
import json, time, os, sys, argparse, statistics
from picarx import Picarx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALIB_PATH = os.path.join(BASE_DIR, "ir_calib.json")

def sample_series(px, seconds, hz):
    dt = 1.0 / hz
    buf = [[], [], []]
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            v = px.get_grayscale_data()
            if v and len(v) == 3:
                for i in range(3):
                    buf[i].append(float(v[i]))
        except Exception:
            pass
        time.sleep(dt)
    return buf

def summarize_series(buf):
    # returns per-channel (mean, std, min, max)
    out = []
    for ch in buf:
        if ch:
            mu = statistics.fmean(ch)
            sd = statistics.pstdev(ch) if len(ch) > 1 else 0.0
            out.append((mu, sd, min(ch), max(ch)))
        else:
            out.append((0.0, 0.0, 0.0, 0.0))
    return out

def aggregate_sets(sets):
    """
    sets: list of per-set tuples [(mu, sd, min, max)]*3
    return per-channel aggregate: mean of means, pooled std (within + between),
           and robust min/max across sets expanded by avg within std.
    """
    agg = []
    for ch in range(3):
        mus = [s[ch][0] for s in sets]
        sds = [s[ch][1] for s in sets]
        mins = [s[ch][2] for s in sets]
        maxs = [s[ch][3] for s in sets]
        mu_mean = statistics.fmean(mus) if mus else 0.0
        within_var = statistics.fmean([sd*sd for sd in sds]) if sds else 0.0
        between_var = statistics.pvariance(mus) if len(mus) > 1 else 0.0
        pooled_sd = (within_var + between_var) ** 0.5
        avg_within_sd = statistics.fmean(sds) if sds else 0.0
        rob_min = (min(mins) if mins else mu_mean) - 1.0 * avg_within_sd
        rob_max = (max(maxs) if maxs else mu_mean) + 1.0 * avg_within_sd
        agg.append({
            "mean": mu_mean,
            "std": pooled_sd,
            "min": rob_min,
            "max": rob_max
        })
    return agg

def main():
    ap = argparse.ArgumentParser(description="Multi-surface IR calibration for PiCar-X")
    ap.add_argument("--floor-sets", type=int, default=3, help="number of different FLOOR patches")
    ap.add_argument("--line-sets", type=int, default=3, help="number of different LINE patches")
    ap.add_argument("--sec", type=float, default=1.2, help="seconds to sample each set")
    ap.add_argument("--hz", type=float, default=120.0, help="sampling rate")
    ap.add_argument("--gamma", type=float, default=1.8, help="softmax gamma used by fusion")
    args = ap.parse_args()

    print("PiCar-X IR calibration (multi-surface)")
    print(f"Floor sets: {args.floor_sets}, Line sets: {args.line_sets}, per set: {args.sec:.1f}s @ {args.hz:.0f}Hz\n")

    try:
        px = Picarx()
        time.sleep(0.3)
    except Exception as e:
        print("Failed to init Picarx:", e)
        sys.exit(1)

    floor_sets = []
    for i in range(args.floor_sets):
        input(f"Position sensors over FLOOR patch #{i+1} (no line). Press Enter to sample...")
        print(f"Sampling FLOOR #{i+1}...")
        buf = sample_series(px, args.sec, args.hz)
        s = summarize_series(buf)
        floor_sets.append(s)
        print("  Means:", [round(s[j][0], 1) for j in range(3)],
              "  Std:", [round(s[j][1], 1) for j in range(3)])

    line_sets = []
    for i in range(args.line_sets):
        input(f"Position CENTER sensor over the LINE patch #{i+1}. Press Enter to sample...")
        print(f"Sampling LINE #{i+1}...")
        buf = sample_series(px, args.sec, args.hz)
        s = summarize_series(buf)
        line_sets.append(s)
        print("  Means:", [round(s[j][0], 1) for j in range(3)],
              "  Std:", [round(s[j][1], 1) for j in range(3)])

    floor = aggregate_sets(floor_sets)
    line = aggregate_sets(line_sets)

    # Build channels, polarity and separation metric
    channels = []
    seps = []
    for i in range(3):
        mu_f, sd_f, mn_f, mx_f = floor[i]["mean"], floor[i]["std"], floor[i]["min"], floor[i]["max"]
        mu_l, sd_l, mn_l, mx_l = line[i]["mean"], line[i]["std"], line[i]["min"], line[i]["max"]
        delta = mu_l - mu_f
        sep = abs(delta) / max(1.0, (sd_f**2 + sd_l**2) ** 0.5)  # d' like
        seps.append(sep)
        channels.append({
            "floor": {"mean": mu_f, "std": sd_f, "min": mn_f, "max": mx_f},
            "line":  {"mean": mu_l, "std": sd_l, "min": mn_l, "max": mx_l},
            "delta": delta,
            "polarity": "light" if delta > 0 else "dark",
            "sep": sep
        })

    min_sep = min(seps) if seps else 0.0
    print("\nSeparation d' (per channel):", [round(s, 2) for s in seps], "  min:", round(min_sep, 2))
    if min_sep < 0.8:
        print("Warning: low separation on at least one channel; performance may suffer in that condition.")

    data = {
        "ts": time.time(),
        "gamma": float(args.gamma),
        "channels": channels,
        "quality": {
            "separation": seps,
            "min_separation": min_sep
        }
    }
    tmp = CALIB_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CALIB_PATH)
    print("\nSaved calibration to", CALIB_PATH)
    print("Restart your server so IRSensorReader picks up the new stats.")

if __name__ == "__main__":
    main()