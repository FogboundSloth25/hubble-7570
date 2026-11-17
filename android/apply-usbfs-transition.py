#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: apply-usbfs-transition.py <usbhost/main.go>")

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")

if 'func waitForOptionalSamePort(' in source:
    print("BL31 USB transition handling already present")
    raise SystemExit(0)

old_comment = 'n, err := bulkWrite(f, data) // one USBFS bulk request, including the entire U-Boot frame'
new_comment = 'n, err := bulkWrite(f, data) // ordered USBFS chunks preserve one contiguous DNW byte stream'
if old_comment not in source:
    raise SystemExit("cannot find expected USBFS bulkWrite call; source archive revision does not match")
source = source.replace(old_comment, new_comment, 1)

wait_anchor = '''func waitForSamePort(old usbDevice, timeout time.Duration) (usbDevice, error) {
    logf("waiting for EUB re-enumeration on physical OTG port %s (bus=%d old-devnum=%d), timeout=%s", old.Name, old.Bus, old.Dev, timeout)
    deadline := time.Now().Add(timeout)
    sawGone := false
    for time.Now().Before(deadline) {
        var current *usbDevice
        for _, d := range discover() {
            if d.Name == old.Name && d.Bus == old.Bus {
                copy := d
                current = &copy
                break
            }
        }
        if current == nil {
            sawGone = true
        } else if sawGone || current.Dev != old.Dev {
            logf("EUB returned on same port: %s bus=%d devnum=%d product=%q", current.Name, current.Bus, current.Dev, current.Product)
            return *current, nil
        }
        time.Sleep(10*time.Millisecond)
    }
    return usbDevice{}, fmt.Errorf("EUB did not return on sysfs port %s within %s", old.Name, timeout)
}
'''
wait_add = '''
// waitForOptionalSamePort watches a stage boundary where the BootROM may reset
// the USB device. If no disconnect/re-enumeration is seen during observeWindow,
// the current device remains available and the next stage can proceed. Once a
// disconnect is observed, the function waits for the same physical USB port to
// return and never allows writes through the stale USBFS handle.
func waitForOptionalSamePort(old usbDevice, observeWindow, timeout time.Duration) (usbDevice, bool, error) {
    logf("watching for optional EUB re-enumeration after BL31 on port %s (bus=%d old-devnum=%d); observe=%s timeout=%s", old.Name, old.Bus, old.Dev, observeWindow, timeout)
    started := time.Now()
    observeDeadline := started.Add(observeWindow)
    deadline := started.Add(timeout)
    sawGone := false

    for time.Now().Before(deadline) {
        var current *usbDevice
        for _, d := range discover() {
            if d.Name == old.Name && d.Bus == old.Bus {
                copy := d
                current = &copy
                break
            }
        }

        if current == nil {
            if !sawGone {
                logf("EUB disappeared after BL31; waiting for it to return on the same physical port")
            }
            sawGone = true
        } else if sawGone || current.Dev != old.Dev {
            logf("EUB re-enumerated after BL31: %s bus=%d devnum=%d product=%q", current.Name, current.Bus, current.Dev, current.Product)
            return *current, true, nil
        } else if !sawGone && !time.Now().Before(observeDeadline) {
            logf("no USB re-enumeration observed after BL31 within %s; keeping the current device", observeWindow)
            return old, false, nil
        }

        time.Sleep(10 * time.Millisecond)
    }

    if sawGone {
        return usbDevice{}, false, fmt.Errorf("EUB disappeared after BL31 but did not return on sysfs port %s within %s; refusing to use the stale USB handle", old.Name, timeout)
    }
    return old, false, nil
}
'''
if wait_anchor not in source:
    raise SystemExit("cannot find expected waitForSamePort function; source archive revision does not match")
source = source.replace(wait_anchor, wait_anchor + wait_add, 1)

run_anchor = '''    stages := []string{"fwbl1.dnw", "bl31.dnw", "bl2.dnw", "u-boot.dnw"}
    for i, stage := range stages {
        if err := sendStage(f, framesDir, stage); err != nil {
            releaseAndClose(f)
            return err
        }
        if stage == "bl2.dnw" {
            releaseAndClose(f)
            f = nil
            next, err := waitForSamePort(d, 10*time.Second)
            if err != nil { return err }
            d = next
            f, err = openAndClaim(d)
            if err != nil { return fmt.Errorf("re-enumerated EUB found but interface claim failed: %w", err) }
            logf("interface 0 reclaimed; ready for U-Boot")
        } else if i == len(stages)-1 {
            // Nothing follows U-Boot in this experimental profile.
        }
    }
'''
run_replacement = '''    stages := []string{"fwbl1.dnw", "bl31.dnw", "bl2.dnw", "u-boot.dnw"}
    for i, stage := range stages {
        if err := sendStage(f, framesDir, stage); err != nil {
            releaseAndClose(f)
            return err
        }

        // Match the Exynos 7570 profile's 100 ms inter-stage settling delay.
        if i < len(stages)-1 {
            logf("settling for 100ms after %s before the next boot stage", stage)
            time.Sleep(100 * time.Millisecond)
        }

        if stage == "bl31.dnw" {
            // The device trace showed USB endpoint shutdown at the first BL2
            // write. BL31 can reset/re-enumerate the EUB interface, so watch
            // this boundary before attempting BL2 rather than writing to a
            // potentially stale /dev/bus/usb handle.
            next, changed, err := waitForOptionalSamePort(d, 1*time.Second, 10*time.Second)
            if err != nil {
                releaseAndClose(f)
                return err
            }

            // Reopen the usbfs node even when devnum did not change. Linux may
            // reuse the same devnum after a fast reset, while the old fd still
            // refers to the shut-down endpoint instance.
            releaseAndClose(f)
            f = nil
            d = next
            f, err = openAndClaim(d)
            if err != nil { return fmt.Errorf("USBFS interface could not be reclaimed after BL31 (reenumerated=%t): %w", changed, err) }
            logf("USBFS interface 0 reopened after BL31; ready for BL2 (reenumerated=%t)", changed)
        } else if stage == "bl2.dnw" {
            releaseAndClose(f)
            f = nil
            next, err := waitForSamePort(d, 10*time.Second)
            if err != nil { return err }
            d = next
            f, err = openAndClaim(d)
            if err != nil { return fmt.Errorf("re-enumerated EUB found but interface claim failed: %w", err) }
            logf("interface 0 reclaimed; ready for U-Boot")
        }
    }
'''
if run_anchor not in source:
    raise SystemExit("cannot find expected run-stage loop; source archive revision does not match")
source = source.replace(run_anchor, run_replacement, 1)
path.write_text(source, encoding="utf-8")
print(f"Applied BL31 USB transition handling to {path}")
