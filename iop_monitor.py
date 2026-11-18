"""Check IOP's article tracker and email only when the reported stage changes."""

import hashlib
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from utils.notify import NotificationKit

TRACKER_ORIGIN = 'https://publishingsupport.iopscience.iop.org'
TRACKER_PAGE = f'{TRACKER_ORIGIN}/track-my-article/'
TRACKER_API = 'https://oh0l6sxf16.execute-api.eu-west-1.amazonaws.com/prod'
CATALOG_URL = 'https://publishingsupport.iopscience.iop.org/wp-json/wp/v2/article-stages'


def parse_status(payload):
	if not isinstance(payload, dict):
		raise ValueError('Unexpected tracker response')
	status = {key: payload.get(key) for key in ('stage', 'sub_stage')}
	if any(type(value) is not int or value < 1 for value in status.values()):
		raise ValueError('Tracker response does not contain a valid status')
	return status


def fetch_status(article_id):
	response = httpx.get(
		TRACKER_API,
		params={'articleId': article_id},
		headers={'Origin': TRACKER_ORIGIN, 'Referer': TRACKER_PAGE},
		timeout=30,
	)
	response.raise_for_status()
	return parse_status(response.json())


def status_label(status, catalog):
	slug = f'stage-{status["stage"]}-{status["sub_stage"]}'
	for item in catalog:
		if item.get('slug') == slug:
			return html.unescape(item['title']['rendered'])
	return f'Stage {status["stage"]}.{status["sub_stage"]}'


def notify_change(article_id, previous, current):
	response = httpx.get(CATALOG_URL, params={'per_page': 30}, timeout=30)
	response.raise_for_status()
	catalog = response.json()
	old_label = status_label(previous, catalog)
	new_label = status_label(current, catalog)
	detected = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S 北京时间')
	body = (
		f'论文编号：{article_id}\n'
		f'原状态：{old_label}\n新状态：{new_label}\n'
		f'检测时间：{detected}\n\n'
		f'查看投稿状态：{TRACKER_PAGE}?manuscript-id={article_id}\n'
	)
	NotificationKit().send_email(f'[论文状态变化] {article_id}: {new_label}', body)


def check_article(article_id, state_file, baseline=None):
	current = fetch_status(article_id)
	manuscript_key = hashlib.sha256(article_id.encode()).hexdigest()
	previous = baseline
	if state_file.exists():
		saved = json.loads(state_file.read_text(encoding='utf-8'))
		if saved.get('manuscript_key') == manuscript_key:
			previous = parse_status(saved)
	if previous is not None:
		previous = parse_status(previous)
	changed = previous is not None and previous != current
	if changed:
		# A failed email must keep the old state so the next check retries the notification.
		notify_change(article_id, previous, current)
	state_file.parent.mkdir(parents=True, exist_ok=True)
	temporary = state_file.with_suffix('.tmp')
	temporary.write_text(json.dumps({'manuscript_key': manuscript_key, **current}), encoding='utf-8')
	temporary.replace(state_file)
	print(f'[INFO] Current status: {current["stage"]}.{current["sub_stage"]}')
	print('[INFO] Status changed; email sent' if changed else '[INFO] No status change; no email sent')
	return changed


def main():
	article_id = os.getenv('IOP_MANUSCRIPT_ID', '').strip()
	if not article_id:
		raise ValueError('IOP_MANUSCRIPT_ID is missing')
	baseline_text = os.getenv('IOP_BASELINE_STATUS', '').strip()
	baseline = parse_status(json.loads(baseline_text)) if baseline_text else None
	check_article(article_id, Path('.iop-monitor/state.json'), baseline)


if __name__ == '__main__':
	try:
		main()
	except Exception as error:
		# HTTP/SMTP exception messages may contain the manuscript ID or recipient.
		print(f'[ERROR] Status check or email delivery failed ({type(error).__name__}); previous state retained')
		sys.exit(1)
