#!/usr/bin/env python3
from pathlib import Path
import re
import subprocess
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: apply-usbfs-fixes.py <extracted-hubble-android-directory>")
root = Path(sys.argv[1])
go_path = root / "usbhost/main.go"
java_path = root / "app/src/main/java/com/validity/hubble7570/MainActivity.java"
if not go_path.is_file():
    raise SystemExit(f"missing Go helper: {go_path}")

subprocess.run(["gofmt", "-w", str(go_path)], check=True)
s = go_path.read_text(encoding="utf-8")

if "usbDevfsSubmitURB" not in s:
    m = re.search(r"(?m)^(\s*usbDevfsBulk\s*=.*)$", s)
    if not m:
        raise SystemExit("cannot locate USBDEVFS_BULK constant")
    add = m.group(1) + "\n" + "\n".join([
        "\tusbDevfsSubmitURB       = 0x8038550A // _IOR('U', 10, struct usbdevfs_urb), 56 bytes",
        "\tusbDevfsDiscardURB      = 0x0000550B // _IO('U', 11)",
        "\tusbDevfsReapURB         = 0x4008550C // _IOW('U', 12, void *)",
        "\tusbDevfsReapURBNDelay   = 0x4008550D // _IOW('U', 13, void *)",
        "\tusbDevfsGetCapabilities = 0x8004551A // _IOR('U', 26, __u32)",
    ])
    s = s[:m.start()] + add + s[m.end():]

if "type usbdevfsURB struct" not in s:
    m = re.search(r"type bulkTransfer struct \{.*?\n\}", s, re.S)
    if not m:
        raise SystemExit("cannot locate bulkTransfer structure")
    s = s[:m.end()] + """

// usbdevfsURB matches struct usbdevfs_urb from linux/usbdevice_fs.h on 64-bit Linux.
type usbdevfsURB struct {
	Type            uint8
	Endpoint        uint8
	Pad             [2]byte
	Status          int32
	Flags           uint32
	Buffer          uintptr
	BufferLength    int32
	ActualLength    int32
	StartFrame      int32
	NumberOfPackets int32
	ErrorCount      int32
	Signr           uint32
	UserContext     uintptr
}
""" + s[m.end():]

if "func validateEUBEndpoints(" not in s:
    anchor = "func openAndClaim(d usbDevice) (*os.File, error) {"
    if anchor not in s:
        raise SystemExit("cannot locate openAndClaim")
    funcs = r'''func validateEndpoint(d usbDevice, address uint8) error {
	name := fmt.Sprintf("ep_%02X", address)
	base := d.SysfsPath + ":1.0/" + name
	gotAddress := readTrim(filepath.Join(base, "bEndpointAddress"))
	attributes := readTrim(filepath.Join(base, "bmAttributes"))
	packet := readTrim(filepath.Join(base, "wMaxPacketSize"))
	if gotAddress == "" || attributes == "" || packet == "" {
		return fmt.Errorf("endpoint %s descriptor is not available under %s", name, base)
	}
	addr, err := strconv.ParseUint(gotAddress, 16, 8)
	if err != nil || uint8(addr) != address {
		return fmt.Errorf("endpoint %s address mismatch: %q", name, gotAddress)
	}
	attrs, err := strconv.ParseUint(attributes, 16, 8)
	if err != nil || attrs&0x03 != 0x02 {
		return fmt.Errorf("endpoint 0x%02X is not Bulk (bmAttributes=%q)", address, attributes)
	}
	maxPacket, err := strconv.ParseUint(packet, 16, 16)
	if err != nil || maxPacket != 512 {
		return fmt.Errorf("endpoint 0x%02X max packet is not 512 bytes (wMaxPacketSize=%q)", address, packet)
	}
	logf("USB endpoint verified address=0x%02X type=Bulk max_packet=%d", address, maxPacket)
	return nil
}

func validateEUBEndpoints(d usbDevice) error {
	if err := validateEndpoint(d, 0x02); err != nil { return err }
	if err := validateEndpoint(d, 0x81); err != nil { return err }
	return nil
}

func logUSBfsCapabilities(f *os.File) {
	var caps uint32
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), usbDevfsGetCapabilities, uintptr(unsafe.Pointer(&caps)))
	runtime.KeepAlive(&caps)
	if errno != 0 {
		logf("USBFS capabilities unavailable: %v", errno)
		return
	}
	logf("USBFS capabilities=0x%X bulk_scatter_gather=%t", caps, caps&0x08 != 0)
}

'''
    s = s.replace(anchor, funcs + anchor, 1)
    s = s.replace(anchor, anchor + "\n\tif err := validateEUBEndpoints(d); err != nil { return nil, err }", 1)
    start = s.index(anchor)
    end = s.index("\n}", start)
    part = s[start:end].replace("\treturn f, nil", "\tlogUSBfsCapabilities(f)\n\treturn f, nil", 1)
    s = s[:start] + part + s[end:]

if "func bulkWriteURB(" not in s:
    anchor = "func sendStage(f *os.File, framesDir, filename string) error {"
    if anchor not in s:
        raise SystemExit("cannot locate sendStage")
    fn = r'''// bulkWriteURB submits one complete DNW frame as one asynchronous USB bulk URB.
// This mirrors upstream Hubble's single libusb Bulk OUT request for Exynos 7570 U-Boot.
func bulkWriteURB(f *os.File, data []byte) (int, error) {
	if len(data) == 0 { return 0, errors.New("refusing empty DNW frame") }
	if len(data) > int(^uint32(0)>>1) { return 0, fmt.Errorf("URB frame too large: %d", len(data)) }
	if unsafe.Sizeof(usbdevfsURB{}) != 56 { return 0, fmt.Errorf("usbdevfs_urb ABI size mismatch: got %d, expected 56", unsafe.Sizeof(usbdevfsURB{})) }

	urb := usbdevfsURB{Type: 3, Endpoint: outEndpoint, Buffer: uintptr(unsafe.Pointer(&data[0])), BufferLength: int32(len(data))}
	started := time.Now()
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), usbDevfsSubmitURB, uintptr(unsafe.Pointer(&urb)))
	runtime.KeepAlive(data)
	runtime.KeepAlive(&urb)
	if errno != 0 { return 0, fmt.Errorf("USBDEVFS_SUBMITURB length=%d: %w", len(data), errno) }
	logf("OUT URB SUBMITTED bytes=%d endpoint=0x%02X; waiting for one frame completion", len(data), outEndpoint)

	deadline := started.Add(time.Duration(transferTimeoutMs) * time.Millisecond)
	for {
		var completed uintptr
		_, _, reapErr := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), usbDevfsReapURBNDelay, uintptr(unsafe.Pointer(&completed)))
		runtime.KeepAlive(&completed)
		runtime.KeepAlive(data)
		runtime.KeepAlive(&urb)
		if reapErr == 0 {
			if completed != uintptr(unsafe.Pointer(&urb)) { return 0, fmt.Errorf("REAPURBNDELAY returned unexpected URB pointer 0x%X", completed) }
			actual := int(urb.ActualLength)
			if urb.Status != 0 { return actual, fmt.Errorf("bulk URB status=%d actual=%d/%d: %s", urb.Status, actual, len(data), syscall.Errno(-urb.Status)) }
			if actual != len(data) { return actual, fmt.Errorf("short bulk URB completion: %d/%d bytes", actual, len(data)) }
			logf("OUT URB COMPLETE bytes=%d elapsed=%s status=0", actual, time.Since(started))
			return actual, nil
		}
		if reapErr != syscall.EAGAIN {
			return 0, fmt.Errorf("USBDEVFS_REAPURBNDELAY: %w (URB may have been interrupted)", reapErr)
		}
		if time.Now().After(deadline) {
			_, _, discardErr := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), usbDevfsDiscardURB, uintptr(unsafe.Pointer(&urb)))
			runtime.KeepAlive(&urb)
			if discardErr != 0 { return 0, fmt.Errorf("URB timeout; USBDEVFS_DISCARDURB failed: %w", discardErr) }
			var cancelled uintptr
			_, _, reapCancelErr := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), usbDevfsReapURB, uintptr(unsafe.Pointer(&cancelled)))
			runtime.KeepAlive(&cancelled)
			runtime.KeepAlive(data)
			runtime.KeepAlive(&urb)
			if reapCancelErr != 0 { return 0, fmt.Errorf("URB timeout; cancelled URB reap failed: %w", reapCancelErr) }
			return int(urb.ActualLength), fmt.Errorf("bulk URB timed out after %s; partial frame NOT retried", time.Since(started))
		}
		time.Sleep(5 * time.Millisecond)
	}
}

'''
    s = s.replace(anchor, fn + anchor, 1)

if "mode=single-async-URB" not in s:
    log_anchor = '\tlogf("OUT BEGIN stage=%s frame=%d bytes (0x%X) endpoint=0x%02X timeout=%dms", filename, len(data), len(data), outEndpoint, transferTimeoutMs)\n'
    if log_anchor not in s:
        raise SystemExit("cannot locate sendStage OUT BEGIN log")
    log_new = '''\tif filename == "u-boot.dnw" {
\t\tlogf("OUT BEGIN stage=%s frame=%d bytes (0x%X) endpoint=0x%02X mode=single-async-URB timeout=%dms", filename, len(data), len(data), outEndpoint, transferTimeoutMs)
\t} else {
\t\tlogf("OUT BEGIN stage=%s frame=%d bytes (0x%X) endpoint=0x%02X mode=4096-byte-sync-chunks timeout=%dms", filename, len(data), len(data), outEndpoint, transferTimeoutMs)
\t}
'''
    s = s.replace(log_anchor, log_new, 1)
    write_anchor = '\tn, err := bulkWrite(f, data) // one USBFS bulk request, including the entire U-Boot frame\n'
    if write_anchor not in s:
        raise SystemExit("cannot locate sendStage bulkWrite call")
    write_new = '''\tvar n int
\tif filename == "u-boot.dnw" {
\t\tn, err = bulkWriteURB(f, data)
\t} else {
\t\tn, err = bulkWrite(f, data)
\t}
'''
    s = s.replace(write_anchor, write_new, 1)

if "func waitForOptionalSamePort(" not in s:
    marker = "func waitForSamePort(old usbDevice, timeout time.Duration) (usbDevice, error) {"
    start = s.find(marker)
    if start < 0: raise SystemExit("cannot locate waitForSamePort")
    end = s.index("\n}\n", start) + 3
    wait = r'''
// Observe the optional BL31 reset. Once disconnect is seen, never reuse the stale usbfs fd.
func waitForOptionalSamePort(old usbDevice, observeWindow, timeout time.Duration) (usbDevice, bool, error) {
	logf("watching for optional EUB re-enumeration after BL31 on port %s (bus=%d old-devnum=%d); observe=%s timeout=%s", old.Name, old.Bus, old.Dev, observeWindow, timeout)
	started := time.Now()
	observeDeadline := started.Add(observeWindow)
	deadline := started.Add(timeout)
	sawGone := false
	for time.Now().Before(deadline) {
		var current *usbDevice
		for _, d := range discover() {
			if d.Name == old.Name && d.Bus == old.Bus { copy := d; current = &copy; break }
		}
		if current == nil {
			if !sawGone { logf("EUB disappeared after BL31; waiting for it to return on the same physical port") }
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
	if sawGone { return usbDevice{}, false, fmt.Errorf("EUB disappeared after BL31 but did not return on sysfs port %s within %s", old.Name, timeout) }
	return old, false, nil
}
'''
    s = s[:end] + wait + s[end:]

if "waitForOptionalSamePort(d" not in s:
    start = s.find('\tstages := []string{"fwbl1.dnw", "bl31.dnw", "bl2.dnw", "u-boot.dnw"}')
    if start < 0: raise SystemExit("cannot locate run stage loop")
    end_marker = '\n\treleaseAndClose(f)\n\tlogf("all four DNW frames accepted by USBFS; this does NOT prove bootloader execution or Download Mode")'
    end = s.find(end_marker, start)
    if end < 0: raise SystemExit("cannot locate end of run stage loop")
    loop = '''\tstages := []string{"fwbl1.dnw", "bl31.dnw", "bl2.dnw", "u-boot.dnw"}
\tfor _, stage := range stages {
\t\tif err := sendStage(f, framesDir, stage); err != nil {
\t\t\treleaseAndClose(f)
\t\t\treturn err
\t\t}
\t\tif stage != "u-boot.dnw" {
\t\t\tlogf("settling for 100ms after %s before the next boot stage", stage)
\t\t\ttime.Sleep(100 * time.Millisecond)
\t\t}
\t\tif stage == "bl31.dnw" {
\t\t\tnext, changed, err := waitForOptionalSamePort(d, time.Second, 10*time.Second)
\t\t\tif err != nil { releaseAndClose(f); return err }
\t\t\treleaseAndClose(f)
\t\t\tf = nil
\t\t\td = next
\t\t\tf, err = openAndClaim(d)
\t\t\tif err != nil { return fmt.Errorf("USBFS interface could not be reclaimed after BL31 (reenumerated=%t): %w", changed, err) }
\t\t\tlogf("USBFS interface 0 reopened after BL31; ready for BL2 (reenumerated=%t)", changed)
\t\t} else if stage == "bl2.dnw" {
\t\t\treleaseAndClose(f)
\t\t\tf = nil
\t\t\tnext, err := waitForSamePort(d, 10*time.Second)
\t\t\tif err != nil { return err }
\t\t\td = next
\t\t\tf, err = openAndClaim(d)
\t\t\tif err != nil { return fmt.Errorf("re-enumerated EUB found but interface claim failed: %w", err) }
\t\t\tlogf("interface 0 reclaimed; ready for U-Boot")
\t\t}
\t}
'''
    s = s[:start] + loop + s[end:]

go_path.write_text(s, encoding="utf-8")
subprocess.run(["gofmt", "-w", str(go_path)], check=True)

if java_path.is_file():
    java = java_path.read_text(encoding="utf-8")
    java = java.replace(
        "U-Boot будет отправлен одним USBFS Bulk OUT запросом.",
        "U-Boot будет отправлен одной асинхронной USBFS Bulk URB-заявкой (один цельный DNW-кадр)."
    )
    java_path.write_text(java, encoding="utf-8")

print("Applied: validated EUB endpoints, logged usbfs capabilities, retained BL31/BL2 reconnect fixes, and one async URB for U-Boot.")

# Keep this PR diff focused on exercising the APK build workflow.
