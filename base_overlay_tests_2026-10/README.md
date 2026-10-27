# Base-overlay tests (September–October 2026)

Notebooks and data from the tests with PYNQ's base overlay on our RFSoC 4x2, before switching to RF-clock (see `../rf_clock_board/`). Copied from the board's `~/jupyter_notebooks/felipe/base_overlay_tests_2026-10/` on 2026-10-23.

| File | What |
| --- | --- |
| `tone_test_5p7GHz.ipynb` | first 5.7 GHz tone (DAC zone 2 / mix-mode, NCO 784.8 MHz) for the SA124B measurements |
| `test1.ipynb`, `V1.ipynb` | base-overlay bring-up: converter readback, tone, finding which software channel is which ADC connector |
| `report_measurements.ipynb` | the report measurements below (was `Untitled.ipynb` on the board) |
| `report_data/passband_sweep.*` | DAC_B → BPF → unpowered amplifier → ADC_C response, 5.45–5.95 GHz: passband about 5.66–5.9 GHz, 4–6 dB ripple with about 57 MHz period (reflections) |
| `report_data/linearity.*` | received level against DAC amplitude at 5.7 GHz |
| `report_data/phase_stability.*` | carrier phase and residual frequency over 60 s, TX and RX on one board |
| `report_data/rx_nco_tuning.*` | 5.7 GHz tone after the ADC NCO at −1228.8, −900, −784.8, −700 MHz: lands at the predicted frequency each time |
| `noted.md` | the copy of the bench notes that was on the board (2026-09-26); the current version is `../noted.md` |

Connector map for the base overlay: transmitter channel 0 = DAC_B; receiver channel 0 = ADC_D, channel 1 = ADC_C; channels 2 and 3 hang in `transfer()`.
