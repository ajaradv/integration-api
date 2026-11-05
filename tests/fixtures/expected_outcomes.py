"""Expected result per sample (1-based index into sample-tive-payloads.json).

Source: the design's "Expected outcome per sample" table.

R6 (EntryTimeUtc disagreeing with EntryTimeEpoch) is a warning: the integer epoch
is authoritative. Sample 12 carries a 5 h offset in EntryTimeUtc and is accepted
with a timestamp_mismatch warning.
"""

from __future__ import annotations

from typing import Any

# status, codes by severity, selected PX values
EXPECTED: dict[int, dict[str, Any]] = {
    1: {
        "status": "accepted",
        "blocking": [],
        "warning": ["shipment_device_mismatch"],
        "correction": [],
        "sensor": {"humidity": 38.7, "temperature": 10.08},
        "magnitude": 0.990,
        "location": {"location_source": "WiFi", "location_accuracy_category": "High"},
    },
    2: {
        "status": "quarantined",
        "blocking": ["latitude_out_of_range"],
        "warning": [],
        "correction": [],
    },
    3: {
        "status": "accepted",
        "blocking": [],
        "warning": [],
        "correction": [],
        "location": {"location_source": "GPS", "location_accuracy_category": "High"},
    },
    4: {
        "status": "accepted",
        "blocking": [],
        "warning": ["probe_fault"],
        "correction": [],
        "probe": (None, True),
    },
    5: {
        "status": "quarantined",
        "blocking": ["device_imei_missing"],
        "warning": [],
        "correction": [],
    },
    6: {
        "status": "quarantined",
        "blocking": ["timestamp_implausible"],
        "warning": ["timestamp_mismatch"],
        "correction": [],
    },
    7: {
        "status": "accepted",
        "blocking": [],
        "warning": ["probe_fault"],
        "correction": [],
        "probe": (None, True),
        "location": {
            "location_accuracy_category": "Medium",
            "location_source": "Cellular",
            "address": {
                "street": None,
                "locality": None,
                "state": None,
                "country": None,
                "postal_code": None,
                "full_address": "London, UK",
            },
        },
    },
    8: {
        "status": "accepted_with_corrections",
        "blocking": [],
        "warning": ["shipment_device_mismatch"],
        "correction": ["number_from_string"] * 3,
        "sensor": {"temperature": 10.5},
        "location": {"cellular_dbm": -100.0},
    },
    9: {
        "status": "quarantined",
        "blocking": ["longitude_out_of_range"],
        "warning": [],
        "correction": [],
    },
    10: {"status": "accepted", "blocking": [], "warning": [], "correction": [], "account_id": 999},
    11: {
        "status": "accepted",
        "blocking": [],
        "warning": [],
        "correction": [],
        "probe": (None, False),
        "sensor": {"humidity": None, "accelerometer": None},
        "location": {
            "location_accuracy": None,
            "location_accuracy_category": None,
            "location_source": None,
            "address": None,
        },
    },
    12: {  # EntryTimeUtc is 5 h off the epoch: warning only
        "status": "accepted",
        "blocking": [],
        "warning": ["timestamp_mismatch"],
        "correction": [],
    },
    13: {
        "status": "quarantined",
        "blocking": ["device_imei_missing"],
        "warning": ["entity_name_differs"],
        "correction": [],
    },
}

ACCEPTED_SAMPLES = sorted(i for i, e in EXPECTED.items() if e["status"] != "quarantined")
QUARANTINED_SAMPLES = sorted(i for i, e in EXPECTED.items() if e["status"] == "quarantined")
