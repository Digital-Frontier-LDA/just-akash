"""Canonical Akash account-address validation."""

from __future__ import annotations

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_VALUES = {character: index for index, character in enumerate(_BECH32_CHARSET)}


def _polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return (
        [ord(character) >> 5 for character in hrp]
        + [0]
        + [ord(character) & 31 for character in hrp]
    )


def _convert_bits(values: list[int], from_bits: int, to_bits: int) -> bytes | None:
    accumulator = 0
    bit_count = 0
    output = bytearray()
    maximum = (1 << to_bits) - 1
    for value in values:
        if value < 0 or value >> from_bits:
            return None
        accumulator = (accumulator << from_bits) | value
        bit_count += from_bits
        while bit_count >= to_bits:
            bit_count -= to_bits
            output.append((accumulator >> bit_count) & maximum)
    if bit_count >= from_bits or ((accumulator << (to_bits - bit_count)) & maximum):
        return None
    return bytes(output)


def is_canonical_akash_address(value: object) -> bool:
    """Return true only for lowercase Bech32 ``akash`` addresses with a 20-byte payload."""

    if not isinstance(value, str) or value != value.lower() or len(value) != 44:
        return False
    separator = value.rfind("1")
    if separator != 5 or value[:separator] != "akash":
        return False
    try:
        data = [_BECH32_VALUES[character] for character in value[separator + 1 :]]
    except KeyError:
        return False
    if len(data) < 7 or _polymod(_hrp_expand("akash") + data) != 1:
        return False
    decoded = _convert_bits(data[:-6], 5, 8)
    return decoded is not None and len(decoded) == 20
