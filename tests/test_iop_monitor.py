import json
from unittest.mock import Mock, patch

import pytest

from iop_monitor import check_article, fetch_status, parse_status, status_label


def test_unchanged_state_sends_nothing(tmp_path):
	state = tmp_path / 'state.json'
	with (
		patch('iop_monitor.fetch_status', return_value={'stage': 3, 'sub_stage': 1}),
		patch('iop_monitor.notify_change') as notify,
	):
		assert check_article('EXAMPLE-1.R1', state, {'stage': 3, 'sub_stage': 1}) is False
		assert check_article('EXAMPLE-1.R1', state) is False
		notify.assert_not_called()
	assert 'EXAMPLE-1' not in state.read_text()


def test_substage_change_sends_once_and_updates_state(tmp_path):
	state = tmp_path / 'state.json'
	with (
		patch('iop_monitor.fetch_status', return_value={'stage': 3, 'sub_stage': 2}),
		patch('iop_monitor.notify_change') as notify,
	):
		assert check_article('EXAMPLE-1.R1', state, {'stage': 3, 'sub_stage': 1}) is True
		assert check_article('EXAMPLE-1.R1', state) is False
		notify.assert_called_once_with('EXAMPLE-1.R1', {'stage': 3, 'sub_stage': 1}, {'stage': 3, 'sub_stage': 2})
	assert json.loads(state.read_text())['sub_stage'] == 2


def test_email_failure_keeps_old_state(tmp_path):
	state = tmp_path / 'state.json'
	with patch('iop_monitor.fetch_status', return_value={'stage': 3, 'sub_stage': 1}):
		check_article('EXAMPLE-1.R1', state)
	old_state = state.read_bytes()
	with (
		patch('iop_monitor.fetch_status', return_value={'stage': 3, 'sub_stage': 2}),
		patch('iop_monitor.notify_change', side_effect=RuntimeError('SMTP unavailable')),
		pytest.raises(RuntimeError),
	):
		check_article('EXAMPLE-1.R1', state)
	assert state.read_bytes() == old_state


def test_tracker_failure_sends_nothing_and_keeps_old_state(tmp_path):
	state = tmp_path / 'state.json'
	state.write_text('{"previous": "unchanged"}')
	with (
		patch('iop_monitor.fetch_status', side_effect=RuntimeError('Unavailable')),
		patch('iop_monitor.notify_change') as notify,
		pytest.raises(RuntimeError),
	):
		check_article('EXAMPLE-1.R1', state)
	assert state.read_text() == '{"previous": "unchanged"}'
	notify.assert_not_called()


def test_tracker_request_uses_site_origin_and_exact_id():
	response = Mock()
	response.json.return_value = {'stage': 3, 'sub_stage': 1, 'doi': 'null'}
	with patch('iop_monitor.httpx.get', return_value=response) as request:
		assert fetch_status('EXAMPLE-1.R1') == {'stage': 3, 'sub_stage': 1}
	assert request.call_args.kwargs['headers']['Origin'] == 'https://publishingsupport.iopscience.iop.org'
	assert request.call_args.kwargs['params']['articleId'] == 'EXAMPLE-1.R1'


@pytest.mark.parametrize('payload', [{}, {'stage': 0, 'sub_stage': 0}, {'stage': True, 'sub_stage': 1}, []])
def test_invalid_status_is_rejected(payload):
	with pytest.raises(ValueError):
		parse_status(payload)


def test_official_catalog_names_are_used():
	catalog = [{'slug': 'stage-3-3', 'title': {'rendered': 'Revision: All reviewer reports received'}}]
	assert status_label({'stage': 3, 'sub_stage': 3}, catalog) == 'Revision: All reviewer reports received'
