What we did, start to finish

You can paste this into bench_notes.md.

1. Software stack

The RFSoC 4x2 has two halves on one chip:

the processing system: ARM cores running Linux;
the programmable logic: the FPGA fabric, plus the hard RF Data Converter blocks (DACs, ADCs, NCOs, digital mixers).

PYNQ is a Linux image for the ARM cores that lets Python control the programmable logic. It loads overlays (a bitstream plus a .hwh file describing its IP blocks) and exposes each block as a Python object. The RF Data Converters are controlled through the xrfdc driver. You use it all through Jupyter Lab, which the board serves over the network. Your code runs on the board; your browser is just the interface.

We used PYNQ v3.1.1, the release built with Vivado/PetaLinux 2024.1, which matches your Vivado installation. Custom overlays built later will be compatible with it.

2. SD card: what went wrong and how we fixed it
Wrong image. The first download was pynq_z2_v3.1.1.zip, for the PYNQ-Z2, a different board. We replaced it with rfsoc4x2_v3.1.1.img (about 9.8 GB).
Written with dd to /dev/sdb, the 14.8 GB card, using conv=fsync so dd only finishes once the data is physically on the card.
Boot failure. The board loaded its bootloader and bitstream (DONE and INIT LEDs lit) but never came up on the network. On the serial console we found a kernel panic: Unable to mount root fs on unknown-block(179,2). The boot partition was readable, but the Linux root partition wasn't.
Diagnosis.
fsck.ext4 -fn on the card found a heavily corrupted root filesystem.
The same check on the image file itself was clean, so the download was fine and the write had failed.
dmesg showed the cause: the USB card reader kept disconnecting and reconnecting (error -71) on a faulty front USB port. Each dropout during the 10 GB write corrupted blocks.
Fix. We rewrote the card through a rear USB port and verified it with cmp and fsck. The only remaining cmp difference was the ext4 last-mount-time field, from Ubuntu auto-mounting the card, which is harmless. The board then booted normally.
3. Serial console

The PROG_UART micro-USB port connects to an FTDI FT2232H chip. It shows up on the PC as two serial devices: interface if00 for JTAG (which Vivado uses) and if01 for the Linux serial console (/dev/ttyUSB1 in the final setup, at 115200 baud). The console showed the kernel panic and later gave us the first login. The repeated usb2-port1: Cannot enable messages come from the board's own USB 3 controller and are harmless.

4. Networking
The board's Ethernet port (eth0) tries DHCP first, then falls back to a fixed address, 192.168.2.99. Every PYNQ image uses that same backup address, so your two boards conflicted.
We changed this board to 192.168.2.100 by editing /etc/network/interfaces.d/ and applying it with ip addr. The other board stays at .99.
Your PC's eno1 port got 192.168.2.1/24.
Jupyter is at http://192.168.2.100/lab.
The board also has a USB network link (usb0, 192.168.3.1) on the USB DEVICE port, which we didn't use.
The OLED showing "no IP" is harmless; it only reports DHCP addresses.

To have the PC and both boards on one network at once, you'll need a small Ethernet switch.

5. The base overlay

We loaded the pre-built base.bit and initialized the RF clock chips (init_rf_clks()). Its radio subsystem contains:

2 DAC channels (transmitter channel_00 and channel_20). Each has an amplitude controller that feeds a constant value into the DAC's input. The DAC's fine mixer multiplies that constant by the NCO's complex exponential, which produces a tone at the NCO frequency. Later, the PRN waveform will enter the same path in place of the constant.
4 ADC channels, each with DMA engines that capture samples into memory, and from there into NumPy.
The RF Data Converter (radio/rfdc), which holds all NCO, Nyquist-zone, and mixer settings.

Readout of the default settings:

Setting	Value
DAC and ADC sample rate	fs = 4.9152 GSPS
DAC mixer	fine mixer, complex-to-real, NCO at 1228.8 MHz, Nyquist zone 1
ADC mixer	real-to-complex, NCO at −1228.8 MHz
FIFO status	no overflows or underflows

Two practical findings:

channel[0] drives the connector labeled DAC_B, not DAC_A.
A loose SMA connection initially hid the tone entirely. SMA connectors must be finger-tight.

The correct control interface is channel.control.gain, channel.control.enable, and dac_block.MixerSettings['Freq']. The gain readback shows 1.5 no matter what you write, probably a quirk of how the driver interprets the register, but the output behaves correctly.

6. The Nyquist-zone approach

A DAC sampling at rate fs doesn't output a single frequency. Its output contains copies (images) of the digital signal at k·fs ± f_NCO for every integer k. With fs = 4.9152 GHz, the zones are:

Zone	Range
1	0 – 2.4576 GHz
2	2.4576 – 4.9152 GHz
3	4.9152 – 7.3728 GHz ← 5.7 GHz is here

To put a tone at 5.7 GHz, set the NCO to the frequency whose image lands there:

f_NCO = 5.7 − 4.9152 = 0.7848 GHz

The images then fall at:

Image	Frequency
fundamental (f_NCO)	0.7848 GHz
fs − f_NCO	4.1304 GHz
fs + f_NCO	5.7000 GHz
2fs − f_NCO	9.0456 GHz (beyond the display)

How much power lands in each image depends on the DAC's output mode:

Normal mode (NRZ): each sample is held flat for one period Ts = 1/fs. The response is
|H(f)| = |sin(πf/fs) / (πf/fs)|,
which is highest at DC and falls to zero at fs. It favors zone 1.
Mix-mode (RF mode): each sample is output as +value for half a period and −value for the other half. The response is
|H(f)| = sin²(πf/2fs) / (πf/2fs),
which is suppressed near DC and peaks around 0.74·fs (about 3.6 GHz here). It favors zones 2 and 3.

On Gen 3 RFSoCs, setting the DAC's NyquistZone to 2 selects mix-mode. There's no zone-3 setting, but mix-mode's response is still strong in zone 3: at 5.7 GHz it's only about 3 dB below its peak. That's what makes this approach work.

7. Measurements (SA124B, direct cable)

Test A: NCO at 1000 MHz, zone 1 (normal mode), gain 0.5

Line	Measured	Theory, relative to the tone	Measured, relative to the tone	Extra loss
1.000 GHz (tone)	−6.6 dBm	0 dB	0 dB	reference
3.915 GHz	−19.8 dBm	−11.9 dB	−13.2 dB	1.4 dB
5.915 GHz	−27.8 dBm	−15.4 dB	−21.3 dB	5.8 dB

Test B: NCO at 784.8 MHz, zone 2 (mix-mode), gain 0.5

Line	Measured	Theory, relative to Test A's tone	Measured, relative to Test A's tone	Extra loss
0.785 GHz	−18.2 dBm	−11.6 dB	−11.6 dB	0.0 dB
4.130 GHz	−11.2 dBm	−2.4 dB	−4.6 dB	2.3 dB
5.700 GHz	−17.3 dBm	−5.2 dB	−10.7 dB	5.3 dB

Reading the tables:

The ideal DAC model matches exactly at low frequency (0.785 GHz: 0.0 dB error), which confirms that mix-mode is active and behaving as the formula predicts.
The "extra loss" column is the measured frequency response of the output path: baluns, board traces, SMA cable, and the analyzer's own flatness. It rises smoothly: 0 dB at 0.8 GHz, 1.4 dB at 3.9, 2.3 at 4.1, 5.3 at 5.7, and 5.8 at 5.9 GHz. The output path costs about 5–6 dB near your carrier. That's significant but workable, and it's the balun issue I flagged at the start, now quantified.

Frequency accuracy: the 5.7 GHz tone measured 5.699963 GHz, which is −37 ± 2 kHz, or −6.5 ppm. That combines the board's crystal error with the SA124B's internal reference error (INT REF). The close-in lines at −37 to −40 dBc (±11–28 kHz) are most likely the tone's own skirt, not real spurs.

8. What this means for the link design
Carrier generation is proven: the RFSoC produces a clean 5.7 GHz carrier at −17 dBm directly, with no external mixer or synthesizer.
About 38 dB of gain is needed to reach the 21 dBm bench target: a driver amplifier plus the PA.
The transmit bandpass filter must go before the PA. The 4.13 GHz image is 6 dB stronger than the carrier, 1.57 GHz away. The 0.785 GHz line must be filtered out too.
Optional improvement: in a custom overlay with fs around 7.86 GSPS, 5.7 GHz would sit in zone 2 near mix-mode's peak, gaining a few dB and moving the images further away.
Open item: lock the board's clocks, and the SA124B's reference input, to the FS725's 10 MHz. That removes the −6.5 ppm offset and is required for carrier-phase timing.