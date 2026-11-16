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
