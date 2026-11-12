import httpx
import pytest

from telegram_bot.shared import magnetboard_api as api


def run_import(tmp_path, monkeypatch, handler):
    csv = tmp_path / 'data.csv'
    csv.write_text('id,title,magnets\n,test,[]\n')
    client_class = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(api.time, 'sleep', lambda _: None)
    return api.import_csv_sync('http://momo', 'user', 'secret', csv, csv.name)


@pytest.mark.parametrize('wrapped', [False, True])
def test_resource_import_keeps_blank_ids_and_waits_for_completion(tmp_path, monkeypatch, wrapped):
    polls = []
    def handler(req):
        if req.url.path == '/api/auth/login':
            return httpx.Response(200, json={'success': True}, headers={'set-cookie': 'session=auth; Path=/'})
        assert 'session=auth' in req.headers.get('cookie', '')
        if req.method == 'POST':
            assert req.url.path == '/api/data/import-jobs'
            assert req.url.params['auto_push'] == 'false'
            assert req.url.params['update_existing'] == 'false'
            assert req.url.params['submission_key']
            assert b'id,title,magnets\n,test,[]\n' in req.read()
            return httpx.Response(202, json={'id': 'job-1', 'status': 'queued'})
        assert req.url.path == '/api/data/import-jobs/job-1'
        polls.append(req)
        job = {'status': 'processing'} if len(polls) == 1 else {
            'status': 'completed', 'stats': {'total_records': 1, 'imported_records': 1, 'error_records': 0}}
        return httpx.Response(200, json={'success': True, 'data': job} if wrapped else job)
    result = run_import(tmp_path, monkeypatch, handler)
    assert len(polls) == 2
    assert result['data']['imported_records'] == 1
    assert result['job_id'] == 'job-1'


@pytest.mark.parametrize('job', [
    {'status': 'failed', 'error': 'invalid CSV'},
    {'status': 'completed', 'stats': {'error_records': 1, 'imported_records': 0}},
    {'status': 'completed'},
    {'status': 'unexpected'},
])
def test_failed_or_invalid_job_does_not_report_success(tmp_path, monkeypatch, job):
    def handler(req):
        if req.url.path == '/api/auth/login':
            return httpx.Response(200, json={'success': True})
        if req.method == 'POST':
            return httpx.Response(202, json={'id': 'job-1'})
        return httpx.Response(200, json=job)
    with pytest.raises(api.MagnetBoardError):
        run_import(tmp_path, monkeypatch, handler)


def test_no_legacy_fallback_when_resource_endpoint_missing(tmp_path, monkeypatch):
    def handler(req):
        if req.url.path == '/api/auth/login':
            return httpx.Response(200, json={'success': True})
        assert req.url.path == '/api/data/import-jobs'
        return httpx.Response(404, json={'detail': 'not found'})
    with pytest.raises(api.MagnetBoardError):
        run_import(tmp_path, monkeypatch, handler)


def test_existing_lookup_uses_primed_index(monkeypatch):
    bit = "ed2k://|file|98T@SSIS-932_LTX.mp4|7395331724|C317D5620FC7AC4D4093CD01BB071F79|/"
    other = "ed2k://|file|98T@SSIS-932_LTX.mp4|1|0123456789ABCDEF0123456789ABCDEF|/"
    lookup = api.ExistingLookup("http://momo", "user", "secret")

    def handler(req):
        if req.url.path == "/api/auth/login":
            return httpx.Response(200, json={"success": True})
        assert req.url.path == "/api/magnets"
        assert req.url.params.get("source_site") == "ltx"
        page = int(req.url.params.get("page") or 1)
        rows = [
            {"code": "SSIS-932", "source_site": "ltx", "magnet_hash": "c317d5620fc7ac4d4093cd01bb071f79", "magnets": [bit]},
            {"code": "ABP-975", "source_site": "other", "magnets": [other]},
        ]
        if page > 1:
            rows = []
        return httpx.Response(200, json={"success": True, "data": {"data": rows, "total": 1, "page": page, "page_size": 100, "total_pages": 1}})

    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))

    with lookup as primed:
        assert primed.prime() == 1

    # 索引命中后不再发请求（Client 已被 monkeypatch 成 MockTransport，找不到 code 也只走本地）
    assert lookup("ssis-932", bit) is True
    assert lookup("SSIS-932", other) is False
    assert lookup("ABP-975", other) is False
