import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_github_request_cookie_header_is_scoped_to_github():
	from utils.browser import _github_browser_cookies
	cookies = _github_browser_cookies('user_session=private; __Host-user_session_same_site=other; logged_in=yes')
	assert len(cookies) == 3
	assert all(c['url'] == 'https://github.com/' and c['secure'] and c['httpOnly'] for c in cookies)
	assert cookies[1]['name'] == '__Host-user_session_same_site'


def test_github_request_cookie_header_accepts_host_session_without_legacy_cookie():
	cookies = _github_browser_cookies('__Host-user_session_same_site=test-only; tz=Asia%2FShanghai')
	assert cookies[0]['name'] == '__Host-user_session_same_site'
	assert cookies[0]['value'] == 'test-only'
	assert all(cookie['url'] == 'https://github.com/' for cookie in cookies)


def test_github_request_cookie_header_preserves_equals_and_analytics_punctuation():
	cookies = _github_browser_cookies('__Host-user_session_same_site=token==; _dd_s_v2=rum=1&id=(test)[1]&flags={a:b}; color_mode="auto"')
	assert {cookie['name']: cookie['value'] for cookie in cookies} == {
		'__Host-user_session_same_site': 'token==',
		'_dd_s_v2': 'rum=1&id=(test)[1]&flags={a:b}',
		'color_mode': '"auto"',
	}


@pytest.mark.parametrize('header', ['logged_in=yes', 'user_session=a\r\nHost: evil.test', '__Host-user_session_same_site='])
def test_github_request_cookie_header_rejects_missing_auth_and_newlines(header):
	from utils.browser import _github_browser_cookies
	with pytest.raises(ValueError):
		_github_browser_cookies(header)


@pytest.mark.parametrize('header', ['user session=test', 'user_session=test; bad/name=value', 'user_session=test; missing_equals'])
def test_github_request_cookie_header_rejects_invalid_names_or_pairs(header):
	with pytest.raises(ValueError):
		_github_browser_cookies(header)

from utils.browser import _github_browser_cookies, launch_login_context, load_browser_login_settings, login_with_github_cookies


def test_browser_login_settings_records_profile_persistence(monkeypatch, tmp_path):
	monkeypatch.setenv('CHECKIN_BROWSER_PROFILE_DIR', str(tmp_path))

	settings = load_browser_login_settings('Account 1', 'agentrouter', persist_profile=False)

	assert settings.persist_profile is False
	assert settings.profile_dir == tmp_path / 'agentrouter' / 'Account 1'


@pytest.mark.asyncio
async def test_launch_login_context_uses_persistent_context_when_enabled(monkeypatch, tmp_path):
	calls = {}
	context = SimpleNamespace()

	async def fake_launch_persistent_context_async(profile_dir, **kwargs):
		calls['profile_dir'] = profile_dir
		calls['kwargs'] = kwargs
		return context

	monkeypatch.setitem(
		sys.modules,
		'cloakbrowser',
		SimpleNamespace(launch_persistent_context_async=fake_launch_persistent_context_async),
	)

	settings = load_browser_login_settings('Account 1', 'anyrouter', persist_profile=True)
	settings = settings.__class__(
		headless=settings.headless,
		humanize=False,
		wait_timeout_ms=settings.wait_timeout_ms,
		profile_dir=tmp_path / 'profiles' / 'anyrouter' / 'Account 1',
		cloakbrowser_binary_path=settings.cloakbrowser_binary_path,
		persist_profile=settings.persist_profile,
	)

	result = await launch_login_context(settings)

	assert result is context
	assert calls['profile_dir'] == str(settings.profile_dir)


@pytest.mark.asyncio
async def test_launch_login_context_closes_browser_for_ephemeral_context(monkeypatch, tmp_path):
	class FakeContext:
		def __init__(self):
			self.closed = False

		async def close(self):
			self.closed = True

	class FakeBrowser:
		def __init__(self):
			self.context = FakeContext()
			self.closed = False
			self.context_kwargs = {}
			self.launch_kwargs = {}

		async def new_context(self, **kwargs):
			self.context_kwargs = kwargs
			return self.context

		async def close(self):
			self.closed = True

	browser = FakeBrowser()

	async def fake_launch_async(**kwargs):
		browser.launch_kwargs = kwargs
		return browser

	monkeypatch.setitem(
		sys.modules,
		'cloakbrowser',
		SimpleNamespace(launch_async=fake_launch_async),
	)

	settings = load_browser_login_settings('Account 1', 'agentrouter', persist_profile=False)
	settings = settings.__class__(
		headless=settings.headless,
		humanize=False,
		wait_timeout_ms=settings.wait_timeout_ms,
		profile_dir=tmp_path / 'profiles' / 'agentrouter' / 'Account 1',
		cloakbrowser_binary_path=settings.cloakbrowser_binary_path,
		persist_profile=settings.persist_profile,
	)

	context = await launch_login_context(settings)
	await context.close()

	assert context.closed is True
	assert browser.closed is True
	assert not settings.profile_dir.exists()


@pytest.fixture
def github_login(monkeypatch, tmp_path):
	monkeypatch.setenv('CHECKIN_BROWSER_PROFILE_DIR', str(tmp_path))
	monkeypatch.setenv('CHECKIN_WAIT_TIMEOUT_MS', '100')
	provider = SimpleNamespace(name='agentrouter', domain='https://agentrouter.org', login_path='/login', use_proxy=True)
	page = SimpleNamespace(goto=AsyncMock(), url='https://agentrouter.org/login')
	handlers = {}
	context = SimpleNamespace(
		new_page=AsyncMock(return_value=page), add_cookies=AsyncMock(), close=AsyncMock(),
		pages=[page],
		on=lambda event, handler: handlers.update({event: handler}),
	)
	launch = AsyncMock(return_value=context)
	button = SimpleNamespace(click=AsyncMock())
	monkeypatch.setattr('utils.browser.launch_login_context', launch)
	monkeypatch.setattr('utils.browser.prepare_browser_page', AsyncMock())
	monkeypatch.setattr('utils.browser.wait_for_waf_ready', AsyncMock())
	monkeypatch.setattr('utils.browser._first_visible_locator', AsyncMock(return_value=button))
	cookie = {'name': 'user_session', 'value': 'test-only', 'domain': '.github.com', 'path': '/', 'httpOnly': True}

	async def run(payload=None, *, url='https://agentrouter.org/api/oauth/github?code=test&state=test&mode=login', status=200, click_error=False, page_url='https://agentrouter.org/login'):
		page.url = page_url
		async def click(**kwargs):
			if click_error:
				raise TimeoutError('private-exception-message')
			response = SimpleNamespace(url=url, status=status, headers={'content-type': 'application/json'}, json=AsyncMock(return_value=payload))
			await handlers['response'](response)
		button.click.side_effect = click
		result = await login_with_github_cookies('Account 1', provider, '42', [cookie])
		assert launch.call_args.args[0].persist_profile is False
		assert launch.call_args.kwargs == {'use_proxy': True}
		context.add_cookies.assert_awaited_once_with([cookie])
		context.close.assert_awaited_once()
		assert not list(tmp_path.iterdir())
		return result

	return run


@pytest.mark.asyncio
@pytest.mark.parametrize('checked_in, expected', [(True, True), (False, 'no_reward'), (None, None), ('true', None), (0, None)])
async def test_github_login_requires_fresh_checked_in_boolean(github_login, capsys, checked_in, expected):
	data = {'id': 42, 'quota': 12500000, 'used_quota': 250000}
	if checked_in is not None:
		data['checked_in'] = checked_in
	result = await github_login({'success': True, 'data': data})
	assert result[0] == expected
	assert result[1] is None
	assert result[2]['quota'] == 25
	assert result[2]['used_quota'] == 0.5
	if expected == 'no_reward':
		output = capsys.readouterr().out
		assert 'no new credit this run' in output
		assert '[SUCCESS]' not in output and 'already' not in output.lower()


@pytest.mark.asyncio
async def test_github_reward_does_not_require_balance(github_login):
	assert await github_login({'success': True, 'data': {'id': 42, 'checked_in': True}}) == (True, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
	{'success': True, 'data': {'id': 43, 'checked_in': True}},
	{'success': False, 'data': {'id': 42, 'checked_in': True}},
	{'success': True, 'message': 'bind', 'data': {'id': 42, 'checked_in': True}},
	['not an object'],
])
async def test_github_login_rejects_wrong_identity_or_callback(github_login, payload):
	assert await github_login(payload) == (False, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('url', [
	'https://agentrouter.org/api/user/self',
	'https://other.example/api/oauth/github?mode=login',
	'https://agentrouter.org.evil.example/api/oauth/github?mode=login',
	'https://agentrouter.org/api/oauth/github?mode=bind',
])
async def test_github_login_ignores_self_and_other_origins(github_login, url):
	assert await github_login({'success': True, 'data': {'id': 42, 'checked_in': True}}, url=url) == (False, None, None)


@pytest.mark.asyncio
async def test_github_login_closes_context_on_click_timeout(github_login):
	assert await github_login(click_error=True) == (False, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('page_url, category', [
	('https://github.com/login?return_to=private-query-value', 'GitHub login'),
	('https://github.com/login/oauth/authorize?state=private-query-value', 'GitHub authorization'),
	('https://github.com/sessions/two-factor?state=private-query-value', 'GitHub challenge'),
	('https://agentrouter.org/login?state=private-query-value', 'provider login'),
	('https://agentrouter.org/oauth/github?code=private-query-value', 'provider callback'),
])
async def test_github_timeout_logs_stage_and_categories_without_secrets(github_login, capsys, page_url, category):
	assert await github_login(click_error=True, page_url=page_url) == (False, None, None)
	output = capsys.readouterr().out
	assert 'at GitHub button (TimeoutError)' in output
	assert f'page types: {category}' in output
	assert all(secret not in output for secret in ('private-query-value', 'private-exception-message', 'test-only', 'https://'))


@pytest.mark.asyncio
async def test_github_cookie_validation_logs_no_cookie_value(capsys):
	assert await login_with_github_cookies('Account 1', None, '42', 'user_session=private-cookie\r\n') == (False, None, None)
	output = capsys.readouterr().out
	assert 'at cookie validation (ValueError)' in output
	assert 'private-cookie' not in output


@pytest.mark.parametrize('invalid', [
	{'domain': 'agentrouter.org'}, {'domain': 'github.com.evil.example'}, {'domain': '.sub.github.com'},
	{'path': 'relative'}, {'name': ''}, {'value': ''}, {'secure': 'true'}, {'sameSite': 'invalid'},
])
def test_github_cookie_scope_validation(invalid):
	cookie = {'name': 'user_session', 'value': 'test-only', 'domain': '.github.com', 'path': '/'}
	with pytest.raises(ValueError):
		_github_browser_cookies([{**cookie, **invalid}])


def test_github_cookie_export_normalization_and_attribute_allowlist():
	cookie = {'name': 'user_session', 'value': 'test-only', 'domain': 'github.com', 'path': '/'}
	assert _github_browser_cookies([{**cookie, 'url': 'https://other.example', 'storeId': '0', 'sameSite': 'lax', 'expirationDate': 2000000000}]) == [
		{**cookie, 'sameSite': 'Lax', 'expires': 2000000000},
	]
