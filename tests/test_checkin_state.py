import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from checkin import check_in_account, generate_balance_hash, get_user_info, get_user_info_with_browser, main, prepare_cookies
from utils.browser import BrowserLoginResult
from utils.config import AccountConfig, AppConfig, ProviderConfig


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


@pytest.mark.parametrize(
	'field, value, expected_success',
	[
		('valid', None, True),
		('without_proxy', None, True),
		('html_then_json', None, True),
		('html_timeout', None, False),
		('login_timeout', None, False),
		('readiness_timeout', None, False),
		('bad_json', None, False),
		('http_status', 401, False),
		('success', 1, False),
		('data', [], False),
		('id', 'other-account', False),
		('missing_balance', 'quota', False),
		('missing_balance', 'used_quota', False),
		('quota', -1, False),
		('quota', '500000', False),
		('quota', float('nan'), False),
		('used_quota', float('inf'), False),
		('used_quota', True, False),
		('api_user', None, False),
		('api_user', '', False),
		('cleanup_error', None, False),
	],
)
async def test_browser_api_fallback_validates_identity_and_balance(monkeypatch, capsys, field, value, expected_success):
	provider = ProviderConfig(name='agentrouter', domain='https://provider.test', sign_in_path=None, use_proxy=field != 'without_proxy')
	login_url = f'{provider.domain}{provider.login_path}'
	api_url = f'{provider.domain}{provider.user_info_path}'
	payload = {'success': True, 'data': {'id': 123, 'quota': 1250000, 'used_quota': 250000}}
	if field in payload:
		payload[field] = value
	elif field in payload['data']:
		payload['data'][field] = value
	elif field == 'missing_balance':
		payload['data'].pop(value)
	response = SimpleNamespace(
		url=api_url,
		status=value if field == 'http_status' else 200,
		headers={'content-type': 'application/json'},
		json=AsyncMock(return_value=payload, side_effect=ValueError('private-response') if field == 'bad_json' else None),
	)
	page = SimpleNamespace(on=Mock())

	async def navigate(url, **kwargs):
		if url != api_url:
			assert url == login_url
			if field == 'login_timeout':
				raise TimeoutError('private-login-page')
			return
		ready.assert_awaited_once_with(page, 60_000)
		callback = page.on.call_args.args[1]
		await callback(SimpleNamespace(url=f'{api_url}/unrelated'))
		if field in {'html_then_json', 'html_timeout'}:
			await callback(SimpleNamespace(url=api_url, status=200, headers={'content-type': 'text/html'}))
		if field != 'html_timeout':
			await callback(response)
			# A later response must not turn a rejected response into success.
			await callback(SimpleNamespace(
				url=api_url,
				status=200,
				headers={'content-type': 'application/json'},
				json=AsyncMock(return_value={'success': True, 'data': {'id': 123, 'quota': 1250000, 'used_quota': 250000}}),
			))

	page.goto = AsyncMock(side_effect=navigate)
	context = SimpleNamespace(
		new_page=AsyncMock(return_value=page),
		add_cookies=AsyncMock(),
		set_extra_http_headers=AsyncMock(),
		close=AsyncMock(side_effect=RuntimeError('private-cleanup') if field == 'cleanup_error' else None),
	)
	launch = AsyncMock(return_value=context)
	prepare = AsyncMock()
	ready = AsyncMock(side_effect=TimeoutError('private-readiness') if field == 'readiness_timeout' else None)
	monkeypatch.setattr('checkin.launch_login_context', launch)
	monkeypatch.setattr('checkin.prepare_browser_page', prepare)
	monkeypatch.setattr('checkin.wait_for_waf_ready', ready)
	actual_timeout = asyncio.timeout
	timeout_calls = []

	def short_timeout(seconds):
		timeout_calls.append(seconds)
		return actual_timeout(0.02)

	monkeypatch.setattr('checkin.asyncio.timeout', short_timeout)
	api_user = value if field == 'api_user' else '123'
	result = await get_user_info_with_browser('Agent test', provider, {'session': 'private-session'}, api_user)

	assert 'private' not in capsys.readouterr().out
	if field == 'api_user':
		assert result is None
		launch.assert_not_awaited()
		return
	assert launch.await_args.args[0].persist_profile is False
	assert launch.await_args.kwargs == {'use_proxy': provider.use_proxy}
	context.add_cookies.assert_awaited_once_with([
		{'name': 'session', 'value': 'private-session', 'url': f'{provider.domain}/', 'secure': True, 'httpOnly': True}
	])
	context.set_extra_http_headers.assert_awaited_once_with({provider.api_user_key: '123'})
	prepare.assert_awaited_once_with(page)
	navigations = [call(login_url, wait_until='domcontentloaded', timeout=60_000)]
	if field == 'login_timeout':
		ready.assert_not_awaited()
	else:
		ready.assert_awaited_once_with(page, 60_000)
	if field not in {'login_timeout', 'readiness_timeout'}:
		navigations.append(call(api_url, wait_until='commit', timeout=60_000))
	assert page.goto.await_args_list == navigations
	assert timeout_calls == [60]
	context.close.assert_awaited_once()
	if expected_success:
		assert result == {
			'success': True,
			'quota': 2.5,
			'used_quota': 0.5,
			'display': ':money: Current balance: $2.5, Used: $0.5',
		}
	else:
		assert result is None


@pytest.mark.parametrize(
	'provider_name, manual, auth, prepared, primary_success, browser_success, use_fallback',
	[
		('agentrouter', False, 'session', True, False, True, True),
		('agentrouter', False, 'session', True, False, False, True),
		('agentrouter', False, 'session', False, False, True, True),
		('agentrouter', False, 'session', True, True, True, False),
		('anyrouter', False, 'session', True, False, True, False),
		('agentrouter', True, 'session', True, False, True, False),
		('agentrouter', False, 'email', True, False, True, False),
		('agentrouter', False, 'email_failed', True, False, True, False),
	],
)
async def test_browser_fallback_only_follows_failed_agentrouter_session_auto_checkin(
	monkeypatch, provider_name, manual, auth, prepared, primary_success, browser_success, use_fallback
):
	provider = ProviderConfig(name=provider_name, domain='https://provider.test', sign_in_path='/sign_in' if manual else None)
	user_cookies = {'session': 'private-original-session'}
	account = AccountConfig(
		cookies=user_cookies,
		api_user='123',
		provider=provider_name,
		name='Agent test',
		email='test@example.com' if auth != 'session' else None,
		password='private-password' if auth != 'session' else None,
	)
	all_cookies = {**user_cookies, 'acw_tc': 'private-prior-context-cookie'}
	prepare = AsyncMock(return_value=all_cookies if prepared else None)
	login = AsyncMock(return_value=None if auth == 'email_failed' else BrowserLoginResult(all_cookies, '123'))
	before = {'success': True, 'quota': 1.0, 'used_quota': 0.0}
	after = {'success': primary_success}
	primary_result = (primary_success, before, after)
	primary = Mock(return_value=primary_result)
	info = {'success': True, 'quota': 2.5, 'used_quota': 0.5}
	fallback = AsyncMock(return_value=info if browser_success else None)
	monkeypatch.setattr('checkin.prepare_cookies', prepare)
	monkeypatch.setattr('checkin.login_with_credentials', login)
	monkeypatch.setattr('checkin.run_check_in_requests', primary)
	monkeypatch.setattr('checkin.get_user_info_with_browser', fallback)

	result = await check_in_account(account, 0, AppConfig(providers={provider_name: provider}))

	primary_used = auth != 'email_failed' and (auth == 'email' or prepared)
	assert primary.call_count == int(primary_used)
	assert fallback.await_count == int(use_fallback)
	if use_fallback:
		fallback.assert_awaited_once_with('Agent test', provider, user_cookies, '123')
	if use_fallback and browser_success:
		assert result == (None, None, info)
	else:
		assert result == (primary_result if primary_used else (False, None, None))
	assert user_cookies == {'session': 'private-original-session'}


@pytest.mark.parametrize('before', [None, {'success': False}])
async def test_single_snapshot_notification_keeps_balance_without_claiming_reward(monkeypatch, before):
	account = AccountConfig(cookies={}, name='Agent test', provider='agentrouter')
	info = {'success': True, 'quota': 12.34, 'used_quota': 5.67, 'display': 'current balance'}
	check_account = AsyncMock(return_value=(None, before, info))
	save_hash = Mock()
	push = Mock()
	monkeypatch.setattr('checkin.is_debug_enabled', lambda: False)
	monkeypatch.setattr('checkin.AppConfig.load_from_env', Mock(return_value=AppConfig(providers={})))
	monkeypatch.setattr('checkin.load_accounts_config', lambda: [account])
	monkeypatch.setattr('checkin.load_balance_hash', lambda: None)
	monkeypatch.setattr('checkin.save_balance_hash', save_hash)
	monkeypatch.setattr('checkin.check_in_account', check_account)
	monkeypatch.setattr('checkin.notify.push_message', push)

	with pytest.raises(SystemExit) as exited:
		await main()

	assert exited.value.code == 1
	check_account.assert_awaited_once()
	push.assert_called_once()
	message = push.call_args.args[1]
	assert '当前余额: $12.34' in message
	assert '累计消耗: $5.67' in message
	assert '新增奖励金额未单独核实' in message
	assert 'Success: 0/1' in message
	assert 'Unverified: 1/1' in message
	assert 'Failed: 0/1' in message
	assert not any(claim in message for claim in ('签到获得', '今日已签到', '签到前', '签到后'))
	save_hash.assert_called_once_with(generate_balance_hash({'account_1': {'quota': 12.34, 'used': 5.67}}))


@pytest.mark.parametrize('payload, expected', [
	({'success': True}, True),
	({'ret': 1}, True),
	({'code': 0}, True),
	({'success': False, 'code': 0}, False),
	({'success': 'false'}, False),
	({'success': False, 'message': '已签到'}, True),
	({}, False),
	([], False),
	('unsuccessful', False),
])
def test_manual_signin_requires_explicit_json_result(payload, expected):
	from checkin import execute_check_in
	response = Mock(status_code=200)
	if isinstance(payload, str):
		response.json.side_effect = ValueError('not JSON')
		response.text = payload
	else:
		response.json.return_value = payload
	client = Mock()
	client.post.return_value = response
	provider = ProviderConfig(name='anyrouter', domain='https://provider.test')
	assert execute_check_in(client, 'test', provider, {}) is expected


def test_user_info_success_without_login_cannot_confirm_signin(monkeypatch):
	from checkin import run_check_in_requests
	provider = ProviderConfig(name='agentrouter', domain='https://provider.test', sign_in_path=None)
	account = AccountConfig(cookies={'session': 'test'}, api_user='123', provider='agentrouter')
	info = {'success': True, 'quota': 10.0, 'used_quota': 2.0, 'display': 'balance'}
	client = Mock()
	client.__enter__ = Mock(return_value=client)
	client.__exit__ = Mock(return_value=False)
	monkeypatch.setattr('checkin.httpx.Client', Mock(return_value=client))
	monkeypatch.setattr('checkin.get_user_info', Mock(return_value=info))
	assert run_check_in_requests(account.cookies, account, 'test', provider) == (None, info, info)


@pytest.mark.parametrize('success', [True, None, False])
def test_unchanged_balance_does_not_invent_daily_reward(success):
	from checkin import format_check_in_notification
	detail = dict(name='test', success=success, before_quota=10, before_used=2,
		after_quota=10, after_used=2, credit_change=0, usage_increase=0, balance_change=0)
	message = format_check_in_notification(detail)
	assert '今日已签到' not in message
	assert '签到获得' not in message
	assert '两次余额快照无变化' in message


async def test_github_login_uses_account_specific_state(monkeypatch):
	import json
	provider = ProviderConfig(name='agentrouter', domain='https://provider.test', sign_in_path=None)
	account = AccountConfig(cookies={'session': 'old'}, api_user='123', provider='agentrouter', name='Agent test')
	github_cookies = [{'name': 'user_session', 'value': 'private', 'domain': 'github.com', 'path': '/'}]
	monkeypatch.setenv('AGENTROUTER_GITHUB_COOKIES', json.dumps({'123': github_cookies, '456': []}))
	login = AsyncMock(return_value=(True, None, None))
	monkeypatch.setattr('checkin.login_with_github_cookies', login)
	assert await check_in_account(account, 0, AppConfig(providers={'agentrouter': provider})) == (True, None, None)
	login.assert_awaited_once_with('Agent test', provider, '123', github_cookies)


async def test_fresh_login_without_reward_is_healthy_but_not_counted_as_credited(monkeypatch, capsys):
	account = AccountConfig(cookies={}, name='Agent test', provider='agentrouter')
	monkeypatch.setattr('checkin.is_debug_enabled', lambda: False)
	monkeypatch.setattr('checkin.AppConfig.load_from_env', lambda: AppConfig(providers={}))
	monkeypatch.setattr('checkin.load_accounts_config', lambda: [account])
	monkeypatch.setattr('checkin.load_balance_hash', lambda: None)
	monkeypatch.setattr('checkin.check_in_account', AsyncMock(return_value=('no_reward', None, None)))
	with pytest.raises(SystemExit) as exited:
		await main()
	assert exited.value.code == 0
	output = capsys.readouterr().out
	assert 'Success: 0/1' in output
	assert 'Login completed, no new reward: 1/1' in output
	assert 'Failed: 0/1' in output
	assert 'All accounts check-in successful' not in output
	assert 'already checked' not in output
