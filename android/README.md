# Hubble 7570 Android OTG

The Android source project is stored in [hubble-android-source.zip](hubble-android-source.zip).

## Build APK

The workflow **Build Hubble 7570 Android APK** starts automatically when the source archive or workflow changes on `main`. It can also be started manually from the repository's **Actions** tab using **Run workflow**.

After a successful run, download the artifact named **Hubble7570-Android-debug-APK** from the workflow run summary. It contains:

- `app-debug.apk` — debug-signed APK for sideloading
- `app-debug.apk.sha256` — SHA-256 checksum

The workflow builds the Android ARM64 USBFS helper from `usbhost/main.go` before Gradle packages the APK. No prebuilt executable is required in Git.

## Target and limitations

This is an experimental root/USB-OTG host app for Samsung Galaxy J5 Prime SM-G570F / Exynos 7570 EUB. It only accepts the pinned S-Boot image SHA-256 documented in the Android project README.

The FWBL1/BL31 boundaries have stronger image-structure evidence than the BL2/U-Boot boundaries. The latter remain heuristic and have not been confirmed by a successful recovery on an SM-G570F. A successful USB transfer does not prove that the boot ROM accepted or executed the stages.

The output is a debug build, not a Play Store release.
