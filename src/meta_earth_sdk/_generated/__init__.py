"""Integrity pins for deterministic private runtime descriptor resources.

Reproduce with tools/generate_runtime_protocol.py. No generated pb2 module or
private-pool descriptor is ever registered in protobuf's default pool.
"""
from typing import Final

DESCRIPTOR_SHA256: Final = "2f1ee50dac69a75d0ca018f9b9c13148b634cd72284ac9d43bb6d9e4b8a2dda9"
ADMISSION_SHA256: Final = "cb7ebd2f4d2c74097023a396d5c2b0e62766c4700730afb9205cbd77476daef9"