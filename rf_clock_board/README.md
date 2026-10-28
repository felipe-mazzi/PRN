# RF-clock on board 8873292600AB

Bring-up of our RFSoC 4x2 (S/N 8873292600AB, 192.168.2.100) with RF-clock, the two-way time transfer system developed with Haoyu Dong: 5.8 GHz PRN bursts, timed to an FS725-locked 100 MHz tick, correlated on the ARM. This folder records what was installed, every step that was run, and the results, so they can be reviewed and reproduced.

## Contents

| Path | What |
| --- | --- |
| `notebooks/loopback_step_by_step.ipynb` | the M3 loopback measurement split into one cell per step, with plots, executed on the board on 2026-10-23 (outputs included) |
| `notebooks/build_loopback_nb.py` | script that generates that notebook |
| `notebooks/loopback_*.npz` | data saved by the notebook's two runs (capture, reference, correlation, delays) |
| `logs/` | console logs and run records of every check run on the board (M1, VCO scan, MTS record/check, M2, M3) and the saved captures. Copied from `~/jupyter_notebooks/felipe/rf_clock/logs/` on the board |
| `experiments/scope_delay_capture.py` | captures every PPS-triggered C1/C3 acquisition on the HDO9404-MS over USB and measures PPS → DAC_A delay per acquisition (runs on RF-clock-PC as felipemazi, in `~/rf_clock_scope` with its own venv) |
| `scope/` | its results: traces, per-acquisition delays, summary, scope screenshot, check plot |
| `board/board.json` | this board's configuration as written to `/etc/rf-clock/board.json` (serial, VCO bands, MTS capture codes). |
| `reference/rf_clock_v1.3.0.8/` | copy of the RF-clock project at release v1.3.0.8 (commit `d3230cc`): documentation (`docs/`, `plan/`), FPGA sources (`PL/`), Python sources (`src/rf_clock/`), experiment scripts, other boards' configs, and the overlay manifest. The built overlay (`.bit`) is not copied; it is in the release wheel |

## Software

* RF-clock **1.3.0.8** (upgraded to **1.3.0.10** on 2026-10-28, see below), from the group Gitea release: http://kinabalu2.stdusr.yale.internal/gitea/haoyu.dong/RF-clock (tag `v1.3.0.8`).
* Wheel `rf_clock-1.3.0.8-py3-none-any.whl`, sha256 `b3cfd822e48ae6e4d9798df50eb7c24998484b6c92691acc66387f629c280538`. The overlay inside it (`rf_clock.bit`, `.hwh`, `_hw.xsa`, build 1.3.0.0, Vivado 2024.1, timing met) matches the checksums of its manifest.
* Installed into PYNQ's environment as the project specifies: `sudo /usr/local/share/pynq-venv/bin/python -m pip install --no-deps --no-index rf_clock-1.3.0.8-py3-none-any.whl` (nothing else in that environment changed).
* Board: PYNQ 3.1.1. On the board, everything of ours is under `~/jupyter_notebooks/felipe/` (`rf_clock/` for this work, `base_overlay_tests_2026-10/` for the earlier base-overlay tests).

All commands below run on the board as root through PYNQ's Python:

```bash
run="sudo env BOARD=RFSoC4x2 XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python"
```

## Wiring

| Signal | Connection |
| --- | --- |
| 10 MHz | Until 2026-10-27: FS725 → directly to the external reference SMA. From the two-board set-up (2026-10-27 evening): FS725 → EPG1 REF IN; **EPG1 REF OUT → resistive splitter → this board and 887329260072A** |
| 1PPS | FS725 1PPS → EPG1 TRIG IN → PPS SMA J7: **output 2** until the two-board set-up, then **output 1** (887329260072A back on its tuned output 2). EPG1 output 4 → scope C1 |
| RF out | **DAC_A** (`vout20`, DAC tile 2) |
| RF in | **ADC_B** (`vin2_01`, ADC tile 2) |
| Scope (2026-10-27) | DAC_A → HDO9404-MS C3; EPG1 output 4 → C1. Loopback cable removed for this |
| Loopback (2026-10-23) | DAC_A → 5725-5875 MHz band-pass filter → **10 dB** attenuator → ADC_B (the project's reference uses 20 dB) |

EPG1 settings read on 2026-10-27 (`experiments/epg1.py status`, Haoyu's account): TRIG IN threshold 1.30 V, REF IN locked, outputs 1–4 enabled, 10 µs, 2.5 V into 50 Ω; output 2 delay set 6.6 ns (actual 7.48 ns), output 4 set 0 (actual 0.52 ns). So the scope's PPS edge is about 6.96 ns ahead of the board's (plus any cable-length difference).

RF-clock uses DAC_A / ADC_B. Our earlier base-overlay tests used DAC_B (transmitter channel 0) and ADC_C (receiver channel 1).

## Steps run and results

Following `reference/rf_clock_v1.3.0.8/docs/board_setup.md` section 4.

| Date | Step | Command | Result |
| --- | --- | --- | --- |
| 2026-10-22 | **M1** tick against PPS | `$run -m rf_clock.checks.m1_check 60` | **PASS**: 100 000 000 ticks in all 58 sampled periods, jitter 0, faults 0, pps_locked; PPS edges 60/60 on both comparator and Schmitt-trigger inputs |
| 2026-10-22 | **VCO bands** | `$run -m rf_clock.checks.vco_bands scan --write --label ...` | ADC LMX2594: calibration picked band 124 in 20/20 writes, bands 115-124 lock → **124**. DAC: 126 in 20/20, bands 118-126 lock → **126**. Both are the highest band that locks (open question for the project: margin at the edge) |
| 2026-10-22 | **MTS capture codes** | `$run -m rf_clock.checks.mts_dtc record --write`, then `check` | identical windows in 6/6 power-ups: ADC T1 52/55, ADC PLL 61/66, DAC T1 58/57; check: largest distance 1 code (warning above 6) |
| 2026-10-22 | **M2** timed TX (digital) | `$run -m rf_clock.checks.m2_tx_timing 10` | **PASS**: 10/10 bursts, TX_ACTUAL − target = 0, TX_ACTUAL − PPS = 100 000 ticks for every burst; DAC MTS latency 1896, ADC 1296 (the expected values) |
| 2026-10-23 | **M3** loopback | `$run -m rf_clock.checks.m3_loopback 20 --save ...` | **PASS**: delay **229.6670 samples = 287.0837 ns**, standard deviation **1.3 ps** over 20 captures, PSL 22.2 dB (≥ 10 required), 0 invalid windows |
| 2026-10-27 | M1 (after recabling) | `$run -m rf_clock.checks.m1_check 30` | **PASS**: 30/30 periods of 100 000 000 ticks, jitter 0, faults 0 |
| 2026-10-27 | **M2 analog** (scope, square wave, mixer bypassed) | board: `$run -m rf_clock.checks.m2_tx_timing 240 --waveform square --scope-nco 0 --amplitude 32000 --offset-ticks 0`; PC: `experiments/scope_delay_capture.py --seconds 120` | digital PASS (240/240 bursts, TX_ACTUAL − PPS = 0). Scope, EPG1 output 4 rising 50 % → first DAC_A edge at half step: **174.936 ns**, sdev **27.0 ps** over 121 acquisitions, range 128 ps, no drift, no 2.5 ns or 10 ns jumps (`scope/scope_delay_20261027_162017/`) |
| 2026-10-27 | **NTP** (board_setup §1.5) | `/etc/systemd/timesyncd.conf.d/rf-clock.conf`: `NTP=192.168.2.1` (RF-clock-PC's chrony) | NTPSynchronized = yes (before: not synchronized, RTC about 2.5 min off) |
| 2026-10-27 | **M3** after recabling (DAC_A → BPF → 10 dB → ADC_B rebuilt after the scope runs) | `$run -m rf_clock.checks.m3_loopback 20 --save ...` | **PASS**: **287.1142 ns**, sdev 2.0 ps, PSL 22.1 dB. 30.5 ps longer than on 10-23, received level 0.5 dB lower, carrier phase −0.58 rad (was −0.35). Same VCO bands and MTS latencies, so attributed to the rebuilt RF path (connectors/filter); a pure cable-length change would also have moved the phase by about −1.1 rad, so it is not only that |
| 2026-10-27 | **TWTT one-board self-test** | `$run -m rf_clock.twtt.node AB --seconds 300 --log twtt_AB_20261027_232952.jsonl` | **PASS**: 300/300 seconds valid, PSL 22.1–22.3 dB; θ = +0.06 ± 0.07 ps (truth 0), sdev 1.18 ps, Allan deviation 2.1e-12 at 1 s falling as 1/τ to 2.2e-14 at 100 s; one-way delay 287.1094 ns (drift −0.5 ps/min, cancels in θ). Notebook `../TWTT_selftest_Oct_27.ipynb`, figures `twtt/` |
| 2026-10-28 | **Upgrade to RF-clock 1.3.0.10** (same bitstream; LMK divider SYNC; DAC MTS target 1944) | wheel sha256 `0b32c25e…7ea87`, overlay files match the release manifest and the Python files match 887329260072A's install; `board.json` backed up as `board.json.bak_1.3.0.8_20261028`; `mts_dtc record --write` + `check`; `m2_tx_timing 10` | MTS codes stable in 6/6 power-ups (largest distance 1 code), offsets as recorded; M2 PASS, DAC latency 1944, ADC 1296. The 10-27 M3 value (287.1142 ns) is from 1.3.0.8 and does not carry over |
| 2026-10-28 | **Two-board TWTT** with 887329260072A (B) | A: `rf_clock.twtt.node A --seconds 600 --save-every 60`; B: `node B --peer 192.168.2.100 ...` | 593/593 paired seconds valid. θ = **−647.2 ps**, sdev 8.0 ps (second-to-second 3.1 ps, ADEV 5.3e-12 at 1 s). AB direction clean (1.9 ps, flat); **BA direction 10.9 dB weaker, steps of +15 and +22 ps** without the matching carrier-phase change: suspect the BA path hardware. Notebook `../TWTT_two_board_Oct_27.ipynb`, data `twtt_pair/` |
| 2026-10-28 | **Two-board TWTT after the attenuator fix** (the paths had 10 dB A → B and **20 dB** B → A; both replaced by **5 dB**) | the same, `--seconds 600 --save-every 60` (3-min check run first: `--seconds 180 --save-every 20`, run `20261028_113559`) | 591/591 valid. B → A now −3.7 dB relative to A → B (was −10.9); θ second-to-second **1.42 ps**, ADEV **2.4e-12** at 1 s (one-board floor 1.18 ps, 2.1e-12). But B → A still wanders: −7 ps/min drift, then **−45 ps over 25 s at 11:49:00–11:49:25** with the level falling 1.5 dB (2.4 dB by the end) and the carrier phase following less than half: θ = −571.3 ps, sdev 12.3 ps. The attenuators explained the level, not the steps. Notebook `../TWTT_two_board_Oct_28.ipynb`, data `twtt_pair/20261028_114317/` |
| 2026-10-23 | Step-by-step notebook (after a full restart: clocks + MTS) | `notebooks/loopback_step_by_step.ipynb` | delay **287.0833 ns**, 1.76 ps over 10 captures, PSL 22.2 dB: the same delay as M3 to about 1 ps across the restart |

Board label set to `8873292600AB` in the board config on 2026-10-22 (only the label field changed).

## Not done yet

* M2 analog half across restarts: repeat the scope run after reloading (and after rebooting) and compare the means. Also the 3.8 GHz `--waveform rect` envelope method (the real TX path).
* PPS capture margin (board_setup.md 4.2), band table (4.7), NTP (1.5).
* Repeatability across many restarts, long-term stability, precision against SNR (compare with `../simulation/`).
* Two boards (M4): PPS capture margin on EPG1 output 2. B → A path of the two-board set-up: steps and level drops remain after the attenuator fix; swap filter and cables between the paths, wiggle test, then repeat (and after a power cycle).
