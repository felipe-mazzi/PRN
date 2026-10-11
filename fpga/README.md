# PRN player overlay (sample)

A modified RFSoC-PYNQ **base overlay** in which DAC_B plays a waveform from block RAM in a continuous loop, starting on a PPS edge, instead of the base overlay's constant (tone) output. Everything else (receivers, RF Data Converter settings, Python drivers) stays as in the base overlay.

| File | What it is |
|---|---|
| `prn_player.v` | the player: reads one 256-bit word (8 complex samples) per 307.2 MHz clock, loops, starts on PPS (or immediately in free-run mode) |
| `prn_player_tb.v` | self-checking testbench for the player |
| `prn_player.xdc` | PPS pin (AJ13) and clock-crossing constraints |
| `../rfsoc/prn_waveform_board.ipynb` | runs on the board: generates and packs the waveform, writes it into the memory, arms the player |

**Status: sample code.** It has not been simulated or built yet. Start with step 2.

## Block design change

Inside `radio/transmitter/channel_00` (= DAC_B, RF Data Converter tile 228, `rfdc/s00_axis`), which in the base overlay contains only `amplitude_controller`:

```
channel_00/S_AXI ─► AXI SmartConnect ─┬─► AXI BRAM Controller (256-bit) ─► BRAM port A ┐
                                      │                                                Block Memory Generator
                                      └─► AXI GPIO ─► arm, free_run, len_words         (true dual port, 256-bit)
                                                            │                          │
pps_in (pin AJ13) ─────────────────────────────────────────►prn_player ◄─ BRAM port B ─┘
                                                            │
                                                            └─► M_AXIS ─► channel_00/M_AXIS ─► rfdc/s00_axis
```
All of it runs on `aclk` of channel_00, which is `clk_dac0` (307.2 MHz), the same clock as the DAC stream.

## Steps

### 1. Prerequisites (lab PC)
- Vivado **2024.1** (it has to match: the v3.1.1 build scripts refuse other versions), with a license that covers the XCZU48DR.
- `make`, `wget`, `unzip` (the makefile downloads the RFSoC 4x2 board files).

### 2. Simulate the player
1. In Vivado: *File → Project → New*, RTL project, any part (the player is generic Verilog).
2. Add `prn_player.v` as a design source and `prn_player_tb.v` as a simulation source; set `prn_player_tb` as the simulation top.
3. *Run Simulation → Run Behavioral Simulation*, then *Run All*. The console should end with `PASS: 50 words checked, in order, no gaps`, and print the fixed latency from the PPS edge to the first word.

### 3. Build the unchanged base overlay (checks the toolchain)
```bash
git clone --branch v3.1.1 https://github.com/Xilinx/RFSoC-PYNQ.git
cd RFSoC-PYNQ/boards/RFSoC4x2/base
make            # board files, block design, bitstream, timing check: about 1-2 hours
```
This produces `base.bit` and `base.hwh`. Copying them to the board and loading them with `BaseOverlay` must behave like the original before anything is changed.

### 4. Modify the block design (Vivado GUI)
1. `vivado base/base.xpr`, then open the block design `base`.
2. *Add Sources → Add or create design sources* → `prn_player.v`.
3. Double-click `radio`, then `transmitter`, then `channel_00` to open the hierarchy. Delete `amplitude_controller`.
4. Inside `channel_00` add:
   - **AXI SmartConnect**: 1 slave, 2 masters.
   - **AXI BRAM Controller**: AXI protocol AXI4, data width **256**, number of BRAM interfaces **1**.
   - **Block Memory Generator**: memory type **True Dual Port RAM**, mode **BRAM Controller** (widths and depth follow the controller).
   - **AXI GPIO**: channel 1 all outputs, width **2** (bit 0 = arm, bit 1 = free_run); enable dual channel, channel 2 all outputs, width **14** (len_words).
   - **prn_player**: right-click the canvas → *Add Module* → `prn_player`.
5. Connect:
   - `channel_00/S_AXI` → SmartConnect `S00_AXI`; `M00_AXI` → BRAM controller `S_AXI`; `M01_AXI` → GPIO `S_AXI`.
   - BRAM controller `BRAM_PORTA` → memory `BRAM_PORTA`; prn_player `BRAM_PORTB` → memory `BRAM_PORTB`.
   - prn_player `M_AXIS` → `channel_00/M_AXIS` (the pin the amplitude controller used to drive).
   - GPIO `gpio_io_o[0]` → `arm_async`, `gpio_io_o[1]` → `free_run_async`, `gpio2_io_o` → `len_words_async` (use a *Slice* IP to split channel 1 into its two bits).
   - `aclk` → every clock pin (SmartConnect, BRAM controller, GPIO, prn_player `clk`); `aresetn` → every reset pin (prn_player `rst_n`).
6. PPS: *Create Port* `pps_in` (input) at the top level, and pins of the same name on the `radio`, `transmitter` and `channel_00` hierarchies, so it reaches prn_player `pps_async`.
7. *Address Editor*: assign the BRAM controller a **256 KiB** range (8192 words × 32 bytes) and the GPIO 64 KiB, both under `channel_00`. The old amplitude-controller segment is gone.
8. *Validate Design* (F6) and save.
9. The top-level wrapper has to include the new `pps_in` port: in *Sources*, right-click `base.bd` → *Create HDL Wrapper* (let Vivado manage it), set it as top.
10. Add `prn_player.xdc` as a constraints file (or append its lines to `vivado/constraints/base.xdc`).

### 5. Build
1. *Generate Bitstream* (about 1-2 hours).
2. Check the timing summary: WNS ≥ 0 and no unconstrained clock-crossing warnings left for the player.
3. Collect the two files and give them the same name:
   - `base/base.runs/impl_1/base_wrapper.bit` → `prn_player.bit`
   - `base/base.gen/sources_1/bd/base/hw_handoff/base.hwh` → `prn_player.hwh`

### 6. Load and test on the board
1. Copy both files and the notebook to the board, through the lab PC:
   ```bash
   scp prn_player.bit prn_player.hwh ../rfsoc/prn_waveform_board.ipynb xilinx@192.168.2.100:/home/xilinx/jupyter_notebooks/prn/
   ```
2. On the board, load it like the base overlay, so the receiver and RF Data Converter drivers are set up as usual:
   ```python
   from pynq.overlays.base import BaseOverlay
   ol = BaseOverlay('/home/xilinx/jupyter_notebooks/prn/prn_player.bit')
   ol.init_rf_clks()
   dac = ol.radio.transmitter.channel[0].dac_block      # DAC_B: zone 2, NCO 784.8 MHz as before
   ```
   `channel[0]` no longer has `.control` (that was the amplitude controller); the DAC block and the receivers work as before. If `BaseOverlay` refuses the file, use `pynq.Overlay` and set the DAC/ADC through `ol.radio.rfdc`.
3. Run `prn_waveform_board.ipynb` with `LOAD_TO_FPGA = True` and `FREE_RUN = True`:
   - first with the **tone** (`data = tone_packed`): ADC_C (`receiver.channel[1]`, NCO −784.8 MHz) must show it at **+10.02 MHz**. This checks the I/Q packing; if it is wrong, change `PACKING` and repeat.
   - then with the **PRN** (`data = prn_packed`): capture 32768 samples on ADC_C and correlate.
4. With a PPS source connected to the PPS SMA: `FREE_RUN = False`. Playback starts on the next PPS edge.

## Not covered here: locking to the external 10 MHz

Locking the board to the 10 MHz on CLK IN is done in the clock chip, not in the FPGA design. The RFSoC 4x2 routes CLK IN to the LMK04828, whose input is selected by `LMK_CLK_IN_SEL0/1` (processor pins MIO7/MIO8, see the reference manual). It needs:
1. an LMK04828 register configuration for a 10 MHz input on that CLKin (made with TI's TICS Pro), replacing the default file that `init_rf_clks()` loads,
2. the MIO7/MIO8 select lines set to that input.

Once the LMK is locked to it, the RF PLLs, the DAC/ADC sample clocks and `clk_dac0` (and therefore the player) all follow the 10 MHz.
