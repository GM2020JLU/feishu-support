import json

import pytest

from k3_support.board_serial_evidence import fresh_match
from k3_support.board_serial_evidence import boot_attempt_observations


def receipt(**changes):
    return json.dumps({"ok": True, "fresh": True, "matched": True,
                       "output": "ROM: usb download handler", "rx_seq_start": 42, **changes})


def test_structured_fresh_marker_is_required():
    assert fresh_match(receipt(), "ROM: usb download handler")["rx_seq_start"] == 42


@pytest.mark.parametrize("raw", ["fresh ROM: usb download handler", "{}", "null",
    receipt(fresh=False), receipt(matched=False), receipt(ok=1), receipt(output="old prompt"),
    receipt(rx_seq_start=True), receipt(rx_seq_start=-1), receipt(rx_seq_start=None)])
def test_exit_success_does_not_make_bad_serial_evidence_valid(raw):
    with pytest.raises(ValueError):
        fresh_match(raw, "ROM: usb download handler")


def test_loader_attempts_preserve_order_without_inferring_success_or_media():
    text = 'Trying to boot from MMC1\r\nError: -5\nTrying to boot from SPI\n'
    rows = boot_attempt_observations(receipt(output=text))
    assert [row['loader_label'] for row in rows] == ['MMC1', 'SPI']
    for row in rows:
        assert text[row['start']:row['end']] == row['loader_label']
        assert row['successful_boot_medium_verified'] is False
        assert row['observation_kind'] == 'historical_loader_attempt'
        assert row['rx_seq_start'] == 42
        assert not row['truncated']


@pytest.mark.parametrize('text', ['UFS: detected device', 'NVMe device 0',
    'root=/dev/sda2', '=> echo Trying to boot from MMC1',
    'Example: Trying to boot from SPI', 'Trying to boot from <script>',
    'Trying to boot from ' + 'A' * 81])
def test_device_presence_commands_and_examples_are_not_loader_attempts(text):
    assert boot_attempt_observations(receipt(output=text)) == []


def test_loader_attempts_require_fresh_receipt_and_bound_output():
    text = 'Trying to boot from MMC1\n' * 21
    assert boot_attempt_observations(receipt(output=text, fresh=False)) == []
    assert boot_attempt_observations(text) == []
    rows = boot_attempt_observations(receipt(output=text))
    assert len(rows) == 20 and all(row['truncated'] for row in rows)
    assert boot_attempt_observations(receipt(output='x' * 128001)) == []
