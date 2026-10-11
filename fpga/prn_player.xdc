## prn_player.xdc
## Constraints to add to the base overlay's base.xdc for the PPS-triggered player.

## ---- PPS input ----
## The RFSoC 4x2 "Pulse-per-second" SMA goes through a comparator (LMV7235) to
## FPGA pin AJ13, net IRIG_COMP_OUT (RFSoC 4x2 Reference Manual, Appendix A,
## "1PPS Control"). The Schmitt-trigger version, IRIG_TRIG_OUT, is on AH13.
## LVCMOS18 matches the other user I/O near this pin (PMOD, LEDs); if Vivado
## reports a bank-voltage conflict, check the bank voltage in the manual.
set_property PACKAGE_PIN AJ13     [get_ports pps_in]
set_property IOSTANDARD  LVCMOS18 [get_ports pps_in]

## ---- clock-domain crossings into prn_player ----
## pps_in is asynchronous; arm and len_words come from the processor side.
## All of them go through the two-flip-flop synchronizers in prn_player, so the
## first flip-flop must not be timed against the source.
set_false_path -to [get_cells -hierarchical -filter {NAME =~ *prn_player*/pps_meta_reg}]
set_false_path -to [get_cells -hierarchical -filter {NAME =~ *prn_player*/arm_meta_reg}]
set_false_path -to [get_cells -hierarchical -filter {NAME =~ *prn_player*/free_meta_reg}]
## len_words is a multi-bit bus, only sampled while disarmed (see prn_player.v)
set_false_path -to [get_cells -hierarchical -filter {NAME =~ *prn_player*/len_reg_reg[*]}]
