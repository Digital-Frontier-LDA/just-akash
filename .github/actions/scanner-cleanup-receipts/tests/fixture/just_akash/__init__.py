"""Offline fixed-module fixture; no SDK package initializer or credential probes."""

import os

__path__.extend(
    [os.environ["PUBLIC_CLEANUP_SOURCE_DIR"], os.environ["PUBLIC_SCANNER_CONTEXT_DIR"]]
)
