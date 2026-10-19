# add_prn_player.tcl
#
# Modifies the RFSoC-PYNQ v3.1.1 base overlay block design so that DAC_B
# (radio/transmitter/channel_00) plays a waveform from block RAM (prn_player.v)
# instead of the amplitude controller's constant.
#
# SAMPLE SCRIPT: written against base.tcl v3.1.1 but not yet run. If a command
# fails, the step it belongs to is described in README.md (step 4) and can be
# done in the GUI instead.
#
# Use: open base/base.xpr in Vivado 2024.1 (after building the unchanged base
# overlay once), then in the Tcl console:
#     source <path to this repo>/fpga/add_prn_player.tcl

set here [file dirname [file normalize [info script]]]
set ch   radio/transmitter/channel_00          ;# DAC_B, RF Data Converter tile 228, rfdc/s00_axis

open_bd_design [get_files base.bd]

# ---- 1. player source --------------------------------------------------------
add_files -norecurse [file join $here prn_player.v]
update_compile_order -fileset sources_1

# ---- 2. remove the amplitude controller (its address segment goes with it) ----
delete_bd_objs [get_bd_cells $ch/amplitude_controller]

# ---- 3. new blocks inside channel_00 -----------------------------------------
# AXI SmartConnect: one AXI input (from the processor), two outputs
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 $ch/smartconnect
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {2}] [get_bd_cells $ch/smartconnect]

# AXI BRAM Controller: lets Python write the waveform, 256-bit wide like the DAC stream
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl:4.1 $ch/waveform_bram_ctrl
set_property -dict [list CONFIG.PROTOCOL {AXI4} CONFIG.DATA_WIDTH {256} CONFIG.SINGLE_PORT_BRAM {1}] \
    [get_bd_cells $ch/waveform_bram_ctrl]

# Block RAM: true dual port; port A = BRAM controller (processor), port B = player (DAC clock)
create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen:8.4 $ch/waveform_bram
set_property -dict [list CONFIG.Memory_Type {True_Dual_Port_RAM} CONFIG.use_bram_block {BRAM_Controller}] \
    [get_bd_cells $ch/waveform_bram]

# AXI GPIO: channel 1 = {free_run, arm} (2 bits), channel 2 = len_words (14 bits)
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 $ch/player_gpio
set_property -dict [list CONFIG.C_ALL_OUTPUTS {1} CONFIG.C_GPIO_WIDTH {2} \
                         CONFIG.C_IS_DUAL {1} CONFIG.C_ALL_OUTPUTS_2 {1} CONFIG.C_GPIO2_WIDTH {14}] \
    [get_bd_cells $ch/player_gpio]

# split GPIO channel 1 into its two bits
create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 $ch/arm_slice
set_property -dict [list CONFIG.DIN_WIDTH {2} CONFIG.DIN_FROM {0} CONFIG.DIN_TO {0}] [get_bd_cells $ch/arm_slice]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 $ch/free_slice
set_property -dict [list CONFIG.DIN_WIDTH {2} CONFIG.DIN_FROM {1} CONFIG.DIN_TO {1}] [get_bd_cells $ch/free_slice]

# the player itself (RTL module reference). READ_LAT must equal the block RAM's
# port B read latency: 1 in BRAM-controller mode without output registers.
# Check it in the Block Memory Generator's Summary tab after validation.
create_bd_cell -type module -reference prn_player $ch/prn_player
set_property CONFIG.READ_LAT {1} [get_bd_cells $ch/prn_player]

# ---- 4. connections ----------------------------------------------------------
connect_bd_intf_net [get_bd_intf_pins $ch/S_AXI]                      [get_bd_intf_pins $ch/smartconnect/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $ch/smartconnect/M00_AXI]       [get_bd_intf_pins $ch/waveform_bram_ctrl/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $ch/smartconnect/M01_AXI]       [get_bd_intf_pins $ch/player_gpio/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $ch/waveform_bram_ctrl/BRAM_PORTA] [get_bd_intf_pins $ch/waveform_bram/BRAM_PORTA]
connect_bd_intf_net [get_bd_intf_pins $ch/prn_player/BRAM_PORTB]      [get_bd_intf_pins $ch/waveform_bram/BRAM_PORTB]
connect_bd_intf_net [get_bd_intf_pins $ch/prn_player/M_AXIS]          [get_bd_intf_pins $ch/M_AXIS]

# everything on the channel's clock (clk_dac0, 307.2 MHz) and reset
connect_bd_net [get_bd_pins $ch/aclk] \
    [get_bd_pins $ch/smartconnect/aclk] [get_bd_pins $ch/waveform_bram_ctrl/s_axi_aclk] \
    [get_bd_pins $ch/player_gpio/s_axi_aclk] [get_bd_pins $ch/prn_player/clk]
connect_bd_net [get_bd_pins $ch/aresetn] \
    [get_bd_pins $ch/smartconnect/aresetn] [get_bd_pins $ch/waveform_bram_ctrl/s_axi_aresetn] \
    [get_bd_pins $ch/player_gpio/s_axi_aresetn] [get_bd_pins $ch/prn_player/rst_n]

# GPIO -> player controls
connect_bd_net [get_bd_pins $ch/player_gpio/gpio_io_o] [get_bd_pins $ch/arm_slice/Din] [get_bd_pins $ch/free_slice/Din]
connect_bd_net [get_bd_pins $ch/arm_slice/Dout]        [get_bd_pins $ch/prn_player/arm_async]
connect_bd_net [get_bd_pins $ch/free_slice/Dout]       [get_bd_pins $ch/prn_player/free_run_async]
connect_bd_net [get_bd_pins $ch/player_gpio/gpio2_io_o] [get_bd_pins $ch/prn_player/len_words_async]

# PPS: top-level port -> radio -> transmitter -> channel_00 -> player
create_bd_port -dir I pps_in
create_bd_pin  -dir I radio/pps_in
create_bd_pin  -dir I radio/transmitter/pps_in
create_bd_pin  -dir I $ch/pps_in
connect_bd_net [get_bd_ports pps_in]                 [get_bd_pins radio/pps_in]
connect_bd_net [get_bd_pins radio/pps_in]            [get_bd_pins radio/transmitter/pps_in]
connect_bd_net [get_bd_pins radio/transmitter/pps_in] [get_bd_pins $ch/pps_in]
connect_bd_net [get_bd_pins $ch/pps_in]              [get_bd_pins $ch/prn_player/pps_async]

# ---- 5. addresses (free slots in the base overlay's map) ----------------------
assign_bd_address -offset 0xB0130000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] \
    [get_bd_addr_segs $ch/player_gpio/S_AXI/Reg] -force
assign_bd_address -offset 0xB0140000 -range 0x00040000 \
    -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] \
    [get_bd_addr_segs $ch/waveform_bram_ctrl/S_AXI/Mem0] -force

# ---- 6. check, save, wrapper with the new pps_in port, constraints -------------
validate_bd_design
save_bd_design
make_wrapper -files [get_files base.bd] -top
add_files -fileset constrs_1 -norecurse [file join $here prn_player.xdc]

puts "prn_player added. Check: Address Editor (0xB0130000 GPIO, 0xB0140000 BRAM),"
puts "Block Memory Generator Summary (port B latency = READ_LAT = 1). Then Generate Bitstream."
