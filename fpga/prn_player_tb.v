// prn_player_tb.v
//
// Self-checking testbench for prn_player.v (Vivado simulator, or Icarus Verilog).
//
// The block RAM is modelled with a READ_LAT-cycle read latency and filled with
// a recognisable pattern: every 32-bit slot of word k holds 0xA500_0000 + k, so
// the word index can be read back from m_axis_tdata. The PPS period is shortened
// to PPS_PERIOD clock cycles so the simulation is quick.
//
// Checks:
//   1. before the first PPS edge after arming, the output is all zeros
//   2. after it, the words come out in order 0, 1, ..., LEN-1, 0, 1, ... with no gaps
//   3. the first word appears a fixed number of cycles after the PPS edge (printed)
//   4. after disarming, the output returns to zeros
//
// Vivado: add this file as a simulation source, set prn_player_tb as top, Run Simulation.
// Icarus: iverilog -g2005 -o tb prn_player_tb.v prn_player.v && vvp tb

`timescale 1ns / 1ps

module prn_player_tb;

    localparam DATA_W     = 256;
    localparam WORD_AW    = 13;
    localparam READ_LAT   = 2;
    localparam LEN        = 7;          // short waveform so wrap-around is exercised often
    localparam PPS_PERIOD = 400;        // clock cycles between PPS edges (1 s on the real board)
    localparam N_CHECK    = 50;         // words checked after playback starts

    // ---- clock (307.2 MHz) and reset ----
    reg clk = 1'b0;
    always #1.6276 clk = ~clk;
    reg rst_n = 1'b0;

    // ---- stimulus ----
    reg              arm = 1'b0;
    reg [WORD_AW:0]  len_words = LEN;
    reg              pps = 1'b0;
    integer          cyc = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
        pps <= ((cyc % PPS_PERIOD) < 20);   // 20-cycle high pulse every PPS_PERIOD cycles
    end

    // ---- DUT ----
    wire [31:0]          bram_addr;
    wire [DATA_W-1:0]    bram_din, tdata;
    wire [DATA_W/8-1:0]  bram_we;
    wire                 bram_clk, bram_rst, bram_en, tvalid, running;
    wire [31:0]          pps_count, start_pps;
    reg  [DATA_W-1:0]    bram_dout;

    prn_player #(.DATA_W(DATA_W), .WORD_AW(WORD_AW), .READ_LAT(READ_LAT)) dut (
        .clk(clk), .rst_n(rst_n),
        .arm_async(arm), .free_run_async(1'b0), .len_words_async(len_words), .pps_async(pps),
        .bram_clk(bram_clk), .bram_rst(bram_rst), .bram_en(bram_en), .bram_we(bram_we),
        .bram_addr(bram_addr), .bram_din(bram_din), .bram_dout(bram_dout),
        .m_axis_tdata(tdata), .m_axis_tvalid(tvalid), .m_axis_tready(1'b1),
        .running(running), .pps_count(pps_count), .start_pps(start_pps)
    );

    // ---- block RAM model: READ_LAT-cycle latency, pattern 0xA500_0000 + word index ----
    function [DATA_W-1:0] word_pattern(input integer k);
        integer s;
        begin
            for (s = 0; s < DATA_W / 32; s = s + 1)
                word_pattern[32*s +: 32] = 32'hA500_0000 + k;
        end
    endfunction

    reg [DATA_W-1:0] rd_pipe [0:READ_LAT-1];
    integer p;
    always @(posedge clk) begin
        rd_pipe[0] <= word_pattern(bram_addr >> 5);         // byte address -> word index
        for (p = 1; p < READ_LAT; p = p + 1)
            rd_pipe[p] <= rd_pipe[p-1];
    end
    always @(*) bram_dout = rd_pipe[READ_LAT-1];

    // ---- checker ----
    integer errors = 0, n_seen = 0, expect = 0, pps_edge_cyc = -1, first_cyc = -1;
    reg pps_q = 1'b0;
    always @(posedge clk) begin
        pps_q <= pps;
        if (arm && pps && !pps_q && pps_edge_cyc < 0 && !running)
            pps_edge_cyc = cyc;                               // first PPS edge after arming

        if (tdata !== {DATA_W{1'b0}}) begin
            if (first_cyc < 0) first_cyc = cyc;
            if (tdata[31:0] !== 32'hA500_0000 + expect) begin
                errors = errors + 1;
                $display("ERROR at cycle %0d: got word %0d, expected %0d", cyc, tdata[31:0] - 32'hA500_0000, expect);
            end
            if (tdata !== word_pattern(expect)) begin
                errors = errors + 1;
                $display("ERROR at cycle %0d: slots of the word differ", cyc);
            end
            expect = (expect + 1) % LEN;
            n_seen = n_seen + 1;
        end else if (first_cyc >= 0 && arm) begin
            errors = errors + 1;                               // a gap while playing
            $display("ERROR at cycle %0d: zero word while playing", cyc);
        end
    end

    initial begin
        repeat (10) @(posedge clk);
        rst_n = 1'b1;
        repeat (PPS_PERIOD / 2) @(posedge clk);                // arm in the middle of a PPS period
        arm = 1'b1;
        $display("armed at cycle %0d", cyc);

        wait (n_seen >= N_CHECK);
        $display("first word on m_axis at cycle %0d, PPS edge at cycle %0d -> latency %0d cycles",
                 first_cyc, pps_edge_cyc, first_cyc - pps_edge_cyc);
        $display("pps_count = %0d, start_pps = %0d, tvalid = %b", pps_count, start_pps, tvalid);
        if (first_cyc <= pps_edge_cyc) begin
            errors = errors + 1;
            $display("ERROR: output started before the PPS edge");
        end

        @(posedge clk) arm = 1'b0;
        repeat (READ_LAT + 6) @(posedge clk);
        if (tdata !== {DATA_W{1'b0}}) begin
            errors = errors + 1;
            $display("ERROR: output not zero after disarming");
        end

        if (errors == 0) $display("PASS: %0d words checked, in order, no gaps", n_seen);
        else             $display("FAIL: %0d errors", errors);
        $finish;
    end

endmodule
