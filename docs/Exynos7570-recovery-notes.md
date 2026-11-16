# Exynos 7570 EUB recovery notes

**Status: experimental; no successful recovery has been confirmed.**

## Device and image facts verified locally

- Device: Samsung Galaxy J5 Prime SM-G570F, SoC string `Exynos7570`.
- USB identity: VID:PID `04e8:1234`; interface 0; Bulk IN `0x81`; Bulk OUT `0x02`; endpoint max packet size 512 bytes; descriptor string `USB Booting v0.5`.
- Firmware: `G570FXXS4CTJ8`; extracted `sboot.bin` size `0x187110`; SHA-256 `505b9b9e8192b9fa4ee4bb2229e7934327444a47c408eaa6b300facb1c629841`.
- Candidate split map is pinned to that exact S-Boot hash:
  - FWBL1: `0x00000–0x02000`
  - BL31/EPBL candidate: `0x02000–0x20000`
  - BL2 candidate: `0x20000–0x2D000`
  - U-Boot candidate: `0x2D000–0xFE000`
- The remaining `0xFE000–0x187110` range is not mapped or transmitted. Its exact component boundaries and role in this EUB path are unknown.

The BL1/EPBL size fields and `daeh` marker support the first boundaries. The later boundaries use the Exynos7 split-detector's signature/alignment heuristic and hard-coded `0xD1000` main-bootloader size. The detector's own README warns that inferred splits may be inaccurate:
https://github.com/halal-beef/sboot-split-detector

## What the observed trace proves

The host completed writes for FWBL1, BL31, and BL2. At the start of the first U-Boot write, the host waited 4.708 seconds and received libusb `ENODEV` / `No such device`.

The Linux kernel log for the same run showed:

1. EUB device 102 enumerated on bus 1, port path 1-7.
2. Device 102 disconnected.
3. The same VID:PID and same serial reappeared on that port as device 103.
4. Device 103 then disconnected again.

This proves a USB disconnect/re-enumeration occurred. It does **not** prove whether the cause was an intentional BootROM stage transition, a watchdog/reset caused by the image/protocol, or a physical USB fault.

## Research cross-checks and limits

- The Exynos BootROM reverse-engineering write-up describes the legacy `04e8:1234` interface, endpoint 2 Bulk OUT, a DNW-style data frame, and 512-byte USB transfers. Its reverse-engineered target was Exynos 8895, not Exynos 7570:
  https://fredericb.info/2020/06/reverse-engineer-usb-stack-of-exynos-bootrom.html
- Published Exynos 7580 recovery material uses FWBL1 → EL3_MON → BL2 → bootloader and inserts short pauses between images. It also documents intermittent USB re-enumeration and the need to time reconnection. This is a close legacy-EUB comparison, but not proof of identical Exynos 7570 behavior:
  https://github.com/MrSmoer/exynos7580-exynos-usbdl-recovery
  https://github.com/ananjaser1211/exynos8890-exynos-usbdl-recovery/issues/3
- The Exynos 7580 recovery material uses a `0xD1000` bootloader binary. Therefore, the 7570 U-Boot candidate's `0xD1000` length is not by itself evidence that the frame is too large. The 7580 and 7570 layouts are still different.
- The 321536-byte maximum reported in the Exynos 8895 reverse-engineering article is specific to the analyzed BootROM. It must not be generalized to Exynos 7570 without a 7570-specific measurement.
- Hubble's existing profiles show that some Exynos generations require a repeated FWBL1 or an EL3_MON stage, while others do not. No 7570-specific source currently confirms that an extra repeated stage is required, so this profile does not invent one.

## Current guarded change

After successfully sending `bl2.bin`, Hubble now watches for up to 10 seconds for the same EUB device to disappear and return on the same USB bus/port path with the same product and serial. When it returns, Hubble disposes the stale PyUSB handle, detaches the kernel driver if needed, reclaims interface 0, and only then begins U-Boot.

If no re-enumeration occurs and the original connection remains present, Hubble continues with the original handle. If the device disappears and does not return as the same EUB device, Hubble stops rather than writing to a stale handle.

This behavior is inferred from the user's captured trace and remains unverified on hardware. It is not a claim that the recovery is now confirmed.


## 2026-11-16 follow-up trace: disconnect after first U-Boot chunk

A later run using the profile-specific BL2 transition handling produced a more precise failure:

1. FWBL1, BL31, and BL2 were fully transferred.
2. After BL2, the EUB device disappeared and returned as `04e8:1234` on the same bus/port path `(7,)` with a new USB address. Hubble reclaimed interface 0 successfully.
3. U-Boot transmission began on the new handle.
4. U-Boot chunk 1 (512 bytes at offset `0x000`) returned success after approximately 1.131 seconds.
5. U-Boot chunk 2 (offset `0x200`) failed with `ENODEV` after approximately 0.002 seconds. The device was no longer enumerated.

This confirms that the BL2 reconnect handler is working. The current failure occurs inside the U-Boot DNW frame, immediately after its first 512-byte Bulk OUT transfer.

### Leading hypothesis to test: DNW frame-size limit

The U-Boot frame is `0xD100A` bytes including the 8-byte DNW header and 2-byte footer. A published reverse-engineering analysis of the Exynos 8895 BootROM describes a receive buffer of `0x4E800` bytes and a corresponding frame-size check. Exynos 7570 has not been proven to share this limit; this is **not** a conclusion. However, the DNW size field is in the first 8 bytes, so rejection immediately after the first block is consistent with a size-validation/reset hypothesis.

Source: https://fredericb.info/2020/06/reverse-engineer-usb-stack-of-exynos-bootrom.html

The Exynos 7580 recovery project uses a `0xD1000` bootloader file, which shows that a similarly sized stage can work on at least one nearby legacy Exynos EUB implementation. It does not prove identical receive-buffer or protocol behavior on Exynos 7570:
https://github.com/MrSmoer/exynos7580-exynos-usbdl-recovery

### Forensic mode

Run with `--forensic` to collect frame metadata, exact DNW header/footer, SHA-256, first/last 32 payload bytes, millisecond timestamps, monotonic relative timings, immediate USB checks after the first chunk and periodic checkpoints, same-port re-enumeration scans, plus an initial `dmesg` snapshot and live kernel USB log when permitted.

The default forensic mode does not read Bulk IN endpoint `0x81`, to avoid consuming unknown device messages. `--probe-in` is a separate opt-in test; it reads up to 512 bytes with a 25 ms timeout after each complete frame and can consume a pending response.

If the USB device disappears after a successful chunk, forensic mode stops and records any device returning on the same physical port. It does not resume an incomplete DNW frame because per-chunk resume semantics for Exynos 7570 are unknown.


## 2026-11-16 forensic run 22:14:18: kernel-level timing

The second forensic run confirms the same failure and exposes a flaw in the first forensic presence check.

### Host trace

- Device `04e8:1234` was enumerated on bus 1, port path `(7,)`, USB address 11.
- FWBL1, BL31, and BL2 DNW frames all completed. The payload SHA-256 values were recorded in the forensic log.
- After BL2, address 11 disconnected and the same Exynos serial re-enumerated on the same port as address 12. Hubble reclaimed interface 0.
- The U-Boot DNW frame is internally size-consistent: total frame and declared size are both `0xD100A`; the header is `1b 44 4e 57 0a 10 0d 00`; footer is `ff ff`; payload length is `0xD1000`.
- The first U-Boot 512-byte Bulk OUT call returned success after `1.118573 s`. The second call at offset `0x200` failed with `ENODEV` after `0.001104 s`.

### Kernel trace, same wall-clock time

- `22:14:27.342`: xHCI enumerated a new high-speed device on `1-7`.
- `22:14:27.469`: kernel identified it as Exynos7570 / `04e8:1234`, serial ending `...006D7`.
- `22:14:28.634`: kernel logged `USB disconnect, device number 12`.
- `22:14:28.879`: xHCI detected a *low-speed* device on the same port.
- From `22:14:28.993` onward, descriptor reads failed with `-71` (USB protocol error / `EPROTO`); the controller attempted a port power cycle and still could not enumerate the device.

The kernel disconnect timestamp is approximately 327 ms **before** Hubble logged the first U-Boot Bulk OUT call as successful. Therefore the host-side "write succeeded" means libusb completed that transfer from its perspective; it does not prove the device stayed connected through the completion callback.

### Important diagnostic correction

The first forensic USB check called `usb.core.find()` and reported the old identity as still present. The kernel log shows that result can be stale during the disconnect race. The forensic code now follows enumeration with a standard, read-only USB EP0 `GET_DESCRIPTOR` request. That checks whether the physical device actually answers, rather than trusting enumeration metadata alone.

The previous same-port fallback also treated a device with `port_numbers=None` as matching the target port. This incorrectly included the xHCI root-hub controller in the "same port" report. Port-path matching is now strict; devices without the matching physical port path are excluded.

### What can and cannot be concluded

**Confirmed:** the device physically disconnects while the U-Boot first-block transfer is in progress; xHCI then sees a low-speed/protocol-error condition and cannot re-enumerate Exynos7570. This is not merely a Python stale-handle exception.

**Not confirmed:** whether the trigger is BootROM rejection of the U-Boot frame/header, a device-side reset/watchdog, an electrical/PHY issue, or the long-held hardware key. The captured frame header and size field are self-consistent.

**Size-limit hypothesis remains unproven:** the `0x4E800` receive-buffer limit is documented for Exynos8895, not Exynos7570. A published Exynos7580 recovery uses a `0xD1000` bootloader stage, which shows that this length is possible on a related but different legacy Exynos EUB implementation. Do not truncate the 7570 U-Boot based only on the Exynos8895 value.

### Timing clue worth testing

The kernel log contains repeated EUB connect/disconnect cycles before this run as well. In the 22:14 run, the first EUB enumeration lasted about 6.43 seconds before the post-BL2 disconnect/reconnect, then the next valid enumeration lasted about 1.16 seconds before the U-Boot-associated disconnect. Related Exynos recovery documentation explicitly warns that a long-held POWER key can reset the USB connection:
https://github.com/ananjaser1211/exynos8890-exynos-usbdl-recovery

This is a plausible confounder, not proof: the cited guide covers Exynos8890/7580 variants, not SM-G570F/Exynos7570. The exact hardware key being held and whether its long-press reset threshold applies to this board must be confirmed separately.
