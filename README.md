# Hubble - A USB Recovery Tool for Exynos devices

## Why?

  - Why not?
  - I don't want people to be paying to unbrick their devices

## Environment preparation

### Linux

  - Run ```bash udev_rules.sh``` as root to setup udev rules
  - You can now run the tool.

### Windows

  - Uninstall any existing BootROM Drivers
  - Import the needed certificates by double clicking the ```## Driver Certificate (INSTALL ME FIRST BEFORE THE DRIVER).reg``` file and importing the regkeys
  - Install the ones provided by right clicking the inf and pressing install
  - You can now run the tool.

## How to use

  - Install required python packages via ```pip3 install -r requirements.txt```
  - Run the tool, pointing to your bootloader tar file via ```python3 hubble.py -b <PATH_TO_BL_TAR>```
  - Experimental profiles require the explicit ```--allow-experimental``` option.
  - Use ```--badcon``` for unstable USB links. It sends each EUB frame as consecutive 512-byte USB Bulk OUT transfers.
  - ```--badcon``` does not retry a failed chunk: the BootROM does not provide a confirmed per-chunk resume/ack mechanism in this tool, so retrying blindly could corrupt the stream.
  - Add ```--debug``` (or ```--debug-usb```) to log USB endpoint descriptors, enumeration state, and the start/result/duration of every Bulk OUT write. On Linux, also capture ```sudo dmesg -w``` in a second terminal.
  - Add ```--forensic``` for deeper diagnosis. It writes a millisecond-timestamped application log and, on Linux when permitted, captures an initial kernel log snapshot plus live USB events under ```logs/```. It records each DNW frame's size, header, trailer, SHA-256, prefix/suffix, and checks USB enumeration immediately after the first chunk of every stage and every 64 chunks.
  - Forensic mode does not read Bulk IN by default. ```--probe-in``` enables a 25 ms read from endpoint ```0x81``` after each complete frame; this can consume pending device data and should be used only as a separate diagnostic run.
  - The experimental Exynos7570 profile watches the BL2 → U-Boot boundary for USB re-enumeration. The fast path avoids reading string descriptors from the disconnected handle and skips configuration/descriptor probes after the new EUB instance appears. It matches the new device by VID:PID plus the exact same USB bus and physical port path, then claims interface 0 immediately.
  - The profile sends `u-boot.bin` as one libusb Bulk OUT request even when `--badcon` is enabled. The host controller still packetizes the request according to the endpoint's 512-byte maximum packet size, but Hubble avoids issuing roughly 1,672 separate Python/libusb write calls. This is based on the upstream Hubble transfer method; it is not yet hardware-validated on Exynos 7570.
  - Automatic whole-frame retries after a failed U-Boot transfer are disabled. A disconnected EUB instance does not prove that the BootROM reset its DNW receive state, so blindly retransmitting the frame is not safe. Hubble never resumes a partial frame.
  - The `0xD1000` U-Boot payload length also appears in the upstream Exynos7580 Hubble profile. That is useful related-device evidence, not proof that Exynos7570 accepts the same frame. Splitting U-Boot into multiple independent DNW frames is deliberately not attempted because the Exynos7570 protocol has not been shown to support appending separate DNW frames to one image.
  - Plug in your bricked phone
  - Let it run it's magic
  - Reflash the stock firmware

## Recovery demo

https://github.com/user-attachments/assets/78ca9dda-04c2-45d3-ad47-5a713086e33e

## Supported Devices

| Symbol | Meaning      |
|:------:|:------------:|
| ✅     | Working     |
| ❌     | Not Working |
| 🧪     | Experimental; successful recovery not yet confirmed |

### Exynos 9830 Devices

| Name             | Codename | Tested Firmware | Tested by                                             | State |
|:-----------------|:---------|:---------------:|:------------------------------------------------------|:-----:|
| Galaxy S20       | x1s      | `G981BXXSMHXK1` | [halal-beef](https://github.com/halal-beef)           | ✅    |
| Galaxy S20+      | y2s      | `G986BXXSNHYB1` | [Android-Artisan](https://github.com/Android-Artisan) | ✅    |
| Galaxy S20 Ultra | z3s      | `G988BXXSMHXK1` | [BotchedRPR](https://github.com/BotchedRPR)           | ✅    |
| Galaxy S20 FE    | r8s      | `G780FXXUDFWH5` | [Sota4Ever](https://github.com/Sota4Ever)             | ✅    |

### Exynos 9820 Devices

| Name          | Codename   | Tested Firmware | Tested by                                 | State |
|:--------------|:-----------|:---------------:|:------------------------------------------|:-----:|
| Galaxy S10    | beyond1lte | `G973FXXSGHWC2` | [Robotix](https://github.com/Robotix22)   | ✅    |
| Galaxy S10 5G | beyondx    | `G977BXXSEHWC1` | [Creeeeger](https://github.com/Creeeeger) | ✅    |

### Exynos 9810 Devices

| Name      | Codename   | Tested Firmware | Tested by                               | State |
|:----------|:-----------|:---------------:|:----------------------------------------|:-----:|
| Galaxy S9 | starlte    | `G960FXXUHFVG4` | [Robotix](https://github.com/Robotix22) | ✅    |
| Galaxy Note 10 Lite | r7      | `N770FXXS9HXA3` | [bavalucket](https://github.com/bavalucket) | ✅   |

### Exynos 9610 Devices

| Name       | Codename   | Tested Firmware | Tested by                                   | State |
|:-----------|:-----------|:---------------:|:--------------------------------------------|:-----:|
| Galaxy A51 | a51        | `A515FXXU8HWK1` | [AlexFurina](https://github.com/AlexFurina) |✅     |

### Exynos 7885 Devices

| Name       	   | Codename   | Tested Firmware | Tested by                                   | State |
|:-----------	   |:-----------|:---------------:|:--------------------------------------------|:-----:|
| Galaxy A8 (2018) | jackpotlte | `A530FXXULCUK6` | [kdetechy-mp4](https://github.com/kdetechy-mp4) |✅  |

### Exynos 7580 Devices

| Name             | Codename   | Tested Firmware | Tested by                                       | State |
|:-----------------|:-----------|:---------------:|:------------------------------------------------|:-----:|
| Galaxy A5 (2016) | a5xelte    | `A510FXXS8CTI7` | [Daniel224455](https://github.com/Daniel224455) | ✅    |

### Exynos 7570 Devices

| Name           | Codename | Tested Firmware | Profile Contributor | State |
|:---------------|:---------|:---------------:|:-------------------:|:-----:|
| Galaxy J5 Prime (SM-G570F) | on5xelte | `G570FXXS4CTJ8` | FogboundSloth25 (layout only) | 🧪 |

**Experimental profile notes:**

- USB EUB enumeration was confirmed on an SM-G570F: VID:PID `04e8:1234`, `iProduct` `Exynos7570`, `iInterface` `USB Booting v0.5`, interface 0, Bulk OUT `0x02`, Bulk IN `0x81`.
- The profile was derived from `G570FXXS4CTJ8`; its extracted `sboot.bin` SHA-256 is `505b9b9e8192b9fa4ee4bb2229e7934327444a47c408eaa6b300facb1c629841`. The tool pins this hash and refuses a different S-Boot image for this experimental profile.
- The candidate split map is FWBL1 `0x00000–0x02000`, BL31 `0x02000–0x20000`, BL2 `0x20000–0x2D000`, and U-Boot `0x2D000–0xFE000`. The first two boundaries are supported by the embedded size fields and the `daeh` marker; the remaining boundaries follow the Exynos7 split detector's signature/alignment heuristic and its `0xD1000` U-Boot size.
- The appended range `0xFE000–0x187110` is not mapped by this profile. It contains TEEgris-related strings, but its exact component boundaries and whether it is needed for EUB recovery have not been established. It is deliberately not sent.
- `response_support` is disabled pending direct confirmation of the Exynos 7570 response protocol. No successful Hubble recovery / transition to Download Mode has been reported yet.
- The user's debug trace showed USB device 102 disconnecting during the first U-Boot write, then the same `04e8:1234` device reappearing on the same bus/port with the same serial as device 103. The profile now waits 100 ms between boot stages, watches after `bl2.bin` for up to 10 seconds, and reclaims the interface on re-enumeration. This is based on that observed trace and remains unverified on hardware.
- Related Exynos 7580/8890 recovery work documents per-stage delays and USB re-enumeration on some variants. Those observations are not proof that Exynos 7570 has identical timing; they motivate the guarded, profile-specific transition handling. See [the Exynos 8890 transition report](https://github.com/ananjaser1211/exynos8890-exynos-usbdl-recovery/issues/3) and [the Exynos 7580 recovery project](https://github.com/MrSmoer/exynos7580-exynos-usbdl-recovery).
- `--badcon` uses 512-byte USB Bulk OUT write requests for stages not explicitly overridden by the profile. The Exynos7570 profile overrides it for U-Boot and submits one complete DNW frame. The 512-byte protocol research and the `0x4E800` receive-size limit are documented for Exynos 8895 only; neither establishes a packet or buffer limit for Exynos 7570.
- The profile is blocked by default. Review the risks before running Hubble with `--allow-experimental`.

## Credits

Thanks to these people:

[VDavid003](https://github.com/vdavid003) for Helping on the BL2 split for Exynos 9830. <br>
[gaitenis](https://xdaforums.com/m/gaitenis.13049039) found the LK Split for Exynos 9830. <br>
[halal-beef](https://github.com/halal-beef) for finding most of the Exynos 9830 Splits & for the initial Idea. <br>
[Robotix](https://github.com/Robotix22) for adding Support for Exynos 9820, 9810 & 7580. <br>
[alextrack2013](https://github.com/alextrack2013) for adding proper Linux Docs.
