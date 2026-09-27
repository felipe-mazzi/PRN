#!/usr/bin/env python3
"""
predicted_tx_spectrum.py

PURPOSE
    Predict what the SA124B spectrum analyzer will show when the RFSoC 4x2 DAC
    transmits the real timing waveform (PRN m-sequence + BPSK + RRC pulse
    shaping), BEFORE any of it is built in the FPGA. Used to choose the chip
    rate and filter roll-off, and to check that the signal fits in the band.

WHERE THIS RUNS
    On your Ubuntu PC. Needs only NumPy and Matplotlib.

WHY THE PREDICTION CAN BE TRUSTED
    The model is calibrated to the bench measurements from 2026-09-25:
      - DAC settings actually used: fs = 4.9152 GSPS, NCO = 784.8 MHz, mix-mode
        -> carrier = fs + f_NCO = 5.7 GHz (the zone-3 image)
      - the absolute power level is tied to the measured CW tone
        (-6.6 dBm at 1 GHz, normal mode, amplitude 0.5)
      - the measured extra loss of the board's output path is included

THE MODEL IN ONE LINE
    For every spectral line of the waveform:
        P(f) = P_digital(f)  x  |H_dac(f)|^2  x  L_board(f)
    where
      P_digital : power of that line in the digital signal after the NCO
      H_dac     : the DAC's output response (mix-mode or normal)
      L_board   : measured extra loss (baluns + traces + cable + analyzer)
    and every line appears at every image frequency k*fs +/- (f_NCO + f_baseband).

ASSUMPTION NOT YET VERIFIED ON THE BENCH
    The DAC's amplitude scales linearly with the digital value (so a waveform
    with the same peak as the 0.5 CW tone has the same peak output amplitude).
"""

# argparse : reads command-line options such as --chip-rate 25.6e6
# Path     : builds file paths and creates the output folder
import argparse
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt


# =============================================================================
# 1. CONSTANTS FROM THE BENCH
# =============================================================================

# DAC sample rate [Hz]. Fixed by the base overlay (read back from the RF Data
# Converter). Every image frequency is k*FS_DAC +/- f.
FS_DAC = 4.9152e9

# NCO frequency [Hz]. Chosen so the image at FS_DAC + F_NCO = 5.7 GHz.
F_NCO = 784.8e6

# Speed of light [m/s], used to convert chip period into path length.
C = 299_792_458.0

# ---- power calibration: the measured reference tone ----
# A constant input at amplitude 0.5, NCO at 1 GHz, normal mode, measured -6.6 dBm.
# From this single point we can compute the absolute power of any other signal.
CW_REF_DBM, CW_REF_AMP, CW_REF_FREQ = -6.6, 0.5, 1.0e9

# ---- measured extra output-path loss ----
# At each frequency, the difference between what the ideal DAC formula predicts
# and what the SA124B measured. This is the frequency response of everything
# after the DAC core: baluns, PCB traces, SMA cable, and the analyzer itself.
# Frequencies in GHz, losses in dB. The 0 GHz point anchors the curve at DC.
LOSS_F_GHZ = np.array([0.0, 0.785, 3.915, 4.130, 5.700, 5.915])
LOSS_DB    = np.array([0.0, 0.0,   1.4,   2.3,   5.3,   5.8])

# Approximate noise floor seen on the SA124B, as a density [dBm per Hz].
# Measured ~-73 dBm at 250 kHz RBW and ~-90 dBm at 10 kHz RBW on the bench;
# both correspond to roughly -128 dBm/Hz. Added so plots look like the analyzer.
ANALYZER_FLOOR_DBM_HZ = -128.0

# Feedback taps for maximal-length LFSRs (primitive polynomials).
# Key = number of register bits n, value = tap positions (1-indexed).
# An n-bit maximal-length LFSR repeats every 2^n - 1 chips; e.g. n=10 -> 1023.
# Taps from the standard Xilinx table (XAPP052).
LFSR_TAPS = {7: (7, 6), 9: (9, 5), 10: (10, 7), 11: (11, 9)}


# =============================================================================
# 2. DAC OUTPUT RESPONSE AND BOARD LOSS
# =============================================================================

def h_nrz(f, fs=FS_DAC):
    """
    Amplitude response of the DAC in NORMAL (NRZ, non-return-to-zero) mode.

    The DAC holds each sample constant for one full period Ts = 1/fs.
    A rectangular pulse of width Ts has the Fourier transform
        |H(f)| = |sin(pi f / fs) / (pi f / fs)|  =  |sinc(f / fs)|
    which is 1 at DC, falls with frequency, and is exactly 0 at fs.
    That's why normal mode favors Nyquist zone 1.

    Note: np.sinc(x) is defined as sin(pi x)/(pi x), so np.sinc(f/fs) is
    exactly the formula above.
    """
    return np.abs(np.sinc(np.atleast_1d(f) / fs))


def h_mix(f, fs=FS_DAC):
    """
    Amplitude response of the DAC in MIX-MODE (RF mode).

    Each sample is output as +value for the first half period and -value
    for the second half. The Fourier transform of that pulse is
        |H(f)| = sin^2(u) / u,   with u = pi f / (2 fs)
    which is 0 at DC, peaks near 0.74*fs (~3.6 GHz here), and falls to 0 at 2*fs.
    That's why mix-mode favors Nyquist zones 2 and 3.

    Both responses are normalized the same way (to Ts), so they can be
    compared directly: at 5.7 GHz, mix-mode gives 0.515 (-5.8 dB) where
    normal mode gives 0.16 (-15.9 dB).
    """
    u = np.pi * np.atleast_1d(f).astype(float) / (2 * fs)
    out = np.zeros_like(u)
    nz = u != 0                              # avoid dividing by zero at DC
    out[nz] = np.sin(u[nz]) ** 2 / u[nz]
    return out


def board_loss_db(f):
    """
    Extra output-path loss [dB] at frequency f [Hz], interpolated linearly
    between the measured points. Beyond the last measured point (5.915 GHz)
    np.interp holds the last value (5.8 dB), which is an assumption:
    the real loss probably keeps rising above 6 GHz.
    """
    return np.interp(np.atleast_1d(f) / 1e9, LOSS_F_GHZ, LOSS_DB)


def cw_ideal_dbm():
    """
    Power [dBm] that a CW tone of amplitude CW_REF_AMP would have with an
    IDEAL DAC (flat response, no board loss).

    We measured -6.6 dBm at 1 GHz, but that measurement already includes
    the normal-mode sinc roll-off at 1 GHz (-0.6 dB) and a tiny board loss.
    Removing both gives the "ideal" reference power, from which the power
    of any line at any frequency in any mode can be computed.
    """
    return (CW_REF_DBM
            - 20 * np.log10(h_nrz(CW_REF_FREQ)[0])   # undo the sinc roll-off at 1 GHz
            + board_loss_db(CW_REF_FREQ)[0])          # undo the board loss at 1 GHz


# =============================================================================
# 3. THE WAVEFORM: m-sequence -> BPSK -> RRC
#    (This is exactly the chain the FPGA will implement.)
# =============================================================================

def mseq(nbits=10):
    """
    Generate one period of a maximal-length sequence (m-sequence) of 0/1 bits
    with a Fibonacci LFSR (linear-feedback shift register).

    How an LFSR works:
      - a shift register of nbits bits, started in a non-zero state (all ones)
      - each clock: output the last bit, compute the XOR of the tap bits,
        shift everything one place, and insert the XOR result at the front
    With primitive-polynomial taps, the register visits every non-zero state
    exactly once, so the output repeats every 2^n - 1 bits.

    This is also exactly what the FPGA version will do: one XOR gate and
    a shift register, producing one chip per clock enable.
    """
    taps = LFSR_TAPS[nbits]
    state = np.ones(nbits, dtype=np.int8)        # any non-zero start state works
    n = 2 ** nbits - 1                           # sequence period in chips
    out = np.empty(n, dtype=np.int8)
    for i in range(n):
        out[i] = state[-1]                       # output bit = last register stage
        # XOR of the tapped stages (taps are 1-indexed, arrays are 0-indexed)
        fb = np.bitwise_xor.reduce(state[[t - 1 for t in taps]])
        state = np.roll(state, 1)                # shift every bit one stage along
        state[0] = fb                            # feedback enters the first stage

    # Sanity check: an m-sequence of period 2^n - 1 has exactly 2^(n-1) ones
    # (one more one than zeros). If this fails, the taps are wrong.
    assert out.sum() == 2 ** (nbits - 1), "not maximal length: check taps"
    return out


def rrc_taps(beta, sps, span):
    """
    Impulse response of a root-raised-cosine (RRC) pulse-shaping filter.

    beta : roll-off factor (0..1). Occupied bandwidth = chip_rate * (1 + beta).
           Small beta = narrower spectrum but longer, ringier pulses.
    sps  : samples per chip (how finely the pulse is sampled).
    span : filter length in chips (longer = closer to the ideal RRC).

    Why RRC: the transmitter uses RRC and the receiver's matched filter uses
    RRC too. Combined, they form a raised-cosine pulse, which has zero
    intersymbol interference and maximizes SNR at the correlator.

    The formula below is the standard closed form of the RRC impulse
    response. It has two special points (t = 0 and t = +/- 1/(4 beta))
    where the general formula divides by zero, so those use their limits.
    """
    # time axis in units of chip periods, centered on 0
    t = np.arange(-(span * sps) // 2, (span * sps) // 2 + 1) / sps
    h = np.empty(t.size)
    for i, ti in enumerate(t):
        if np.isclose(ti, 0.0):
            # limit of the formula at t = 0
            h[i] = 1 - beta + 4 * beta / np.pi
        elif beta > 0 and np.isclose(abs(ti), 1 / (4 * beta)):
            # limit of the formula at t = +/- 1/(4 beta)
            h[i] = (beta / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * beta))
                                          + (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            # general closed-form RRC impulse response
            h[i] = ((np.sin(np.pi * ti * (1 - beta)) + 4 * beta * ti * np.cos(np.pi * ti * (1 + beta)))
                    / (np.pi * ti * (1 - (4 * beta * ti) ** 2)))

    # Normalize to unit energy so the filter doesn't change the signal's scale
    # in an arbitrary way (the peak is rescaled explicitly later anyway).
    return h / np.sqrt(np.sum(h ** 2))


def periodic_baseband(chip_rate, beta, nbits=10, sps=8, span=16, peak_amp=0.5):
    """
    Build ONE PERIOD of the baseband waveform, exactly as it will be
    transmitted continuously (the code repeats with no gaps).

    Steps:
      1. m-sequence bits 0/1  ->  BPSK chips +1/-1   (0 -> +1, 1 -> -1)
      2. upsample: place each chip at the start of sps samples, zeros between
      3. filter with the RRC pulse
      4. scale so the waveform's peak equals peak_amp

    Why circular convolution: the transmitted signal repeats forever, so the
    end of one period flows smoothly into the start of the next. Filtering
    one period "circularly" (wrapping around) reproduces that exactly, with
    no artificial start-up or end transients. Multiplying FFTs and
    transforming back is a fast way to do circular convolution.

    Returns
      b       : one period of the real baseband waveform (BPSK = real only)
      fs_bb   : its sample rate = sps * chip_rate
      papr_db : peak-to-average power ratio [dB] of the shaped waveform
    """
    chips = 1.0 - 2.0 * mseq(nbits)               # step 1: 0/1 -> +1/-1
    L = chips.size * sps                           # samples in one period
    up = np.zeros(L)
    up[::sps] = chips                              # step 2: one chip every sps samples
    # step 3: circular convolution with the RRC filter (zero-padded to L)
    b = np.real(np.fft.ifft(np.fft.fft(up) * np.fft.fft(rrc_taps(beta, sps, span), L)))

    # PAPR: how much higher the waveform's peaks are than its average power.
    # A constant CW tone has PAPR 0 dB; RRC-shaped BPSK has a few dB.
    # This matters because the DAC (and later the PA) are limited by PEAK,
    # but the analyzer measures AVERAGE power.
    papr_db = 10 * np.log10(np.max(b ** 2) / np.mean(b ** 2))

    # step 4: set the peak to peak_amp, the same scale as the DAC gain setting,
    # so peak_amp=0.5 means "same peak amplitude as the 0.5 CW bench tone"
    b *= peak_amp / np.max(np.abs(b))
    return b, sps * chip_rate, papr_db


def occupied_bw(b, fs_bb, frac=0.99):
    """
    Bandwidth containing `frac` (default 99%) of the signal's power, the
    standard "occupied bandwidth" used for band planning and regulations.

    Method: compute the power spectrum, sort by frequency, accumulate, and
    find the frequencies where 0.5% and 99.5% of the power has been reached.
    """
    P = np.abs(np.fft.fft(b)) ** 2
    f = np.fft.fftfreq(b.size, 1 / fs_bb)
    order = np.argsort(f)                           # FFT order -> ascending frequency
    cum = np.cumsum(P[order]) / P.sum()             # cumulative power fraction
    lo = f[order][np.searchsorted(cum, (1 - frac) / 2)]
    hi = f[order][np.searchsorted(cum, 1 - (1 - frac) / 2)]
    return hi - lo


# =============================================================================
# 4. DAC OUTPUT: every spectral line of every Nyquist image
# =============================================================================

def dac_output_lines(b, fs_bb, mode="mix", f_max=8e9):
    """
    Compute the frequency and power of every spectral line at the DAC output,
    including all Nyquist images up to f_max.

    Why "lines": because the waveform is periodic (the PRN repeats), its
    spectrum is not a continuous curve but a comb of discrete lines spaced
    chip_rate / (2^n - 1) apart. The FFT of exactly one period gives the
    amplitude of every line directly.

    Returns
      freqs  : frequency of every line [Hz], all images together
      pows   : power of every line [mW]
      images : list of dicts, one per image (label, center freq, total power)
    """
    L = b.size

    # FFT of one period, divided by L: each bin is one spectral line's complex
    # amplitude. By Parseval, sum(|Bk|^2) = mean(b^2) = average signal power.
    Bk = np.fft.fft(b) / L
    fk = np.fft.fftfreq(L, 1 / fs_bb)               # baseband frequency of each line

    # Power of each line relative to the CW reference tone.
    # Reasoning: after the complex-to-real mixer, a CW constant of amplitude A
    # gives one-sided power A^2/2, and a baseband line of amplitude Bk gives
    # one-sided power |Bk|^2/2. Their ratio is |Bk|^2 / A^2.
    rel = np.abs(Bk) ** 2 / CW_REF_AMP ** 2

    # Drop negligible lines (far out in the RRC filter's stopband), for speed.
    keep = rel > 1e-14

    # The NCO shifts every baseband line up by F_NCO. This gives the
    # frequencies in the DIGITAL domain (first Nyquist zone).
    f_dig, rel = F_NCO + fk[keep], rel[keep]

    # All digital lines must lie inside the first Nyquist zone (0 .. fs/2),
    # otherwise the images would overlap each other (aliasing).
    ok = (f_dig > 0) & (f_dig < FS_DAC / 2)
    if not ok.all():
        print(f"warning: {np.sum(~ok)} lines fall outside the first Nyquist zone and were dropped")
    f_dig, rel = f_dig[ok], rel[ok]

    H = h_mix if mode == "mix" else h_nrz           # choose the DAC output response
    p0_mw = 10 ** (cw_ideal_dbm() / 10)             # reference power, dBm -> mW

    freqs, pows, images = [], [], []

    # Loop over every image. The analog DAC output contains copies of the
    # digital spectrum at m*fs + f_dig and m*fs - f_dig for m = 0, 1, 2, ...
    #   m=0, + : the fundamental at f_NCO             (0.785 GHz)
    #   m=1, - : fs - f_NCO, spectrally mirrored      (4.130 GHz)
    #   m=1, + : fs + f_NCO, our carrier              (5.700 GHz)
    #   m=2, - : 2fs - f_NCO                          (9.05 GHz, beyond f_max)
    for m in range(int(f_max // FS_DAC) + 2):
        for sgn in (+1, -1):
            if m == 0 and sgn == -1:
                continue                             # negative frequencies: not physical here
            f = m * FS_DAC + sgn * f_dig             # this image's line frequencies
            sel = (f > 0) & (f < f_max)              # keep only what's on the display
            if not sel.any():
                continue
            f = f[sel]

            # Line power = reference power
            #              x relative line power (from the waveform)
            #              x DAC response squared (amplitude -> power)
            #              x measured board loss (dB -> linear)
            p = p0_mw * rel[sel] * H(f) ** 2 * 10 ** (-board_loss_db(f) / 10)

            fc = m * FS_DAC + sgn * F_NCO            # image center frequency
            label = "f_NCO" if m == 0 else f"{m}fs{'+' if sgn > 0 else '-'}f_NCO"
            images.append(dict(label=label, fc=fc, p_dbm=10 * np.log10(p.sum())))
            freqs.append(f)
            pows.append(p)
    return np.concatenate(freqs), np.concatenate(pows), images


def analyzer_trace(freqs, pows_mw, f_start, f_stop, rbw):
    """
    Turn the list of spectral lines into something that looks like a swept
    spectrum-analyzer trace.

    A real analyzer sweeps a filter of width RBW (resolution bandwidth)
    across the band and displays the power inside it. We approximate that by
    dividing the span into bins of width RBW and summing the power of all
    lines in each bin (a rectangular filter shape: slightly idealized, but
    total powers come out right).

    Consequence to be aware of: if the RBW is WIDER than the line spacing,
    several lines add up in each bin and the trace looks like a smooth band.
    If the RBW is NARROWER than the line spacing, you see the individual
    lines of the PRN comb (Figure 3 shows this).

    The analyzer's own noise floor (density x RBW) is added to every bin.
    Returns bin-center frequencies [Hz] and power per bin [dBm].
    """
    edges = np.arange(f_start, f_stop + rbw, rbw)
    p, _ = np.histogram(freqs, bins=edges, weights=pows_mw)   # sum line power per bin
    p += 10 ** (ANALYZER_FLOOR_DBM_HZ / 10) * rbw             # add the noise floor
    return 0.5 * (edges[:-1] + edges[1:]), 10 * np.log10(p)


def simulate(chip_rate, beta, mode="mix", peak_amp=0.5, nbits=10):
    """Run the whole chain for one configuration and bundle the results."""
    b, fs_bb, papr = periodic_baseband(chip_rate, beta, nbits=nbits, peak_amp=peak_amp)
    freqs, pows, images = dac_output_lines(b, fs_bb, mode)
    return dict(chip_rate=chip_rate, beta=beta, freqs=freqs, pows=pows, images=images,
                papr=papr, obw=occupied_bw(b, fs_bb), nchips=2 ** nbits - 1)


def image_power(sim, fc_target):
    """Total power [dBm] of the image whose center is closest to fc_target."""
    return min(sim["images"], key=lambda im: abs(im["fc"] - fc_target))["p_dbm"]


# =============================================================================
# 5. MAIN: run the simulations, make the figures, print the summary table
# =============================================================================

def main():
    # ---- command-line options (all have sensible defaults) ----
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip-rate", type=float, default=FS_DAC / 96,
                    help="chips/s (default fs/96 = 51.2 Mcps)")
    ap.add_argument("--beta", type=float, default=0.35, help="RRC roll-off")
    ap.add_argument("--mode", choices=["mix", "nrz"], default="mix",
                    help="DAC output mode: mix = mix-mode (zone 2 setting), nrz = normal")
    ap.add_argument("--peak-amp", type=float, default=0.5,
                    help="waveform peak, same scale as DAC gain (0.5 = bench tone)")
    ap.add_argument("--nbits", type=int, default=10, choices=sorted(LFSR_TAPS),
                    help="LFSR length; PRN period = 2^nbits - 1 chips")
    ap.add_argument("--outdir", default="figures", help="folder for the PNG files")
    ap.add_argument("--no-show", action="store_true", help="save figures without opening windows")
    a = ap.parse_args()

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)           # create figures/ if it doesn't exist
    f_carrier = FS_DAC + F_NCO                        # 5.7 GHz: the image we transmit on

    # ---- the configuration chosen on the command line ----
    main_sim = simulate(a.chip_rate, a.beta, a.mode, a.peak_amp, a.nbits)

    # ---- Figure 1: full span 0-8 GHz, 250 kHz RBW (same view as the bench screenshots) ----
    fx, px = analyzer_trace(main_sim["freqs"], main_sim["pows"], 100e3, 8e9, 250e3)
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(fx / 1e9, px, lw=0.7)
    # label every image with its name and total power
    for im in main_sim["images"]:
        ax.annotate(f"{im['label']}\n{im['p_dbm']:.1f} dBm total", (im["fc"] / 1e9, px.max() + 3),
                    ha="center", fontsize=8)
    ax.set(xlabel="Frequency [GHz]", ylabel="Power [dBm / 250 kHz RBW]", ylim=(-100, px.max() + 15),
           title=f"Predicted DAC output: PRN-{main_sim['nchips']} BPSK, {a.chip_rate/1e6:.2f} Mcps, "
                 f"RRC beta={a.beta}, {a.mode} mode, NCO {F_NCO/1e6:.1f} MHz")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "pred_full_span.png", dpi=150)

    # ---- Figure 2: compare chip rates around the 5.7 GHz carrier ----
    # These four rates are exact divisions of fs (fs/480, /192, /96, /48).
    # Integer ratios make the FPGA simple: one chip every N DAC samples.
    rates = [FS_DAC / 480, FS_DAC / 192, FS_DAC / 96, FS_DAC / 48]
    sims = [simulate(r, a.beta, a.mode, a.peak_amp, a.nbits) for r in rates]
    fig, ax = plt.subplots(figsize=(11, 5))
    for s in sims:
        fz, pz = analyzer_trace(s["freqs"], s["pows"], f_carrier - 150e6, f_carrier + 150e6, 100e3)
        ax.plot((fz - f_carrier) / 1e6, pz, lw=0.8,
                label=f"{s['chip_rate']/1e6:.2f} Mcps (99% BW {s['obw']/1e6:.1f} MHz)")
    # shade the 5.725-5.850 GHz license-free band, to see what fits
    ax.axvspan((5.725e9 - f_carrier) / 1e6, (5.850e9 - f_carrier) / 1e6, color="green", alpha=0.08,
               label="5.725-5.850 GHz band")
    ax.set(xlabel=f"Offset from {f_carrier/1e9:.4f} GHz [MHz]", ylabel="Power [dBm / 100 kHz RBW]",
           ylim=(-110, None), title=f"Carrier image vs chip rate (RRC beta={a.beta}, {a.mode} mode)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "pred_chip_rate_compare.png", dpi=150)

    # ---- Figure 3: fine zoom (+/- 1 MHz, 10 kHz RBW) showing the PRN line comb ----
    # Because the code repeats every 2^n - 1 chips, the spectrum is a comb of
    # lines spaced chip_rate / (2^n - 1). At 10 kHz RBW the SA124B resolves
    # them, which makes a quick check that the real waveform is right.
    ff, pf = analyzer_trace(main_sim["freqs"], main_sim["pows"], f_carrier - 1e6, f_carrier + 1e6, 10e3)
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot((ff - f_carrier) / 1e3, pf, lw=0.8)
    ax.set(xlabel="Offset from carrier [kHz]", ylabel="Power [dBm / 10 kHz RBW]",
           title=f"Fine structure: lines every chip_rate/{main_sim['nchips']} = "
                 f"{a.chip_rate/main_sim['nchips']/1e3:.1f} kHz (periodic PRN)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "pred_fine_comb.png", dpi=150)

    # ---- summary table ----
    # Columns:
    #   Rc         chip rate
    #   Tc         chip period = coarse timing resolution of the correlator
    #   c*Tc       the same, as a path length (how far light travels in one chip)
    #   99%BW      occupied bandwidth, for band planning
    #   PAPR       peak-to-average power ratio of the shaped waveform
    #   P@5.70     total power in the carrier image
    #   P@4.13     total power in the unwanted fs - f_NCO image (the BPF must kill this)
    #   lines      spacing of the PRN comb lines
    print(f"\nCW reference (ideal DAC, amplitude {CW_REF_AMP}): {cw_ideal_dbm():.2f} dBm")
    hdr = (f"{'Rc [Mcps]':>10} {'Tc [ns]':>8} {'c*Tc [m]':>9} {'99%BW[MHz]':>11} {'PAPR[dB]':>9} "
           f"{'P@5.70[dBm]':>12} {'P@4.13[dBm]':>12} {'lines[kHz]':>11}")
    print(hdr)
    print("-" * len(hdr))
    # print the four comparison rates, plus the command-line rate if it's different
    for s in sims + ([main_sim] if a.chip_rate not in rates else []):
        Tc = 1 / s["chip_rate"]
        print(f"{s['chip_rate']/1e6:10.2f} {Tc*1e9:8.2f} {C*Tc:9.2f} {s['obw']/1e6:11.1f} {s['papr']:9.2f} "
              f"{image_power(s, f_carrier):12.1f} {image_power(s, FS_DAC - F_NCO):12.1f} "
              f"{s['chip_rate']/s['nchips']/1e3:11.1f}")
    print(f"\nFigures saved to {out.resolve()}")

    if not a.no_show:
        plt.show()                                   # open the figure windows


# Only run main() when this file is executed as a script,
# not when it's imported from another file (e.g. to reuse mseq or rrc_taps).
if __name__ == "__main__":
    main()