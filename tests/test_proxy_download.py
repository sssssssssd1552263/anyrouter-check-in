"""Exercise the proxy download fallback without network access."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
	('scenario', 'required', 'returncode', 'calls'),
	[
		('direct', 'true', 0, ['release']),
		('fallback', 'true', 0, ['release', 'metadata', 'asset']),
		('metadata_failure', 'true', 1, ['release', 'metadata']),
		('asset_failure', 'true', 1, ['release', 'metadata', 'asset']),
		('asset_failure', 'false', 0, ['release', 'metadata', 'asset']),
	],
)
def test_proxy_download(tmp_path, scenario, required, returncode, calls):
	source = (Path(__file__).resolve().parents[1] / 'scripts/setup_mihomo_proxy.sh').read_text()
	fragment = tmp_path / 'download.sh'
	fragment.write_text(source.split('gunzip -f "${ARCHIVE}"', 1)[0])
	bin_dir = tmp_path / 'bin'
	bin_dir.mkdir()
	curl = bin_dir / 'curl'
	curl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
url = args[-1]
route = 'metadata' if '/releases/tags/' in url else 'asset' if '/releases/assets/' in url else 'release'
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(route + '\\n')
scenario = os.environ['SCENARIO']
if route == 'release':
    if scenario != 'direct':
        sys.exit(22)
else:
    assert 'Authorization: Bearer test-token' in args
    if route == 'metadata':
        assert 'Accept: application/vnd.github+json' in args
        if scenario == 'metadata_failure':
            sys.exit(22)
        print(json.dumps({'assets': [
            {'name': 'wrong-platform.gz', 'url': 'https://api.github.com/repos/MetaCubeX/mihomo/releases/assets/1'},
            {'name': 'mihomo-linux-amd64-v1.19.27.gz', 'url': 'https://api.github.com/repos/MetaCubeX/mihomo/releases/assets/440046661'}
        ]}))
        sys.exit(0)
    assert url.endswith('/440046661')
    assert 'Accept: application/octet-stream' in args
    if scenario == 'asset_failure':
        sys.exit(22)
pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'downloaded')
''')
	curl.chmod(0o755)
	call_log = tmp_path / 'calls.txt'
	env = dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
		PROXY_SUBSCRIPTION_URL='https://example.invalid/subscription', GH_TOKEN='test-token',
		MIHOMO_VERSION='v1.19.27', PROXY_REQUIRED=required, RUNNER_TEMP=str(tmp_path),
		CALL_LOG=str(call_log), SCENARIO=scenario)
	result = subprocess.run(['bash', str(fragment)], env=env, capture_output=True, text=True, timeout=10)
	assert result.returncode == returncode, result.stderr
	assert call_log.read_text().splitlines() == calls
	archive = tmp_path / 'checkin-proxy/mihomo-linux-amd64-v1.19.27.gz'
	assert archive.exists() == (scenario in ('direct', 'fallback'))
