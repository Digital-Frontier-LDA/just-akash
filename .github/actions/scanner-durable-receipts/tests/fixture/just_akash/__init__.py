"""Offline fixture namespace; import exact real public modules without SDK __init__."""

import os

__path__.append(os.environ["PUBLIC_PROVENANCE_SOURCE_DIR"])
