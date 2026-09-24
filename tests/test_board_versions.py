import json

from k3_support.board_serial_evidence import version_observations


def test_versions_are_component_scoped_and_conflicts_preserved():
    output = 'U-Boot SPL 2025.01-k3\nU-Boot 2025.01-k3\nLinux version 6.6.1-k3\nOpenSBI v1.5\nU-Boot 2024.01\n'
    value = version_observations(json.dumps(dict(ok=True, fresh=True, matched=True,
                                                output=output, rx_seq_start=12)))
    assert {(v['component'], v['version']) for v in value} == {
        ('spl','2025.01-k3'), ('u-boot','2025.01-k3'), ('u-boot','2024.01'),
        ('linux','6.6.1-k3'), ('opensbi','1.5')}
    for item in value:
        assert output[item['start']:item['end']] == item['version']
        assert item['multiple_versions'] == (item['component'] == 'u-boot')
        assert item['current_environment_verified'] is False


def test_plain_text_stale_receipts_and_echoes_are_not_version_observations():
    assert version_observations('U-Boot 2025.01') == []
    for fresh, output in [(False,'U-Boot 2025.01'), (True,'=> echo U-Boot 2025.01')]:
        assert version_observations(json.dumps(dict(ok=True, fresh=fresh, matched=True,
                                                   output=output, rx_seq_start=0))) == []
