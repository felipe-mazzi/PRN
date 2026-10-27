"""Capture every PPS-triggered acquisition on the HDO9404-MS and measure the PPS -> DAC_A delay.

While M2 sends one burst per PPS (started separately on the board), this arms the
scope in single mode, waits for the next trigger, downloads C1 (PPS, EPG1 output 4)
and C3 (DAC_A), and repeats for --seconds.  Every trace is saved; a screenshot of
the first acquisition is saved too.  Then each acquisition is analysed:

* PPS edge: the C1 rising crossing of 50 % between its low and high levels;
* burst start: the first crossing of 50 % of the burst's plateau level on C3,
  after the PPS edge.  ``--signal edge`` uses |C3| (the square wave of
  ``m2_tx_timing --waveform square --scope-nco 0``, whose first step is 0 -> -A);
  ``--signal envelope`` uses the Hilbert envelope (an RF burst, e.g. ``--waveform rect``
  at 3.8 GHz);
* delay = burst start - PPS edge, with linear interpolation between samples.

The scope's channel, timebase and trigger settings are left as they were set on
the front panel (trigger on C1 rising); only the trigger mode is changed, and it
is set back to Normal at the end.  The time window must contain both edges.

Run on RF-clock-PC as felipemazi, from ~/rf_clock_scope, with its own venv
(numpy, pyvisa, pyvisa-py, pyusb, matplotlib).  The scope's USB node is opened to the
plugdev group by /etc/udev/rules.d/60-lecroy-hdo9404.rules (otherwise run as root)::

    .venv/bin/python scope_delay_capture.py --probe
    .venv/bin/python scope_delay_capture.py --seconds 120

Results go to scope_delay_<time>/ next to this script (or --out): traces.npz (all
traces, in volts, with time axes and trigger times), delays.csv, summary.json,
screenshot_first.png and, if matplotlib is available, delays.png.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import struct
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyvisa

SCOPE = "USB0::0x05FF::0x1023::4404N30414::INSTR"  # HDO9404-MS on RF-clock-PC's USB
BACKEND = "@py"
PPS_CH, DAC_CH = "C1", "C3"


# ---------------------------------------------------------------- scope I/O

def tolerate_ren_stall() -> None:
    """The HDO9404 advertises USB488 REN_CONTROL but stalls the request (USB "Pipe error"),
    which pyvisa-py >= 0.8 sends on open; the scope works without it."""
    import usb.core
    from pyvisa_py.protocols import usbtmc

    enable = usbtmc.USBTMC._enable_remote_control

    def enable_or_skip(self):
        try:
            enable(self)
        except usb.core.USBError as exc:
            if exc.errno != 32:
                raise

    usbtmc.USBTMC._enable_remote_control = enable_or_skip


def open_scope(resource: str):
    tolerate_ren_stall()
    rm = pyvisa.ResourceManager(BACKEND)
    scope = rm.open_resource(resource)
    scope.timeout = 10_000
    scope.write("COMM_HEADER OFF")
    scope.write("COMM_FORMAT DEF9,WORD,BIN")
    scope.write("COMM_ORDER LO")
    scope.write("WAVEFORM_SETUP SP,0,NP,0,FP,0,SN,0")  # every point, from the first
    return scope


def read_waveform(scope, channel: str) -> dict:
    """One channel as volts, from the WAVEDESC block of `<ch>:WF? ALL`."""
    scope.write(f"{channel}:WF? ALL")
    raw = scope.read_raw()
    p = raw.find(b"WAVEDESC")
    if p < 0:
        raise RuntimeError(f"{channel}: no WAVEDESC in reply ({raw[:40]!r})")

    def i16(off): return struct.unpack_from("<h", raw, p + off)[0]
    def i32(off): return struct.unpack_from("<l", raw, p + off)[0]
    def f32(off): return struct.unpack_from("<f", raw, p + off)[0]
    def f64(off): return struct.unpack_from("<d", raw, p + off)[0]

    word = i16(32) == 1
    start = p + i32(36) + i32(40) + i32(48) + i32(52) + i32(56)
    nbytes = i32(60)
    if start + nbytes > len(raw):
        raise RuntimeError(f"{channel}: reply shorter than its descriptor says")
    codes = np.frombuffer(raw, dtype="<i2" if word else "i1", count=nbytes // (2 if word else 1), offset=start)
    volts = codes.astype(np.float64) * f32(156) - f32(160)
    trig = (struct.unpack_from("<h", raw, p + 308)[0], raw[p + 307], raw[p + 306], raw[p + 305], raw[p + 304], f64(296))
    return {"v": volts.astype(np.float32), "dt": f32(176), "t0": f64(180), "trigger_time": trig}


def acquire_once(scope, timeout_s: float) -> bool:
    """Arm single, wait for one trigger.  False if none came within timeout_s."""
    scope.write("TRIG_MODE SINGLE")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if scope.query("TRIG_MODE?").strip().upper().startswith("STOP"):
            return True
        time.sleep(0.02)
    return False


def screenshot(scope, path: Path) -> str:
    try:
        scope.write("HARDCOPY_SETUP DEV,PNG,FORMAT,LANDSCAPE,BCKG,BLACK,DEST,REMOTE,PORT,NET,AREA,DSOWINDOW")
        scope.write("SCREEN_DUMP")
        data = scope.read_raw()
        while b"IEND" not in data:  # the PNG can arrive over several USB transfers
            data += scope.read_raw()
        k = data.find(b"\x89PNG")
        if k < 0:
            return f"no PNG in reply ({data[:40]!r})"
        path.write_bytes(data[k:])
        return "saved"
    except Exception as exc:  # the screenshot is a convenience; never stop the run for it
        return f"failed: {exc!r}"


# ---------------------------------------------------------------- analysis

def crossing(t: np.ndarray, s: np.ndarray, level: float, after: float, rising: bool = True) -> float:
    """First time after `after` where s crosses `level`, linearly interpolated."""
    above = s >= level if rising else s <= level
    idx = np.nonzero(above[1:] & ~above[:-1] & (t[1:] > after))[0]
    if idx.size == 0:
        return float("nan")
    i = idx[0]
    return float(t[i] + (level - s[i]) / (s[i + 1] - s[i]) * (t[i + 1] - t[i]))


def envelope(v: np.ndarray) -> np.ndarray:
    n = v.size
    spec = np.fft.fft(v - v.mean())
    h = np.zeros(n)
    h[0] = 1
    h[1:(n + 1) // 2] = 2
    if n % 2 == 0:
        h[n // 2] = 1
    return np.abs(np.fft.ifft(spec * h))


def analyse(t_pps, pps, t_dac, dac, signal: str) -> dict:
    lo, hi = np.percentile(pps, [2, 98])
    t_edge = crossing(t_pps, pps, 0.5 * (lo + hi), after=t_pps[0])
    s = envelope(dac) if signal == "envelope" else np.abs(dac - np.median(dac[: max(dac.size // 50, 10)]))
    plateau = np.percentile(s, 90)
    t_burst = crossing(t_dac, s, 0.5 * plateau, after=t_edge)
    return {"t_pps_s": t_edge, "t_burst_s": t_burst, "delay_s": t_burst - t_edge,
            "pps_low_v": float(lo), "pps_high_v": float(hi), "plateau_v": float(plateau)}


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--seconds", type=float, default=120, help="how long to collect")
    parser.add_argument("--probe", action="store_true", help="one acquisition and a screenshot only")
    parser.add_argument("--signal", choices=("edge", "envelope"), default="edge")
    parser.add_argument("--resource", default=SCOPE)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out = args.out or Path(__file__).resolve().parent / f"scope_delay_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    scope = open_scope(args.resource)
    info = {"idn": scope.query("*IDN?").strip(), "start": datetime.now().isoformat(timespec="seconds"),
            "resource": args.resource, "pps_channel": PPS_CH, "dac_channel": DAC_CH, "signal": args.signal}
    print(info["idn"], flush=True)

    traces, rows = [], []
    try:
        t_end = time.monotonic() + (0 if args.probe else args.seconds)
        while True:
            if acquire_once(scope, timeout_s=2.5):
                wall = time.time()
                a, b = read_waveform(scope, PPS_CH), read_waveform(scope, DAC_CH)
                if not traces:
                    info["screenshot"] = screenshot(scope, out / "screenshot_first.png")
                    info["points"] = int(b["v"].size)
                    info["sample_interval_s"] = b["dt"]
                    print(f"{b['v'].size} points per channel, {b['dt'] * 1e12:.1f} ps per point; "
                          f"screenshot {info['screenshot']}", flush=True)
                t_a = a["t0"] + a["dt"] * np.arange(a["v"].size)
                t_b = b["t0"] + b["dt"] * np.arange(b["v"].size)
                r = {"n": len(traces), "wall_time": wall, **analyse(t_a, a["v"], t_b, b["v"], args.signal)}
                traces.append((a, b, wall))
                rows.append(r)
                print(f"{r['n']:4d}  delay {r['delay_s'] * 1e9:10.4f} ns", flush=True)
            else:
                print("no trigger within 2.5 s (is M2 running?)", flush=True)
            if time.monotonic() >= t_end:
                break
    finally:
        scope.write("TRIG_MODE NORM")
        scope.close()

    if traces:
        np.savez_compressed(
            out / "traces.npz",
            pps=np.stack([a["v"] for a, _, _ in traces]), dac=np.stack([b["v"] for _, b, _ in traces]),
            pps_t0=np.array([a["t0"] for a, _, _ in traces]), dac_t0=np.array([b["t0"] for _, b, _ in traces]),
            dt=np.array(traces[0][1]["dt"]), wall_time=np.array([w for _, _, w in traces]),
            trigger_time=np.array([b["trigger_time"] for _, b, _ in traces]))
        with open(out / "delays.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    d = np.array([r["delay_s"] for r in rows])
    good = d[np.isfinite(d)]
    summary = {**info, "acquisitions": len(rows), "valid": int(good.size)}
    if good.size:
        summary.update(mean_ns=good.mean() * 1e9, sdev_ps=(good.std(ddof=1) * 1e12 if good.size > 1 else 0.0),
                       min_ns=good.min() * 1e9, max_ns=good.max() * 1e9)
        print(f"\n{good.size} delays: mean {summary['mean_ns']:.4f} ns, sdev {summary['sdev_ps']:.1f} ps, "
              f"min {summary['min_ns']:.4f}, max {summary['max_ns']:.4f} ns")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if good.size > 1:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(1, 2, figsize=(11, 4), layout="constrained")
            ax[0].plot((d - good.mean()) * 1e12, "o-", ms=3)
            ax[0].set(xlabel="acquisition", ylabel="delay - mean [ps]",
                      title=f"PPS -> DAC_A, mean {summary['mean_ns']:.4f} ns"); ax[0].grid(alpha=0.3)
            ax[1].hist((good - good.mean()) * 1e12, bins=30)
            ax[1].set(xlabel="delay - mean [ps]", ylabel="count", title=f"sdev {summary['sdev_ps']:.1f} ps")
            ax[1].grid(alpha=0.3)
            fig.savefig(out / "delays.png", dpi=120)
        except ImportError:
            pass

    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:  # leave the results owned by the user who ran sudo
        for p in [out, *out.rglob("*")]:
            os.chown(p, int(uid), int(gid))
    print(f"results in {out}")


if __name__ == "__main__":
    main()
