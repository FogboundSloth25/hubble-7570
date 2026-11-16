import json
import hashlib
import shutil
import usb.core
import usb.util
import usb.backend.libusb1
import libusb
import tarfile
import lz4.frame
import argparse
import struct
import sys
import os
import coloredlogs
import logging
import atexit
import subprocess
from datetime import datetime
from time import sleep, monotonic, perf_counter_ns
import re

EXYNOS_DATA = None

soc     = ""
logger  = logging.getLogger(__name__)
verbose = False
forensic_mode = False
probe_bulk_in = False
forensic_start_ns = None
forensic_log_path = None
kernel_log_process = None
kernel_log_stream = None
forensic_file_handler = None
allow_experimental = False
badcon = False
debug_usb = False
USB_BACKEND = None


class USBStreamInterrupted(RuntimeError):
    """A DNW frame was interrupted by a USB disconnect or short write."""


def forensic_elapsed():
    if forensic_start_ns is None:
        return 0.0
    return (perf_counter_ns() - forensic_start_ns) / 1_000_000_000


def stop_kernel_capture():
    global kernel_log_process, kernel_log_stream
    if kernel_log_process is not None:
        try:
            kernel_log_process.terminate()
            kernel_log_process.wait(timeout=2)
        except Exception:
            try:
                kernel_log_process.kill()
            except Exception:
                pass
        kernel_log_process = None
    if kernel_log_stream is not None:
        try:
            kernel_log_stream.flush()
            kernel_log_stream.close()
        except Exception:
            pass
        kernel_log_stream = None


def start_forensic_capture():
    global forensic_log_path, forensic_file_handler
    global kernel_log_process, kernel_log_stream, forensic_start_ns

    forensic_start_ns = perf_counter_ns()
    os.makedirs("logs", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    forensic_log_path = os.path.join("logs", f"hubble-forensic-{stamp}.log")
    kernel_log_path = os.path.join("logs", f"hubble-dmesg-{stamp}.log")

    file_handler = logging.FileHandler(forensic_log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    forensic_file_handler = file_handler
    logger.warning("FORENSIC: application log: %s", forensic_log_path)
    logger.warning("FORENSIC: monotonic relative timestamps enabled")

    if os.name == "nt":
        logger.warning("FORENSIC: automatic dmesg capture is unavailable on Windows")
        return

    try:
        kernel_log_stream = open(kernel_log_path, "w", encoding="utf-8")
        snapshot = subprocess.run(
            ["dmesg", "--time-format=iso"],
            stdout=kernel_log_stream,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=4,
            check=False
        )
        kernel_log_stream.write(
            "\n===== LIVE KERNEL LOG (dmesg --follow --time-format=iso) =====\n"
        )
        kernel_log_stream.flush()
        kernel_log_process = subprocess.Popen(
            ["dmesg", "--follow", "--time-format=iso"],
            stdout=kernel_log_stream,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        atexit.register(stop_kernel_capture)
        logger.warning(
            "FORENSIC: kernel log: %s (snapshot exit=%s, follow PID=%s)",
            kernel_log_path, snapshot.returncode, kernel_log_process.pid
        )
    except Exception as e:
        logger.warning("FORENSIC: automatic dmesg capture failed: %s", e)
        stop_kernel_capture()


def _usb_descriptor_string(device, descriptor_index):
    if not descriptor_index:
        return None
    try:
        value = usb.util.get_string(device, descriptor_index)
        return value.strip("\0") if value else None
    except Exception as e:
        return f"<unreadable: {type(e).__name__}: {e}>"


def _usb_identity(device, include_strings=False):
    identity = {
        "vid": getattr(device, "idVendor", None),
        "pid": getattr(device, "idProduct", None),
        "bus": getattr(device, "bus", None),
        "address": getattr(device, "address", None),
        "ports": getattr(device, "port_numbers", None),
        "speed": getattr(device, "speed", None),
        "bcdUSB": getattr(device, "bcdUSB", None),
        "bcdDevice": getattr(device, "bcdDevice", None),
        "bMaxPacketSize0": getattr(device, "bMaxPacketSize0", None),
    }
    if include_strings:
        identity.update({
            "manufacturer": _usb_descriptor_string(device, getattr(device, "iManufacturer", 0)),
            "product": _usb_descriptor_string(device, getattr(device, "iProduct", 0)),
            "serial": _usb_descriptor_string(device, getattr(device, "iSerialNumber", 0)),
        })
    return identity


def _devices_on_same_port(reference_device):
    ref_bus = getattr(reference_device, "bus", None)
    ref_ports = getattr(reference_device, "port_numbers", None)
    try:
        devices = list(usb.core.find(find_all=True, backend=USB_BACKEND) or [])
    except Exception as e:
        logger.warning("FORENSIC USB ENUMERATION: find_all failed: %s: %s", type(e).__name__, e)
        return []

    same_port = []
    for candidate in devices:
        bus_matches = ref_bus is None or getattr(candidate, "bus", None) == ref_bus
        candidate_ports = getattr(candidate, "port_numbers", None)

        # A missing port path is not proof that a device is on the target port.
        # In particular, the xHCI root-hub device itself often has ports=None.
        if ref_ports is not None:
            ports_match = candidate_ports is not None and candidate_ports == ref_ports
        else:
            ports_match = False

        if bus_matches and ports_match:
            same_port.append(candidate)
    return same_port


def forensic_report_port(reference_device, context):
    devices = _devices_on_same_port(reference_device)
    if not devices:
        logger.error(
            "FORENSIC USB [%s]: no enumerated USB device on bus=%s ports=%s",
            context, getattr(reference_device, "bus", None),
            getattr(reference_device, "port_numbers", None)
        )
        return []

    for candidate in devices:
        logger.warning("FORENSIC USB [%s]: same-port identity=%s", context, _usb_identity(candidate, include_strings=True))
    return devices


def forensic_watch_after_chunk(device, filename, chunk_index, offset):
    if not forensic_mode:
        return

    before = _usb_identity(device)
    started = perf_counter_ns()
    try:
        current = usb.core.find(
            idVendor=0x04e8, idProduct=0x1234, backend=USB_BACKEND
        )
    except Exception as e:
        logger.exception(
            "FORENSIC USB [%s chunk=%d offset=0x%X]: enumeration query failed",
            filename, chunk_index, offset
        )
        raise USBStreamInterrupted(f"USB enumeration query failed after {filename} chunk {chunk_index}: {e}") from e

    if current is not None:
        after = _usb_identity(current)
        same_instance = (
            after["bus"] == before["bus"]
            and after["ports"] == before["ports"]
            and after["address"] == before["address"]
        )
        if same_instance:
            logger.debug(
                "FORENSIC USB POST-CHUNK: libusb enumeration still reports the same "
                "identity after %s chunk=%d offset=0x%X check_time=%.6fs identity=%s",
                filename, chunk_index, offset,
                (perf_counter_ns() - started) / 1_000_000_000, after
            )

            # usb.core.find() may still return a just-disconnected device object.
            # Issue a standard, read-only GET_DESCRIPTOR request over EP0 to prove
            # that the physical device still answers on the bus.
            ep0_started = perf_counter_ns()
            try:
                descriptor = bytes(current.ctrl_transfer(
                    0x80,       # device-to-host, standard, device recipient
                    0x06,       # GET_DESCRIPTOR
                    0x0100,    # DEVICE descriptor
                    0,
                    18,
                    timeout=150
                ))
                if len(descriptor) != 18 or descriptor[0] != 18 or descriptor[1] != 1:
                    raise RuntimeError(
                        f"unexpected device descriptor ({len(descriptor)} bytes): {descriptor.hex()}"
                    )
                logger.debug(
                    "FORENSIC USB EP0: device answered GET_DESCRIPTOR after %s "
                    "chunk=%d in %.6fs descriptor=%s",
                    filename, chunk_index,
                    (perf_counter_ns() - ep0_started) / 1_000_000_000,
                    descriptor.hex(" ")
                )
                return
            except Exception as e:
                logger.error(
                    "FORENSIC USB EP0: libusb enumeration still showed the device, "
                    "but GET_DESCRIPTOR failed after %s chunk=%d: %s: %s "
                    "(errno=%s backend_error_code=%s)",
                    filename, chunk_index, type(e).__name__, e,
                    getattr(e, "errno", None),
                    getattr(e, "backend_error_code", None)
                )
        else:
            logger.error(
                "FORENSIC USB POST-CHUNK: identity changed immediately after %s "
                "chunk=%d offset=0x%X before=%s after=%s",
                filename, chunk_index, offset, before, after
            )
    else:
        logger.error(
            "FORENSIC USB POST-CHUNK: 04e8:1234 disappeared immediately after %s "
            "chunk=%d offset=0x%X although Bulk OUT returned success",
            filename, chunk_index, offset
        )

    deadline = monotonic() + 2.0
    same_port_identities = None
    while monotonic() < deadline:
        candidates = _devices_on_same_port(device)
        if candidates:
            same_port_identities = [_usb_identity(candidate, include_strings=True) for candidate in candidates]
            logger.warning(
                "FORENSIC USB POST-CHUNK: device(s) returned on the same port after "
                "%s chunk=%d: %s",
                filename, chunk_index, same_port_identities
            )
            break
        sleep(0.02)

    if same_port_identities is None:
        logger.error(
            "FORENSIC USB POST-CHUNK: nothing enumerated on bus=%s ports=%s "
            "within 2.0s after %s chunk=%d",
            before["bus"], before["ports"], filename, chunk_index
        )

    raise USBStreamInterrupted(
        f"USB disappeared or re-enumerated immediately after successful {filename} "
        f"chunk {chunk_index} at offset 0x{offset:X}. Captured same-port enumeration; "
        "stopped before sending more bytes because the DNW frame cannot be resumed safely."
    )


def forensic_log_frame(frame, filename):
    frame_size = len(frame)
    declared_size = struct.unpack("<I", frame[4:8])[0] if frame_size >= 8 else None
    payload = bytes(frame[8:-2]) if frame_size >= 10 else b""
    payload_hash = hashlib.sha256(payload).hexdigest()
    prefix = payload[:32].hex(" ")
    suffix = payload[-32:].hex(" ") if payload else ""

    logger.info(
        "FORENSIC FRAME [%s]: frame_bytes=%d (0x%X) declared_size=%s header=%s trailer=%s",
        filename, frame_size, frame_size,
        f"{declared_size} (0x{declared_size:X})" if declared_size is not None else "invalid",
        bytes(frame[:8]).hex(" "), bytes(frame[-2:]).hex(" ")
    )
    logger.info(
        "FORENSIC FRAME [%s]: payload_bytes=%d (0x%X) SHA256=%s",
        filename, len(payload), len(payload), payload_hash
    )
    logger.info(
        "FORENSIC FRAME [%s]: payload_prefix[0:32]=%s payload_suffix[-32:]=%s",
        filename, prefix, suffix
    )

    if declared_size != frame_size:
        logger.error(
            "FORENSIC FRAME [%s]: DNW size field mismatch: declared=%s actual=%d",
            filename, declared_size, frame_size
        )
    if frame_size > 0x4E800:
        logger.warning(
            "FORENSIC SIZE HYPOTHESIS [%s]: frame 0x%X exceeds 0x4E800. "
            "That receive limit is documented for the separately reverse-engineered "
            "Exynos 8895 BootROM only; it is NOT established for Exynos 7570.",
            filename, frame_size
        )


def forensic_probe_bulk_in(device, context):
    if not probe_bulk_in:
        return
    started = perf_counter_ns()
    try:
        data = bytes(device.read(0x81, 512, timeout=25))
        logger.warning(
            "FORENSIC BULK IN [%s]: received=%d bytes elapsed=%.6fs hex=%s ascii=%r",
            context, len(data),
            (perf_counter_ns() - started) / 1_000_000_000,
            data.hex(" "), data.decode("ascii", errors="replace")
        )
    except usb.core.USBTimeoutError:
        logger.info("FORENSIC BULK IN [%s]: no data on endpoint 0x81 within 25ms", context)
    except Exception as e:
        logger.warning(
            "FORENSIC BULK IN [%s]: read failed after %.6fs: %s: %s "
            "(errno=%s backend_error_code=%s)",
            context, (perf_counter_ns() - started) / 1_000_000_000,
            type(e).__name__, e,
            getattr(e, "errno", None), getattr(e, "backend_error_code", None)
        )


def write_u32(value):
    return struct.pack('<I', value)

def write_header(data, size):
    data[0:4] = b"\x1BDNW"
    data[4:8] = write_u32(size)

def load_file(file_input):
    try:
        if isinstance(file_input, str):  # If input is a filename
            with open(file_input, 'rb') as file:
                file_data = file.read()
        elif isinstance(file_input, bytes):  # If input is raw bytes
            file_data = file_input
        else:
            raise TypeError("Invalid file input type. Must be filename (str) or raw data (bytes).")

        size = len(file_data) + 10
        block = bytearray(size)
        block[8:8+len(file_data)] = file_data

        return block
    except Exception as e:
        logger.critical(f"Error loading file: {e}")
        return None

def append_checksum(data):
    data[-2:] = struct.pack('<H', 0xFFFF)

def find_device():
    global USB_BACKEND
    USB_BACKEND = None
    device_connection_attempts = 0

    if os.name == "nt":
        USB_BACKEND = usb.backend.libusb1.get_backend(find_library=lambda x: libusb.dll._name)

    while True:
        device = usb.core.find(idVendor=0x04e8, idProduct=0x1234, backend=USB_BACKEND)

        if device is None:
            if device_connection_attempts and device_connection_attempts % 300 == 0:
                print()
                logger.debug("Tip: Keep the EUB button pressed; waiting for 04e8:1234.")

            if device_connection_attempts % 20 == 0:
                print(".", end="", flush=True)

            device_connection_attempts += 1
            sleep(0.05)
        else:
            print()
            return device

def print_crash_info(crash_str):
    logger.critical(f"Crash information:")
    chunks = [crash_str[i:i+8] for i in range(0, len(crash_str), 8)]

    version = chunks[1]
    soc_id = chunks[2]
    chip_id0 = chunks[3]
    chip_id1 = chunks[4]
    rst_stat = chunks[9]
    om_stat = chunks[11]
    key_bank_addr = chunks[13]
    bootdevice_information = chunks[26]
    status_reg0 = chunks[27]
    status_reg1 = chunks[28]
    
    logger.critical(f"Version: {version}")
    logger.critical(f"SoC ID: {soc_id}")
    logger.critical(f"Chip IDs: {chip_id0}, {chip_id1}")
    logger.critical(f"Reset Status: {rst_stat}")
    logger.critical(f"OM Status: {om_stat}")
    logger.critical(f"Key Bank Address: {key_bank_addr}")
    logger.critical(f"Boot Device Info: {bootdevice_information}")
    logger.critical(f"Status Registers: {status_reg0}, {status_reg1}")


def debug_usb_state(device, stage):
    if not debug_usb:
        return

    try:
        ports = getattr(device, "port_numbers", None)
        logger.debug(
            "USB DEBUG [%s]: VID:PID=%04x:%04x bus=%s address=%s ports=%s speed=%s",
            stage,
            device.idVendor,
            device.idProduct,
            getattr(device, "bus", None),
            getattr(device, "address", None),
            ports,
            getattr(device, "speed", None),
        )
        config = device.get_active_configuration()
        for interface in config:
            for endpoint in interface:
                logger.debug(
                    "USB DEBUG [%s]: interface=%s endpoint=0x%02x direction=%s "
                    "attributes=0x%02x max_packet=%s",
                    stage,
                    interface.bInterfaceNumber,
                    endpoint.bEndpointAddress,
                    "IN" if usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT",
                    endpoint.bmAttributes,
                    endpoint.wMaxPacketSize,
                )
    except Exception:
        logger.exception("USB DEBUG [%s]: unable to read current USB device state", stage)


def debug_usb_presence(device, stage):
    if not debug_usb:
        return True

    try:
        current = usb.core.find(
            idVendor=0x04e8,
            idProduct=0x1234,
            backend=USB_BACKEND,
        )
    except Exception:
        logger.exception("USB DEBUG [%s]: enumeration check failed", stage)
        return False

    if current is None:
        logger.error(
            "USB DEBUG [%s]: Exynos 7570 (04e8:1234) is NO LONGER ENUMERATED",
            stage,
        )
        return False

    old_bus = getattr(device, "bus", None)
    old_ports = getattr(device, "port_numbers", None)
    new_bus = getattr(current, "bus", None)
    new_ports = getattr(current, "port_numbers", None)
    new_address = getattr(current, "address", None)

    logger.debug(
        "USB DEBUG [%s]: device still enumerated at bus=%s address=%s ports=%s",
        stage, new_bus, new_address, new_ports
    )

    if old_bus is not None and new_bus != old_bus:
        logger.error("USB DEBUG [%s]: device appeared on a different USB bus", stage)
        return False
    if old_ports is not None and new_ports is not None and new_ports != old_ports:
        logger.error("USB DEBUG [%s]: device appeared on a different USB port path", stage)
        return False
    if new_address != getattr(device, "address", None):
        logger.error(
            "USB DEBUG [%s]: USB address changed from %s to %s; device re-enumerated "
            "and the old PyUSB handle must not be reused",
            stage, getattr(device, "address", None), new_address
        )
        return False

    return True


def _read_usb_string(device, descriptor_index):
    if not descriptor_index:
        return None
    try:
        value = usb.util.get_string(device, descriptor_index)
        return value.strip("\0") if value else None
    except Exception:
        return None


def _same_usb_path(device, bus, ports):
    device_bus = getattr(device, "bus", None)
    device_ports = getattr(device, "port_numbers", None)

    # A missing locator is not a match. The fast Exynos 7570 transition path
    # relies on the exact physical bus/port remaining the same.
    if bus is not None and device_bus != bus:
        return False
    if ports is not None and device_ports != ports:
        return False
    return True


def _dispose_usb_handle(device, stage, fast=False):
    if not fast:
        try:
            usb.util.release_interface(device, 0)
        except Exception as e:
            logger.debug("USB transition [%s]: old interface release was not possible: %s", stage, e)

    try:
        usb.util.dispose_resources(device)
    except Exception as e:
        logger.debug("USB transition [%s]: old handle cleanup warning: %s", stage, e)


def _claim_usb_handle(device, stage, fast=False):
    if os.name != "nt":
        try:
            if device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
        except Exception as e:
            logger.debug("USB transition [%s]: kernel-driver detach check: %s", stage, e)

    # The kernel has already configured this freshly enumerated EUB device.
    # Avoid another GET_DESCRIPTOR walk on the short BL2 -> U-Boot reconnect.
    if not fast:
        device.get_active_configuration()
    usb.util.claim_interface(device, 0)
    if not fast:
        debug_usb_state(device, stage)
    else:
        logger.warning("USB fast path [%s]: interface 0 claimed without descriptor probing", stage)
    return device


def wait_for_stage_reenumeration(device, stage, force=False):
    """
    Watch an explicitly configured stage boundary for a USB reset/re-enumeration.

    Some older Exynos EUB implementations re-enumerate between boot stages.
    When that happens, the previous PyUSB handle is stale; find and claim the
    same device again by physical USB path and serial number. If no disconnect
    happens within the configured window, keep using the original handle.
    """
    transitions = EXYNOS_DATA.get("stage_transitions", {})
    if force:
        policy = EXYNOS_DATA.get("usb_recovery", {})
        if not policy.get("enabled", False):
            raise RuntimeError("Automatic USB stream recovery is not enabled for this SoC profile.")
    else:
        policy = transitions.get(stage)
        if not policy:
            return device

    timeout = max(0.5, float(policy.get("timeout_ms", 10000)) / 1000.0)
    poll_interval = max(0.01, float(policy.get("poll_ms", 25)) / 1000.0)

    fast_reconnect = bool(policy.get("fast_reconnect", False)) and not force
    old_bus = getattr(device, "bus", None)
    old_ports = getattr(device, "port_numbers", None)
    old_address = getattr(device, "address", None)

    # The old handle may already refer to a disconnected EUB device. Reading
    # its string descriptors can consume the entire short reconnect window.
    # In fast mode, the same VID:PID on the same physical port is our identity.
    if fast_reconnect:
        old_product = None
        old_serial = None
        logger.warning(
            "USB fast transition after %s: using cached bus/port/address only; "
            "skipping string reads from the old handle",
            stage
        )
    else:
        old_product = _read_usb_string(device, getattr(device, "iProduct", 0))
        old_serial = _read_usb_string(device, getattr(device, "iSerialNumber", 0))

    if force:
        expected_cycle = float(policy.get("expected_cycle_ms", 0)) / 1000.0
        logger.warning(
            "USB recovery after interrupted %s: waiting up to %.1fs for the same EUB "
            "device to return on bus=%s ports=%s (observed cycle approximately %.1fs)",
            stage, timeout, old_bus, old_ports, expected_cycle
        )
    else:
        logger.warning(
            "USB transition after %s: watching for re-enumeration for up to %.1fs "
            "(bus=%s ports=%s address=%s serial=%s)",
            stage, timeout, old_bus, old_ports, old_address, old_serial
        )

    wait_started = monotonic()
    deadline = wait_started + timeout
    saw_disconnect = False
    old_handle_disposed = False

    while monotonic() < deadline:
        try:
            current = usb.core.find(
                idVendor=0x04e8,
                idProduct=0x1234,
                backend=USB_BACKEND,
            )
        except Exception:
            logger.exception("USB transition after %s: enumeration check failed", stage)
            sleep(poll_interval)
            continue

        if current is None:
            if not saw_disconnect:
                saw_disconnect = True
                logger.warning(
                    "USB transition after %s: 04e8:1234 disappeared; waiting for the same device to return",
                    stage
                )
                _dispose_usb_handle(device, stage, fast=fast_reconnect)
                old_handle_disposed = True
            sleep(poll_interval)
            continue

        if not _same_usb_path(current, old_bus, old_ports):
            sleep(poll_interval)
            continue

        current_address = getattr(current, "address", None)
        is_new_enumeration = saw_disconnect or current_address != old_address
        if not is_new_enumeration:
            sleep(poll_interval)
            continue

        if fast_reconnect:
            logger.warning(
                "USB fast transition after %s: found same-path Exynos at address=%s "
                "after %.3fs; skipping product/serial string requests",
                stage, current_address, monotonic() - wait_started
            )
            current_product = None
            current_serial = None
        else:
            current_product = _read_usb_string(current, getattr(current, "iProduct", 0))
            current_serial = _read_usb_string(current, getattr(current, "iSerialNumber", 0))

            if old_product and current_product != old_product:
                logger.debug(
                    "USB transition after %s: ignoring product mismatch %r (expected %r)",
                    stage, current_product, old_product
                )
                sleep(poll_interval)
                continue

            if old_serial and current_serial != old_serial:
                logger.debug(
                    "USB transition after %s: ignoring serial mismatch %r (expected %r)",
                    stage, current_serial, old_serial
                )
                sleep(poll_interval)
                continue

        if not old_handle_disposed:
            _dispose_usb_handle(device, stage, fast=fast_reconnect)
            old_handle_disposed = True

        claim_started = monotonic()
        try:
            _claim_usb_handle(current, f"re-enumerated after {stage}", fast=fast_reconnect)
        except Exception as e:
            usb.util.dispose_resources(current)
            raise RuntimeError(
                f"Exynos re-enumerated after {stage}, but interface 0 could not be reclaimed: {e}"
            ) from e

        logger.warning(
            "USB transition after %s: reclaimed Exynos at bus=%s ports=%s address=%s "
            "serial=%s claim_time=%.3fs total_wait=%.3fs",
            stage,
            getattr(current, "bus", None),
            getattr(current, "port_numbers", None),
            getattr(current, "address", None),
            current_serial if current_serial is not None else "<not-read-fast-path>",
            monotonic() - claim_started,
            monotonic() - wait_started
        )
        return current

    if saw_disconnect:
        raise RuntimeError(
            f"Exynos disappeared after {stage} but did not return as the same EUB device "
            f"within {timeout:.1f}s. Refusing to write the next stage through a stale USB handle."
        )

    if force:
        raise RuntimeError(
            f"USB stream for {stage} was interrupted, but no fresh same-device "
            f"enumeration appeared on the original USB path within {timeout:.1f}s."
        )

    logger.warning(
        "USB transition after %s: no re-enumeration was observed within %.1fs; "
        "continuing with the existing connection",
        stage, timeout
    )
    return device


def send_part_to_device(device, file, filename):
    file_size = len(file)
    single_transfer_stages = EXYNOS_DATA.get("single_transfer_stages", [])
    single_transfer = filename in single_transfer_stages
    use_chunked_transfer = badcon and not single_transfer

    logger.warning(f"=> Uploading {file_size} bytes")
    if single_transfer and badcon:
        logger.warning(
            "USB fast transfer [%s]: overriding --badcon and submitting one Bulk OUT "
            "request; the USB host controller still packetizes it at the endpoint's "
            "512-byte maximum packet size",
            filename
        )

    write_header(file, file_size)
    append_checksum(file)

    if forensic_mode:
        forensic_log_frame(file, filename)
        logger.info(
            "FORENSIC STAGE [%s]: transmission starts at relative t+%.6fs",
            filename, forensic_elapsed()
        )

    if use_chunked_transfer:
        # Keep the EUB frame intact, but split its USB Bulk OUT stream into
        # small transfers. 512 bytes matches a documented Exynos BootROM
        # implementation; Exynos 7570 still needs hardware validation.
        chunk_size = 512
        logger.warning(f"=> Bad connection mode: sending in {chunk_size}-byte USB chunks")
        ret = 0
        next_progress = 128 * 1024

        while ret < file_size:
            chunk = file[ret:ret + chunk_size]

            chunk_index = (ret // chunk_size) + 1
            transfer_started = monotonic()
            transfer_started_ns = perf_counter_ns()

            if debug_usb:
                logger.debug(
                    "USB DEBUG: OUT BEGIN file=%s chunk=%d offset=0x%X size=%d "
                    "timeout=50000ms relative_t=%.6fs",
                    filename, chunk_index, ret, len(chunk), forensic_elapsed()
                )

            try:
                written = device.write(2, chunk, timeout=50000)
            except Exception as e:
                elapsed = monotonic() - transfer_started
                if debug_usb:
                    logger.exception(
                        "USB DEBUG: OUT FAILED file=%s chunk=%d offset=0x%X "
                        "chunk_size=%d total=%d elapsed=%.6fs elapsed_ns=%d errno=%s "
                        "backend_error_code=%s args=%r",
                        filename,
                        chunk_index,
                        ret,
                        len(chunk),
                        file_size,
                        elapsed,
                        perf_counter_ns() - transfer_started_ns,
                        getattr(e, "errno", None),
                        getattr(e, "backend_error_code", None),
                        getattr(e, "args", None),
                    )
                    debug_usb_presence(device, f"write failure in {filename}")
                    debug_usb_state(device, f"write failure in {filename}")
                    if forensic_mode:
                        forensic_report_port(
                            device, f"write failure in {filename} chunk={chunk_index}"
                        )
                raise USBStreamInterrupted(
                    f"USB write failed for {filename} at offset 0x{ret:X} "
                    f"({ret}/{file_size} bytes, chunk {chunk_index}, "
                    f"{elapsed:.3f}s). The stream cannot be resumed safely: {e}"
                ) from e

            elapsed = monotonic() - transfer_started
            elapsed_ns = perf_counter_ns() - transfer_started_ns
            if debug_usb:
                logger.debug(
                    "USB DEBUG: OUT OK file=%s chunk=%d offset=0x%X "
                    "size=%d elapsed=%.6fs elapsed_ns=%d relative_t=%.6fs",
                    filename, chunk_index, ret, written, elapsed, elapsed_ns,
                    forensic_elapsed()
                )

            if written != len(chunk):
                raise USBStreamInterrupted(
                    f"Short USB write for {filename} at offset 0x{ret:X}: "
                    f"{written} of {len(chunk)} bytes"
                )

            ret += written

            if forensic_mode and probe_bulk_in and chunk_index == 1:
                # Read a possible BootROM response before the EP0 liveness test
                # can abort on a simultaneous device reset.
                forensic_probe_bulk_in(device, f"after first chunk {filename}")

            if forensic_mode and (
                chunk_index == 1 or (chunk_index % 64 == 0 and ret < file_size)
            ):
                forensic_watch_after_chunk(
                    device, filename, chunk_index, ret - written
                )

            if ret >= next_progress or ret == file_size:
                logger.info(f"=> {ret}/{file_size} bytes transferred")
                while next_progress <= ret:
                    next_progress += 128 * 1024
    else:
        transfer_started = monotonic()
        if debug_usb:
            logger.debug(
                "USB DEBUG: OUT BEGIN file=%s size=%d timeout=50000ms",
                filename, file_size
            )
        try:
            ret = device.write(2, file, timeout=50000)
        except Exception as e:
            elapsed = monotonic() - transfer_started
            if debug_usb:
                logger.exception(
                    "USB DEBUG: OUT FAILED file=%s bytes=%s elapsed=%.3fs "
                    "errno=%s backend_error_code=%s args=%r",
                    filename,
                    file_size,
                    elapsed,
                    getattr(e, "errno", None),
                    getattr(e, "backend_error_code", None),
                    getattr(e, "args", None),
                )
                debug_usb_presence(device, f"write failure in {filename}")
                debug_usb_state(device, f"write failure in {filename}")
            raise USBStreamInterrupted(
                f"USB write failed for {filename} ({file_size} bytes): {e}"
            ) from e
        if debug_usb:
            logger.debug(
                "USB DEBUG: OUT OK file=%s bytes=%d elapsed=%.3fs",
                filename, ret, monotonic() - transfer_started
            )

    if forensic_mode and probe_bulk_in:
        forensic_probe_bulk_in(device, f"after complete frame {filename}")

    if EXYNOS_DATA["response_support"] == True and verbose == True:
        while True:
            try:
                data = device.read(0x81, 512, timeout=50)

                byte_str = ''.join(chr(n) for n in data[0:])
                response = byte_str.split('\x00',1)[0]

                if len(response) != 0:
                    if not "UUUUUUUU" in response:
                        logger.debug(f"=> Device Response: {response}")
                    else:
                        logger.critical("BootROM Crash Detected")
                        tmp = response

                        try:
                            data = device.read(0x81, 512, timeout=50)

                            byte_str = ''.join(chr(n) for n in data[0:])
                            response = byte_str.split('\x00',1)[0]

                            if len(response) != 0:
                                tmp = f"{tmp}{response}"
                        except:
                            break

                        tmp = tmp[2:]
                        print_crash_info(tmp)
            except:
                break

    if ret == file_size:
        logger.info(f"=> {ret} bytes written.")
    else:
        logger.critical(f"=> {ret} bytes written.")
        raise USBStreamInterrupted(
            f"Short USB frame write for {filename}: {ret} of {file_size} bytes."
        )

    print()


def send_stage_with_retries(device, frame, filename):
    """Send one complete DNW frame, with only profile-authorized recovery."""
    fast_transfer = filename in EXYNOS_DATA.get("single_transfer_stages", [])
    recovery = EXYNOS_DATA.get("usb_recovery", {})
    recovery_enabled = recovery.get("enabled", False)
    max_retries = max(0, int(recovery.get("max_stage_retries", 0))) if recovery_enabled else 0
    attempt = 0

    while True:
        try:
            if not fast_transfer:
                if debug_usb and not debug_usb_presence(device, f"before {filename}"):
                    raise USBStreamInterrupted(
                        f"USB device disappeared or re-enumerated before {filename}."
                    )
                debug_usb_state(device, f"before {filename}")
            else:
                logger.warning(
                    "USB fast transfer [%s]: skipping pre-transfer enumeration/configuration probes",
                    filename
                )
            send_part_to_device(device, frame, filename)
            return device
        except USBStreamInterrupted as e:
            if not recovery_enabled or attempt >= max_retries:
                raise

            attempt += 1
            logger.warning(
                "DNW frame %s was interrupted (%s). Retry %d/%d: the partial frame "
                "will NOT be resumed; waiting for a fresh EUB enumeration and then "
                "resending the entire frame from offset 0.",
                filename, e, attempt, max_retries
            )
            device = wait_for_stage_reenumeration(device, filename, force=True)
            logger.warning(
                "EUB returned after interrupted %s; retrying the complete DNW frame.",
                filename
            )


def extract_bl_tar(path):
    output_dir = "./output/"

    os.makedirs (output_dir, exist_ok=True)

    with tarfile.open(path, 'r') as tar:
        members = tar.getmembers()

        try:
            tar.extractall(path=output_dir, members=members, filter="data")
        except:
            logger.critical("Failure in extracting BL tar! Bailing!")
            sys.exit(-1)

        for resultant_bin in members:
            resultant_bin_path = os.path.join(output_dir, resultant_bin.name)

            with open(resultant_bin_path, 'rb') as input_lz4:
                try:
                    filename_no_lz4 = os.path.splitext(resultant_bin_path)[0]

                    with open(filename_no_lz4, 'wb') as output_bin:
                        output_bin.write(lz4.frame.decompress(input_lz4.read()))

                        output_bin.close()

                    logger.warning(f"Extracted: {resultant_bin.name}")
                except:
                    logger.critical("Failure in extracting LZ4 archives! Bailing!")
                    sys.exit(-1)

    print()

def delete_file(filename):
    try:
        os.remove(filename)
        logger.warning(f"Deleted: {filename}")
    except:
        pass

def legacy_soc_detection():
    with open("output/sboot.bin", "rb") as sboot:
        sboot_data = sboot.read()
        soc_pattern = re.compile(b'EXYNOS[0-9]+')
        matches = soc_pattern.findall(sboot_data)

        if not matches:
            logger.critical("Failed to detect SoC from sboot.bin!")
            sys.exit(-1)

        return f"{matches[0].decode('utf-8').title()}"

def display_and_verify_device_info(device):
    global EXYNOS_DATA

    device_config = device.get_active_configuration()

    soc = usb.util.get_string(device, device.iProduct).strip("\0")
    usb_serial_num = usb.util.get_string(device, device.iSerialNumber)
    usb_booting_version = usb.util.get_string(device, device_config[(0, 0)].iInterface)

    print()
    logger.debug(f"==================== Device Information ====================")
    logger.info(f"SoC: {soc}".center(60))

    # Extremely old SoCs don’t host this information
    if usb_serial_num != None:
        logger.info(f"SoC ID: {usb_serial_num[0:15]}".center(60))
        logger.info(f"Chip ID: {usb_serial_num[15:31]}".center(60))

    if usb_booting_version != None:
        logger.info(f"USB Booting Version: {usb_booting_version[12:16]}".center(60))

    print()

    if soc == "SEC S5PC210 Test B/D":
        logger.error("Legacy SoC detected, attempting to detect SoC via S-Boot image.")
        soc = legacy_soc_detection()
        logger.error(f"Detected SoC: {soc}")

    try:
        with open (f"ExynosData/{soc}.json", "r") as f:
            EXYNOS_DATA = json.load (f)
    except:
        logger.critical("This SoC is not Supported!")
        sys.exit(-1)

    if EXYNOS_DATA.get("experimental", False):
        if not allow_experimental:
            logger.critical(
                "This SoC profile is experimental and is blocked by default. "
                "Review the profile, then rerun with --allow-experimental to proceed."
            )
            sys.exit(-1)
        logger.warning("Experimental SoC profile enabled; S-Boot hash will be checked before transfer.")

def validate_sboot_image(path):
    try:
        with open(path, "rb") as sboot:
            sboot_data = sboot.read()
    except OSError as e:
        logger.critical(f"Could not read S-Boot image: {e}")
        sys.exit(-1)

    if EXYNOS_DATA.get("experimental", False):
        expected_hash = EXYNOS_DATA.get("sboot_sha256")
        if not expected_hash:
            logger.critical("Experimental profile has no pinned S-Boot SHA-256; refusing to continue.")
            sys.exit(-1)

        actual_hash = hashlib.sha256(sboot_data).hexdigest()
        if actual_hash.lower() != expected_hash.lower():
            logger.critical("S-Boot image does not match the image used to derive this experimental profile.")
            logger.critical(f"Expected SHA-256: {expected_hash}")
            logger.critical(f"Actual SHA-256:   {actual_hash}")
            sys.exit(-1)

        logger.info(f"Experimental S-Boot SHA-256 verified: {actual_hash}")

    for image_name, split in EXYNOS_DATA["bootloader_splits"].items():
        start = split.get("start")
        length = split.get("length")
        if not isinstance(start, int) or not isinstance(length, int):
            logger.critical(f"Invalid split metadata for {image_name}: start/length must be integers.")
            sys.exit(-1)
        if start < 0 or length <= 0 or start + length > len(sboot_data):
            logger.critical(
                f"Split {image_name} is outside S-Boot bounds: "
                f"start=0x{start:X}, length=0x{length:X}, image=0x{len(sboot_data):X}"
            )
            sys.exit(-1)

    logger.info(f"Validated {len(EXYNOS_DATA['bootloader_splits'])} bootloader split(s) against S-Boot size 0x{len(sboot_data):X}.")


def main():
    global verbose, allow_experimental, badcon, debug_usb
    global forensic_mode, probe_bulk_in

    coloredlogs.install(
        level="DEBUG",
        fmt="%(asctime)s.%(msecs)03d %(message)s",
        level_styles={
            'debug': {'color': 'magenta'},
            'info': {'color': 'green'},
            'warning': {'color': 'white', 'bold': True},
            'error': {'color': 'yellow', 'bold': True},
            'critical': {'color': 'red', 'bold': True},
        },
        field_styles={
            'asctime': {'color': 'blue'},
            'levelname': {'bold': True},
        }
    )

    logger.debug(r"""
  _    _ _    _ ____  ____  _      ______
 | |  | | |  | |  _ \|  _ \| |    |  ____|
 | |__| | |  | | |_) | |_) | |    | |__
 |  __  | |  | |  _ <|  _ <| |    |  __|
 | |  | | |__| | |_) | |_) | |____| |____
 |_|  |_|\____/|____/|____/|______|______|
    """)

    print("USB Recovery Tool")
    print("Version 1.0 (c) 2025 Umer Uddin <umer.uddin@mentallysanemainliners.org>")
    print()
    logger.error("Notice: This program and it's source code is licensed under GPL 2.0.")
    logger.error("Notice: If you have paid for this, you have been scammed!")
    logger.error("Please issue a refund and get the official program from")
    logger.info("https://github.com/halal-beef/hubble")
    print()

    parser = argparse.ArgumentParser(description="USB Recovery Tool for Exynos devices.")
    parser.add_argument('-b', '--bl-tar', type=str, help="Path to the .tar or .tar.md5 file", required=True)
    parser.add_argument('-v', '--verbose', action="store_true", help="Enables more Logs during Operation")
    parser.add_argument('--allow-experimental', action="store_true", help="Allow an experimental SoC profile after reviewing its limitations")
    parser.add_argument('--badcon', action="store_true", help="Send EUB data in 512-byte USB Bulk OUT chunks for unstable connections")
    parser.add_argument('--debug', '--debug-usb', dest="debug_usb", action="store_true", help="Log USB descriptors, device presence, and timing/errors for every Bulk OUT transfer")
    parser.add_argument('--forensic', action="store_true", help="Enable detailed frame/USB tracing, millisecond log files, and automatic Linux dmesg capture")
    parser.add_argument('--probe-in', action="store_true", help="With --forensic, read Bulk IN 0x81 for 25ms after the first chunk and each complete frame; may consume pending device data")

    args = parser.parse_args()
    allow_experimental = args.allow_experimental
    badcon = args.badcon
    debug_usb = args.debug_usb or args.forensic or args.probe_in
    forensic_mode = args.forensic or args.probe_in
    probe_bulk_in = args.probe_in

    if forensic_mode:
        start_forensic_capture()

    if args.bl_tar:
        if os.path.isfile(args.bl_tar):
            logger.warning(f"Using file: {args.bl_tar}")
        else:
            logger.critical(f"Error: The file {args.bl_tar} does not exist or is not a valid file.")
            sys.exit(-1)

    if args.verbose:
        verbose = True

    if badcon:
        logger.warning(
            "Bad connection mode enabled: 512-byte USB Bulk OUT submissions for normal stages. "
            "The Exynos 7570 profile overrides this for U-Boot and uses one bulk request."
        )

    if debug_usb:
        logger.warning("USB debug mode enabled: descriptors, enumeration, and Bulk OUT transfer timing will be logged.")

    if forensic_mode:
        logger.warning(
            "FORENSIC mode enabled: DNW frame hashes, precise timing, post-chunk USB checks, "
            "same-port device scans, and automatic kernel log capture are enabled."
        )
        if probe_bulk_in:
            logger.warning(
                "FORENSIC Bulk IN probing is enabled; reads from 0x81 can consume pending "
                "device messages. Compare with a run without --probe-in."
            )

    logger.warning("Extracting files...")
    extract_bl_tar(args.bl_tar)

    logger.warning("Waiting for device")
    device = find_device()
    logger.warning("Found device.")
    debug_usb_state(device, "device found")
    debug_usb_presence(device, "device found")

    display_and_verify_device_info(device)
    validate_sboot_image("output/sboot.bin")

    logger.warning(f"Starting USB booting...")
    print()

    if os.name != "nt":
        if device.is_kernel_driver_active(0):
            device.detach_kernel_driver(0)

    usb.util.claim_interface(device, 0)
    debug_usb_state(device, "interface 0 claimed")

    with open("output/sboot.bin", "rb") as sboot:
        for img_name, split_params in EXYNOS_DATA["bootloader_splits"].items():
            try:
                logger.debug(f"Sending file part {img_name} (0x{split_params["start"]:X} - 0x{split_params["start"] + split_params["length"]:X})...")

                sboot.seek(split_params["start"])
                sboot_section = load_file(sboot.read(split_params["length"]))

                if sboot_section is None:
                    logger.critical(f"Failed to load {img_name}")
                    sys.exit(-1)

                device = send_stage_with_retries(device, sboot_section, img_name)
                stage_delay_ms = int(EXYNOS_DATA.get("inter_stage_delay_ms", 0))
                if stage_delay_ms > 0:
                    logger.debug("Waiting %d ms before the next boot stage...", stage_delay_ms)
                    sleep(stage_delay_ms / 1000.0)
                device = wait_for_stage_reenumeration(device, img_name)
                if img_name not in EXYNOS_DATA.get("fast_transition_stages", []):
                    debug_usb_presence(device, f"after {img_name}")
                    debug_usb_state(device, f"after {img_name}")
                else:
                    logger.warning(
                        "USB fast transition after %s: skipping post-reconnect diagnostics "
                        "so the next stage can start immediately",
                        img_name
                    )
            except Exception as e:
                logger.critical(f"Error when trying to process sboot.bin! ({e})")
                sys.exit(-1)

        sboot.close()

    for file_to_send in EXYNOS_DATA["files_to_send"]:
        logger.debug(f"Uploading file {file_to_send}...")

        file_data = load_file(f"output/{file_to_send}")

        if file_data is None:
            logger.critical(f"Failed to load {file_to_send}")
            sys.exit(-1)

        try:
            send_part_to_device(device, file_data, file_to_send)
        except:
            pass

    usb.util.release_interface(device, 0)
    usb.util.dispose_resources(device)

    logger.warning("Cleaning up...")
    print()

    shutil.rmtree ("./output")

    print()
    logger.error("You should be in download mode now, please reflash the stock firmware as the bootloader will still be wiped.")

if __name__ == "__main__":
    main()
