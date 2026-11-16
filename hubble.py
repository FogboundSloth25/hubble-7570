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
from time import sleep, monotonic
import re

EXYNOS_DATA = None

soc     = ""
logger  = logging.getLogger(__name__)
verbose = False
allow_experimental = False
badcon = False
debug_usb = False
USB_BACKEND = None

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

    if bus is not None and device_bus is not None and device_bus != bus:
        return False
    if ports is not None and device_ports is not None and device_ports != ports:
        return False
    return True


def _dispose_usb_handle(device, stage):
    try:
        usb.util.release_interface(device, 0)
    except Exception as e:
        logger.debug("USB transition [%s]: old interface release was not possible: %s", stage, e)

    try:
        usb.util.dispose_resources(device)
    except Exception as e:
        logger.debug("USB transition [%s]: old handle cleanup warning: %s", stage, e)


def _claim_usb_handle(device, stage):
    if os.name != "nt":
        try:
            if device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
        except Exception as e:
            logger.debug("USB transition [%s]: kernel-driver detach check: %s", stage, e)

    usb.util.claim_interface(device, 0)
    debug_usb_state(device, stage)
    return device


def wait_for_stage_reenumeration(device, stage):
    """
    Watch an explicitly configured stage boundary for a USB reset/re-enumeration.

    Some older Exynos EUB implementations re-enumerate between boot stages.
    When that happens, the previous PyUSB handle is stale; find and claim the
    same device again by physical USB path and serial number. If no disconnect
    happens within the configured window, keep using the original handle.
    """
    transitions = EXYNOS_DATA.get("stage_transitions", {})
    policy = transitions.get(stage)
    if not policy:
        return device

    timeout = max(0.5, float(policy.get("timeout_ms", 10000)) / 1000.0)
    poll_interval = max(0.01, float(policy.get("poll_ms", 25)) / 1000.0)

    old_bus = getattr(device, "bus", None)
    old_ports = getattr(device, "port_numbers", None)
    old_address = getattr(device, "address", None)
    old_product = _read_usb_string(device, getattr(device, "iProduct", 0))
    old_serial = _read_usb_string(device, getattr(device, "iSerialNumber", 0))

    logger.warning(
        "USB transition after %s: watching for re-enumeration for up to %.1fs "
        "(bus=%s ports=%s address=%s serial=%s)",
        stage, timeout, old_bus, old_ports, old_address, old_serial
    )

    deadline = monotonic() + timeout
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
                _dispose_usb_handle(device, stage)
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
            _dispose_usb_handle(device, stage)
            old_handle_disposed = True

        try:
            _claim_usb_handle(current, f"re-enumerated after {stage}")
        except Exception as e:
            usb.util.dispose_resources(current)
            raise RuntimeError(
                f"Exynos re-enumerated after {stage}, but interface 0 could not be reclaimed: {e}"
            ) from e

        logger.warning(
            "USB transition after %s: reclaimed Exynos at bus=%s ports=%s address=%s serial=%s",
            stage,
            getattr(current, "bus", None),
            getattr(current, "port_numbers", None),
            getattr(current, "address", None),
            current_serial
        )
        return current

    if saw_disconnect:
        raise RuntimeError(
            f"Exynos disappeared after {stage} but did not return as the same EUB device "
            f"within {timeout:.1f}s. Refusing to write the next stage through a stale USB handle."
        )

    logger.warning(
        "USB transition after %s: no re-enumeration was observed within %.1fs; "
        "continuing with the existing connection",
        stage, timeout
    )
    return device


def send_part_to_device(device, file, filename):
    file_size = len(file)

    logger.warning(f"=> Uploading {file_size} bytes")

    write_header(file, file_size)
    append_checksum(file)

    if badcon:
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

            if debug_usb:
                logger.debug(
                    "USB DEBUG: OUT BEGIN file=%s chunk=%d offset=0x%X size=%d timeout=50000ms",
                    filename, chunk_index, ret, len(chunk)
                )

            try:
                written = device.write(2, chunk, timeout=50000)
            except Exception as e:
                elapsed = monotonic() - transfer_started
                if debug_usb:
                    logger.exception(
                        "USB DEBUG: OUT FAILED file=%s chunk=%d offset=0x%X "
                        "chunk_size=%d total=%d elapsed=%.3fs errno=%s "
                        "backend_error_code=%s args=%r",
                        filename,
                        chunk_index,
                        ret,
                        len(chunk),
                        file_size,
                        elapsed,
                        getattr(e, "errno", None),
                        getattr(e, "backend_error_code", None),
                        getattr(e, "args", None),
                    )
                    debug_usb_presence(device, f"write failure in {filename}")
                    debug_usb_state(device, f"write failure in {filename}")
                raise RuntimeError(
                    f"USB write failed for {filename} at offset 0x{ret:X} "
                    f"({ret}/{file_size} bytes, chunk {chunk_index}, "
                    f"{elapsed:.3f}s). The stream cannot be resumed safely: {e}"
                ) from e

            elapsed = monotonic() - transfer_started
            if debug_usb:
                logger.debug(
                    "USB DEBUG: OUT OK file=%s chunk=%d offset=0x%X "
                    "size=%d elapsed=%.3fs",
                    filename, chunk_index, ret, written, elapsed
                )

            if written != len(chunk):
                raise RuntimeError(
                    f"Short USB write for {filename} at offset 0x{ret:X}: "
                    f"{written} of {len(chunk)} bytes"
                )

            ret += written

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
            raise
        if debug_usb:
            logger.debug(
                "USB DEBUG: OUT OK file=%s bytes=%d elapsed=%.3fs",
                filename, ret, monotonic() - transfer_started
            )

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
        logger.critical(f"Failed to write {file_size} bytes")
        sys.exit(-1)

    print()

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

    coloredlogs.install(
        level="DEBUG",
        fmt="%(asctime)s %(message)s",
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

    args = parser.parse_args()
    allow_experimental = args.allow_experimental
    badcon = args.badcon
    debug_usb = args.debug_usb

    if args.bl_tar:
        if os.path.isfile(args.bl_tar):
            logger.warning(f"Using file: {args.bl_tar}")
        else:
            logger.critical(f"Error: The file {args.bl_tar} does not exist or is not a valid file.")
            sys.exit(-1)

    if args.verbose:
        verbose = True

    if badcon:
        logger.warning("Bad connection mode enabled: 512-byte USB Bulk OUT chunks; automatic retries are disabled.")

    if debug_usb:
        logger.warning("USB debug mode enabled: descriptors, enumeration, and Bulk OUT transfer timing will be logged.")

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
                if debug_usb and not debug_usb_presence(device, f"before {img_name}"):
                    raise RuntimeError(
                        f"USB device disappeared or re-enumerated before {img_name}; "
                        "check the previous EUB stage and kernel USB log."
                    )
                debug_usb_state(device, f"before {img_name}")
                logger.debug(f"Sending file part {img_name} (0x{split_params["start"]:X} - 0x{split_params["start"] + split_params["length"]:X})...")

                sboot.seek(split_params["start"])
                sboot_section = load_file(sboot.read(split_params["length"]))

                if sboot_section is None:
                    logger.critical(f"Failed to load {img_name}")
                    sys.exit(-1)

                send_part_to_device(device, sboot_section, img_name)
                device = wait_for_stage_reenumeration(device, img_name)
                debug_usb_presence(device, f"after {img_name}")
                debug_usb_state(device, f"after {img_name}")
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
