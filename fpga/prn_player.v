// prn_player.v
//
// Plays a waveform stored in block RAM to an RF Data Converter DAC input,
// in a continuous loop, starting exactly on a PPS edge.
//
// SAMPLE CODE: written as a starting point, not yet simulated or built.
// Run prn_player_tb.v in the Vivado simulator before using it in a design.
//
// How it fits in the base overlay (radio/transmitter/channel_00 = DAC_B):
//
//   processor --AXI--> AXI BRAM Controller --port A--> Block RAM --port B--> prn_player --AXI-Stream--> rfdc/s00_axis
//   processor --AXI--> AXI GPIO --> arm, len_words
//   PPS SMA --> comparator --> FPGA pin AJ13 --> pps_async
//
// Clocking:
//   clk is the DAC stream clock (clk_dac0, 307.2 MHz). It comes from the same
//   PLLs as the DAC sample clock, so once the board's clock chip (LMK04828) is
//   locked to the external 10 MHz on CLK IN, every sample, chip and code period
//   is locked to that 10 MHz. One 256-bit word = 8 complex samples per clock
//   = 2.4576 GS/s, which the RF Data Converter interpolates x2 to 4.9152 GS/s.
//
// Behaviour:
//   arm = 0           : output zeros, address held at 0, length register follows len_words
//   arm 0 -> 1        : wait for the next rising PPS edge
//   PPS edge (armed)  : start reading word 0, then 1, 2, ... len_words-1, 0, 1, ... continuously
//   arm 1 -> 0        : stop, output zeros again
//   free_run = 1      : start as soon as armed, without waiting for PPS (bench tests with
//                       no PPS source connected; the start time is then not defined)
//
//   The delay from the PPS edge to the first sample on m_axis is a fixed number
//   of clock cycles (synchronizer + BRAM latency + output register), the same
//   every time. Like every other fixed delay in the chain, it is removed by
//   calibration.
//
// Memory format: word k (256 bits) holds 8 complex samples, packed as produced
// by rfsoc/prn_waveform_board.ipynb (pack_iq). The player does not look inside
// the words: the I/Q layout only has to match what the DAC expects.

`timescale 1ns / 1ps

module prn_player #(
    parameter DATA_W   = 256,   // 8 complex samples x (16-bit I + 16-bit Q) per clock
    parameter WORD_AW  = 13,    // word address width: up to 2^13 = 8192 words (2 Mbit)
    parameter READ_LAT = 2      // block RAM read latency on port B (2 = with output register)
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF M_AXIS:BRAM_PORTB, ASSOCIATED_RESET rst_n" *)
    input  wire                 clk,            // DAC stream clock, 307.2 MHz
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rst_n RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire                 rst_n,

    // control (from an AXI GPIO; synchronized inside, so any clock domain works)
    input  wire                 arm_async,      // 1 = play from the next PPS edge on, 0 = stop
    input  wire                 free_run_async, // 1 = start immediately when armed (no PPS needed)
    input  wire [WORD_AW:0]     len_words_async,// waveform length in 256-bit words (e.g. 3066); only read while disarmed
    input  wire                 pps_async,      // external 1 PPS (asynchronous to clk)

    // block RAM port B. In "BRAM Controller" mode the Block Memory Generator uses
    // byte addresses on both ports, so word k is at byte address 32*k.
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB CLK" *)
    (* X_INTERFACE_PARAMETER = "MASTER_TYPE BRAM_CTRL, MEM_ECC NONE, MEM_WIDTH 256, READ_WRITE_MODE READ_ONLY" *)
    output wire                 bram_clk,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB RST" *)
    output wire                 bram_rst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB EN" *)
    output wire                 bram_en,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB WE" *)
    output wire [DATA_W/8-1:0]  bram_we,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB ADDR" *)
    output wire [31:0]          bram_addr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB DIN" *)
    output wire [DATA_W-1:0]    bram_din,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 BRAM_PORTB DOUT" *)
    input  wire [DATA_W-1:0]    bram_dout,

    // AXI-Stream to the RF Data Converter DAC input (rfdc/s00_axis)
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TDATA" *)
    output reg  [DATA_W-1:0]    m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TVALID" *)
    output wire                 m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TREADY" *)
    input  wire                 m_axis_tready,  // the DAC takes one word every clock; not used

    // status (to AXI GPIO inputs, for reading from Python)
    output reg                  running,        // 1 while playing
    output reg  [31:0]          pps_count,      // number of PPS edges seen since reset
    output reg  [31:0]          start_pps       // pps_count value of the edge that started playback
);

    localparam BYTE_SHIFT = $clog2(DATA_W / 8);     // 5: 32 bytes per word

    // ---- synchronizers: bring the asynchronous inputs into the clk domain ----
    // Two flip-flops in a row per signal; ASYNC_REG keeps them close together
    // and tells Vivado they are synchronizers.
    (* ASYNC_REG = "TRUE" *) reg pps_meta, pps_sync;
    (* ASYNC_REG = "TRUE" *) reg arm_meta, arm_sync;
    (* ASYNC_REG = "TRUE" *) reg free_meta, free_sync;
    reg pps_prev;
    wire pps_rise = pps_sync & ~pps_prev;           // one-cycle pulse on each rising PPS edge

    // ---- player state ----
    reg [WORD_AW:0]   len_reg;                      // waveform length, captured while disarmed
    reg [WORD_AW-1:0] addr;                         // word currently being read
    reg [READ_LAT-1:0] run_pipe;                    // 'running' delayed to line up with the BRAM output
    integer i;

    always @(posedge clk) begin
        if (!rst_n) begin
            pps_meta  <= 1'b0;  pps_sync <= 1'b0;  pps_prev <= 1'b0;
            arm_meta  <= 1'b0;  arm_sync <= 1'b0;
            free_meta <= 1'b0;  free_sync <= 1'b0;
            running   <= 1'b0;
            addr      <= {WORD_AW{1'b0}};
            len_reg   <= {{WORD_AW{1'b0}}, 1'b1};
            pps_count <= 32'd0;
            start_pps <= 32'd0;
        end else begin
            pps_meta <= pps_async;  pps_sync <= pps_meta;  pps_prev <= pps_sync;
            arm_meta <= arm_async;  arm_sync <= arm_meta;
            free_meta <= free_run_async;  free_sync <= free_meta;

            if (pps_rise)
                pps_count <= pps_count + 32'd1;

            if (!arm_sync) begin
                // disarmed: stop, rewind, and follow the length register. len_words
                // is a multi-bit value crossing clock domains, which is only safe
                // because software writes it while disarmed and it is then stable
                // for many cycles before arm goes high.
                running <= 1'b0;
                addr    <= {WORD_AW{1'b0}};
                len_reg <= len_words_async;
            end else if (!running) begin
                // armed, waiting: start on the PPS edge (or right away in free-run mode)
                if (pps_rise || free_sync) begin
                    running   <= 1'b1;
                    addr      <= {WORD_AW{1'b0}};
                    start_pps <= pps_rise ? pps_count + 32'd1 : pps_count;   // edge that started it
                end
            end else begin
                // playing: next word, wrap after the last one
                if ({1'b0, addr} == len_reg - 1'b1)
                    addr <= {WORD_AW{1'b0}};
                else
                    addr <= addr + 1'b1;
            end
        end
    end

    // ---- read path: address -> block RAM (READ_LAT cycles) -> output register ----
    always @(posedge clk) begin
        if (!rst_n) begin
            run_pipe     <= {READ_LAT{1'b0}};
            m_axis_tdata <= {DATA_W{1'b0}};
        end else begin
            run_pipe[0] <= running;
            for (i = 1; i < READ_LAT; i = i + 1)
                run_pipe[i] <= run_pipe[i-1];
            // run_pipe[READ_LAT-1] is high exactly when bram_dout holds a word that was
            // read while running; otherwise send zeros (DAC output silent)
            m_axis_tdata <= run_pipe[READ_LAT-1] ? bram_dout : {DATA_W{1'b0}};
        end
    end

    // The RF Data Converter consumes one word every clock and expects a continuous
    // stream, so tvalid is always high (zeros when not playing), as in the base
    // overlay's amplitude controller.
    assign m_axis_tvalid = 1'b1;

    // block RAM port B: read only, always enabled, byte address = word index * 32
    assign bram_clk  = clk;
    assign bram_rst  = 1'b0;
    assign bram_en   = 1'b1;
    assign bram_we   = {(DATA_W/8){1'b0}};
    assign bram_din  = {DATA_W{1'b0}};
    assign bram_addr = {{(32 - WORD_AW - BYTE_SHIFT){1'b0}}, addr, {BYTE_SHIFT{1'b0}}};

endmodule
