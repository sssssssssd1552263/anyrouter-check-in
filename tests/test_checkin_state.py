import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from checkin import generate_balance_hash, get_user_info, prepare_cookies
from utils.config import ProviderConfig


def test_balance_hash_changes_when_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 125.0, 'used': 20.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_changes_when_used_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 100.0, 'used': 21.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_is_stable_for_equivalent_balances():
	left = {
		'account_2': {'quota': 50.0, 'used': 1.0},
		'account_1': {'quota': 100.0, 'used': 20.0},
	}
	right = {
		'account_1': {'used': 20.0, 'quota': 100.0},
		'account_2': {'used': 1.0, 'quota': 50.0},
	}

	assert generate_balance_hash(left) == generate_balance_hash(right)


@pytest.mark.parametrize('failure', [None, 'waf_timeout', 'missing_cookie'])
async def test_waf_cookies_follow_settings_and_require_readiness(monkeypatch, capsys, failure):
	monkeypatch.setenv('CHECKIN_HEADLESS', 'false')
	monkeypatch.setenv('CHECKIN_WAIT_TIMEOUT_MS', '120000')
	monkeypatch.setenv('CHECKIN_HUMANIZE', 'true')
	monkeypatch.setenv('CHECKIN_HUMANIZE_AGENTROUTER', 'false')
	provider = ProviderConfig(
		name='agentrouter',
		domain='https://provider.test',
		bypass_method='waf_cookies',
		waf_cookie_names=['acw_tc'],
		use_proxy=True,
		persist_profile=True,
	)
	page = SimpleNamespace(
		goto=AsyncMock(),
		wait_for_load_state=AsyncMock(),
		wait_for_function=AsyncMock(
			side_effect=TimeoutError('private challenge body') if failure == 'waf_timeout' else None
		),
	)
	cookies = [
		{'name': 'site_extra', 'value': 'private-site-value'},
		{'name': 'session', 'value': 'private-browser-session'},
	]
	if failure != 'missing_cookie':
		cookies.append({'name': 'acw_tc', 'value': 'private-waf-value'})
	context = SimpleNamespace(
		new_page=AsyncMock(return_value=page),
		cookies=AsyncMock(return_value=cookies),
		close=AsyncMock(),
	)
	launch = AsyncMock(return_value=context)
	monkeypatch.setattr('checkin.launch_login_context', launch)
	monkeypatch.setattr('checkin.prepare_browser_page', AsyncMock())
	monkeypatch.setattr('utils.browser.dismiss_popups', AsyncMock(return_value=0))

	result = await prepare_cookies('Account 1', provider, {'session': 'private-user-session'})

	settings = launch.await_args.args[0]
	assert settings.headless is False
	assert settings.humanize is False
	assert settings.persist_profile is False
	assert launch.await_args.kwargs == {'use_proxy': True}
	page.goto.assert_awaited_once_with('https://provider.test/login', wait_until='domcontentloaded', timeout=120_000)
	assert page.wait_for_function.await_args.kwargs['timeout'] == 120_000
	context.close.assert_awaited_once()
	output = capsys.readouterr().out
	assert 'private' not in output
	if failure:
		assert result is None
		assert '[SUCCESS]' not in output
	else:
		assert result == {
			'acw_tc': 'private-waf-value',
			'site_extra': 'private-site-value',
			'session': 'private-user-session',
		}
	if failure == 'waf_timeout':
		context.cookies.assert_not_awaited()
	else:
		context.cookies.assert_awaited_once_with('https://provider.test/api/user/self')


@pytest.mark.parametrize(
	'content_type, body, is_html',
	[
		('text/html', b'<!doctype html><html>private response</html>', True),
		('text/plain', b'private non-JSON response', False),
		('application/octet-stream', b'\xffprivate invalid encoding', False),
	],
)
def test_non_json_user_info_reports_only_response_metadata(content_type, body, is_html):
	client = Mock()
	client.get.return_value = httpx.Response(
		200, content=body, headers={'content-type': content_type, 'content-encoding': 'identity'}
	)
	result = get_user_info(client, {'Cookie': 'private-session'}, 'https://provider.test/api/user/self')

	assert result['success'] is False
	assert 'HTTP 200, non-JSON response' in result['error']
	assert f'Content-Type={content_type}' in result['error']
	assert 'Content-Encoding=identity' in result['error']
	assert f'body_bytes={len(body)}' in result['error']
	assert f'HTML={is_html}' in result['error']
	assert 'private' not in result['error']
