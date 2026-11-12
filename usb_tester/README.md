# USB Tester — USB Flash Drive Qualification

Python application for **Ubuntu/Linux** designed to automate USB flash drive qualification.

The tester performs:

- USB read/write performance measurements
- SHA-256 data integrity verification
- VBUS voltage/current monitoring with an Acroname USBExt3c
- USB 2.0 High-Speed fallback verification
- Automatic DUT reformatting
- Test-link mating-cycle tracking
- Final **PASS / FAIL** qualification
- CSV, DOCX and PDF report generation

The GUI is implemented with **PyQt5**.

> **WARNING — DESTRUCTIVE TEST**
>
> The application writes directly to the DUT block device and reformats it at the end of the sequence.
> **All data stored on the tested flash drive will be lost.**

---

## Test sequence

1. The operator enters the **OF number** and selects the **SAP / test link**.
2. The application waits for a DUT on the dedicated USB test port.
3. The DUT identity is recorded using Linux `DISKSEQ`.
4. The selected test-link mating counter is incremented.
5. Mounted partitions are unmounted.
6. Raw write/read cycles are performed using `O_DIRECT`.
7. SHA-256 verifies the integrity of the data read back.
8. Write/read speeds and statistics are calculated.
9. VBUS voltage and current are recorded during IDLE, WRITE and READ phases.
10. The DUT is reformatted.
11. The Acroname module forces USB 2.0 operation and verifies enumeration at High-Speed.
12. Normal USB operation is restored.
13. The final report and **PASS / FAIL** result are generated.

---

## Project structure

```text
usb_tester/
├── main.py
├── config.py
├── model.py
├── controller.py
├── view.py
├── test_service.py
├── acroname.py
├── Report_Template.docx
├── requirements.txt
├── test_link.json
└── results/
```

| File | Responsibility |
| --- | --- |
| `main.py` | Application startup and privilege elevation |
| `config.py` | Test parameters, thresholds and hardware configuration |
| `model.py` | USB/system utilities, safety checks, statistics, reformatting and reports |
| `controller.py` | Application state, USB monitoring and Qt workers |
| `test_service.py` | Qualification sequence and USB 2.0 fallback logic |
| `view.py` | PyQt5 GUI and performance graph |
| `acroname.py` | USBExt3c BrainStem interface |

---

## Current configuration

Main parameters are defined in `config.py`.

| Parameter | Value |
| --- | ---: |
| Test mode | `cycles` |
| Test cycles | `2` |
| Test file size | `512 MB` |
| Block size | `4 MB` |
| Minimum write speed | `80 MB/s` |
| Minimum read speed | `160 MB/s` |
| Maximum read speed | `500 MB/s` |
| Final filesystem | `FAT32` |
| Volume label | `FISCHER` |
| Maximum test-link mating cycles | `1000` |
| VBUS sample interval | `0.2 s` |

The performance criteria are strict:

```text
Write > 80 MB/s

160 MB/s < Read < 500 MB/s
```

---

## Device safety

The tester only accepts storage devices connected through the configured dedicated USB port.

Linux `DISKSEQ` is used to prevent a newly connected device from being mistaken for the previously detected DUT.

The DUT identity is checked before critical operations such as:

- raw block-device access
- partition-table modification
- filesystem creation

The system disk is excluded from test candidates.

---

## Acroname and USB 2.0 fallback

The Acroname USBExt3c is used for:

- VBUS voltage measurement
- VBUS current measurement
- DUT power control
- enabling/disabling USB SuperSpeed
- USB 2.0 fallback verification

The fallback sequence verifies that the DUT enumerates at approximately:

```text
480 Mb/s
```

Normal USB 2 + USB 3 operation is restored afterwards.

---

## Test-link tracking

Each SAP is associated with a test-link reference.

Mating-cycle counters are stored persistently in:

```text
test_link.json
```

Example:

```text
=== TEST-LINK ===
Reference           : TL-001
Mating cycle        : 123 / 1000
```

The counter is incremented only for a physical DUT insertion, not for intentional Acroname re-enumeration during the USB 2.0 test.

---

## Reports

Results are stored in:

```text
results/
```

Typical files:

```text
USB_test_YYYYMMDD_HHMMSS_SERIAL.csv
USB_report_YYYYMMDD_HHMMSS_SERIAL.docx
USB_report_YYYYMMDD_HHMMSS_SERIAL.pdf
```

The report includes:

- DUT identification
- OF and SAP
- test-link reference and mating cycles
- performance statistics
- integrity result
- VBUS measurements
- reformatting result
- USB 2.0 fallback result
- final PASS / FAIL verdict
- performance graph

---

## Installation

Create and activate a virtual environment:

```bash
cd usb_tester

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

The station also requires the appropriate Linux utilities for partitioning and formatting, as well as:

- Acroname BrainStem Python package
- LibreOffice for PDF conversion

---

## Running

Start the application with:

```bash
python3 main.py
```

The application automatically requests administrator privileges using `pkexec` when required.

---

## Basic verification

Check Python syntax:

```bash
python3 -m py_compile *.py
```

Check module imports:

```bash
python3 -c "import config, model, controller, view, test_service, acroname, main"
```

Useful USB diagnostics:

```bash
lsblk -o NAME,PATH,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS
lsusb -t
```

To inspect the physical path of the DUT:

```bash
udevadm info --query=property --name=/dev/sdX | grep ID_PATH
```

---

## Limitations

- One DUT is tested at a time.
- The USB 2.0 stage verifies enumeration only, not storage performance.
- VBUS values are measured but currently have no independent PASS/FAIL thresholds.
- The test covers only the configured data volume, not the complete flash capacity.
- No USB eye-diagram, BER or compliance testing is performed.
- Performance depends on the complete test chain: DUT, connectors, cables, test link, USB controller and host computer.