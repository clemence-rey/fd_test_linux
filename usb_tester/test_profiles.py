"""SAP-specific USB qualification profiles."""

TEST_PROFILES = {
    "123456": {
        "test_link": "TL-001",
        "min_write_mbps": 70.0,
        "min_read_mbps": 130.0,
        "max_read_mbps": 500.0,
        "test_limit_mode": "cycles",
        "test_cycles": 2,
        "test_file_size_mb": 512,
        "usb2_enabled": True,
    },

    "153456": {
        "test_link": "TL-002",
        "min_write_mbps": 100.0,
        "min_read_mbps": 170.0,
        "max_read_mbps": 500.0,
        "test_limit_mode": "duration",
        "test_duration": 30,
        "test_file_size_mb": 512,
        "usb2_enabled": True,
    },

    "126456": {
        "test_link": "TL-003",
        "min_write_mbps": 25.0,
        "min_read_mbps": 35.0,
        "max_read_mbps": 500.0,
        "test_limit_mode": "cycles",
        "test_cycles": 2,
        "test_file_size_mb": 256,
        "usb2_enabled": False,
    },
}