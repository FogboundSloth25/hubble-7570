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
from time import sleep
import re

EXYNOS_DATA = None

soc     = ""
logger  = logging.getLogger(__name__)
verbose = False
allow_experimental = False
badcon = False

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
    usb_backend = None
    device_connection_attempts = 0

    if os.name == "nt":
        usb_backend = usb.backend.libusb1.get_backend(find_library=lambda x: libusb.dll._name)

    while True:
        device = usb.core.find(idVendor=0x04e8, idProduct=0x1234, backend=usb_backend)

        if device is None:
            if device_connection_attempts == 15:
                device_connection_attempts = 0

                print()
                logger.debug(f"Tip: Plug in your device with the power button pressed.")

            print(".", end="", flush=True)
            device_connection_attempts += 1
            sleep(1)
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

            try:
                written = device.write(2, chunk, timeout=50000)
            except Exception as e:
                raise RuntimeError(
                    f"USB write failed for {filename} at offset 0x{ret:X} "
                    f"({ret}/{file_size} bytes). The stream cannot be resumed safely: {e}"
                ) from e

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
        ret = device.write(2, file, timeout=50000)

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
    global verbose, allow_experimental, badcon

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

    args = parser.parse_args()
    allow_experimental = args.allow_experimental
    badcon = args.badcon

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

    logger.warning("Extracting files...")
    extract_bl_tar(args.bl_tar)

    logger.warning("Waiting for device")
    device = find_device()
    logger.warning("Found device.")

    display_and_verify_device_info(device)
    validate_sboot_image("output/sboot.bin")

    logger.warning(f"Starting USB booting...")
    print()

    if os.name != "nt":
        if device.is_kernel_driver_active(0):
            device.detach_kernel_driver(0)

    usb.util.claim_interface(device, 0)

    with open("output/sboot.bin", "rb") as sboot:
        for img_name, split_params in EXYNOS_DATA["bootloader_splits"].items():
            try:
                logger.debug(f"Sending file part {img_name} (0x{split_params["start"]:X} - 0x{split_params["start"] + split_params["length"]:X})...")

                sboot.seek(split_params["start"])
                sboot_section = load_file(sboot.read(split_params["length"]))

                if sboot_section is None:
                    logger.critical(f"Failed to load {img_name}")
                    sys.exit(-1)

                send_part_to_device(device, sboot_section, img_name)
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
