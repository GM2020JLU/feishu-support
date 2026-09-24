"""Validate the serial client's structured fresh-match receipt, not exit alone."""

import json
import re

from .ids import digest


def boot_markers(stdout: str) -> list[dict]:
    """Historical text markers only, never proof of current stage or boot health."""
    if not isinstance(stdout, str) or len(stdout.encode()) > 128000:
        return []
    try:
        value = fresh_match(stdout, r'.')
    except ValueError:
        return []
    output = value['output']
    patterns = {
        'spl': r'^U-Boot SPL \d{4}\.\d{2}[^\r\n]*',
        'u-boot': r'^U-Boot \d{4}\.\d{2}[^\r\n]*',
        'opensbi': r'^OpenSBI v\d+\.\d+[^\r\n]*',
        'linux': r'^Linux version \d+\.\d+[^\r\n]*',
    }
    result = []
    for label, pattern in patterns.items():
        match = re.search(pattern, output, re.MULTILINE)
        if match:
            result.append({'marker': label, 'start': match.start(), 'end': match.end(),
                           'text_digest': digest(match.group()),
                           'rx_seq_start': value['rx_seq_start'],
                           'scope': 'historical_serial_marker_not_current_environment'})
    return result


def version_observations(stdout: str) -> list[dict]:
    """Component-qualified banner values; never select a latest/current version."""
    if not isinstance(stdout, str) or len(stdout.encode()) > 128000:
        return []
    try:
        receipt = fresh_match(stdout, r'.')
    except ValueError:
        return []
    patterns = {'spl': r'^U-Boot SPL (\d{4}\.\d{2}[A-Za-z0-9._+-]{0,100})(?=\s|$)',
                'u-boot': r'^U-Boot (\d{4}\.\d{2}[A-Za-z0-9._+-]{0,100})(?=\s|$)',
                'opensbi': r'^OpenSBI v(\d+\.\d+[A-Za-z0-9._+-]{0,100})(?=\s|$)',
                'linux': r'^Linux version (\d+\.\d+[A-Za-z0-9._+-]{0,100})(?=\s|$)'}
    result = []
    for component, pattern in patterns.items():
        matches = list(re.finditer(pattern, receipt['output'], re.MULTILINE))
        versions = {match.group(1) for match in matches}
        for match in matches[:20]:
            result.append({'component': component, 'version': match.group(1),
                           'multiple_versions': len(versions) > 1,
                           'start': match.start(1), 'end': match.end(1),
                           'rx_seq_start': receipt['rx_seq_start'],
                           'current_environment_verified': False})
    return result


def boot_attempt_observations(stdout: str) -> list[dict]:
    """SPL loader attempts, not successful boot/rootfs/storage measurements.

    Upstream common/spl/spl.c prints this marker BEFORE spl_load_image().
    Preserve loader names; MMC labels cannot distinguish SD from eMMC.
    """
    if not isinstance(stdout, str) or len(stdout.encode()) > 128000:
        return []
    try:
        receipt = fresh_match(stdout, r'.')
    except ValueError:
        return []
    matches = list(re.finditer(
        r'^Trying to boot from ([A-Za-z0-9][A-Za-z0-9 ._+()/:-]{0,79})\r?$',
        receipt['output'], re.MULTILINE))
    return [{'loader_label': match.group(1), 'start': match.start(1),
             'end': match.end(1), 'rx_seq_start': receipt['rx_seq_start'],
             'observation_kind': 'historical_loader_attempt',
             'successful_boot_medium_verified': False,
             'truncated': len(matches) > 20}
            for match in matches[:20]]


def fresh_match(stdout: str, pattern: str) -> dict:
    try:
        value = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("structured fresh serial evidence required") from exc
    if (not isinstance(value, dict) or value.get("ok") is not True
            or value.get("fresh") is not True or value.get("matched") is not True
            or not isinstance(value.get("output"), str)
            or type(value.get("rx_seq_start")) is not int or value["rx_seq_start"] < 0):
        raise ValueError("fresh serial match receipt is invalid")
    if re.search(pattern, value["output"]) is None:
        raise ValueError("serial receipt lacks the expected marker")
    return value
