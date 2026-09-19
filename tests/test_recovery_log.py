import json
from concurrent.futures import ThreadPoolExecutor

from miremote.eventlog import RecoveryLog


def test_log_is_lazy_and_drops_audio_and_dictation(tmp_path):
    path = tmp_path / 'logs' / 'voice-recovery.jsonl'
    log = RecoveryLog(path)
    assert not path.exists()
    assert log.record('voice.ready', session=2, state='ready',
                      text='private dictation', transcript='secret', audio=b'raw',
                      frames=[b'packet'])
    log.close()
    row = json.loads(path.read_text('utf-8'))
    assert row['event'] == 'voice.ready'
    assert row['session'] == 2
    assert row['state'] == 'ready'
    assert 'time' in row
    assert not {'text', 'transcript', 'audio', 'frames'} & row.keys()
    assert 'private dictation' not in path.read_text('utf-8')


def test_rotation_bounds_disk_usage(tmp_path):
    path = tmp_path / 'voice-recovery.jsonl'
    log = RecoveryLog(path, max_bytes=500, backup_count=2)
    for i in range(40):
        log.record('voice.retry', attempt=i, error='simulated error ' * 8)
    log.close()
    files = list(tmp_path.glob('voice-recovery.jsonl*'))
    assert len(files) == 3
    for file in files:
        assert file.stat().st_size <= 500
        for line in file.read_text('utf-8').splitlines():
            json.loads(line)


def test_log_can_reopen_for_next_service_start(tmp_path):
    path = tmp_path / 'voice-recovery.jsonl'
    log = RecoveryLog(path)
    log.record('service.stop')
    log.close()
    log.record('service.start')
    log.close()
    assert [json.loads(s)['event'] for s in path.read_text('utf-8').splitlines()] == [
        'service.stop', 'service.start']


def test_unwritable_log_does_not_break_recovery(tmp_path):
    parent = tmp_path / 'not-a-directory'
    parent.write_text('existing file', 'utf-8')
    log = RecoveryLog(parent / 'voice-recovery.jsonl')
    assert log.record('voice.retry', attempt=1) is False
    assert parent.read_text('utf-8') == 'existing file'


def test_parallel_events_remain_valid_json_lines(tmp_path):
    path = tmp_path / 'voice-recovery.jsonl'
    log = RecoveryLog(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda i: log.record('voice.state', session=i), range(80)))
    log.close()
    rows = [json.loads(s) for s in path.read_text('utf-8').splitlines()]
    assert {row['session'] for row in rows} == set(range(80))


def test_invalid_or_unstructured_event_data_is_not_persisted(tmp_path):
    path = tmp_path / 'voice-recovery.jsonl'
    log = RecoveryLog(path)
    assert not log.record('dictation: private speech')
    assert log.record('voice.error', error='x' * 8000, status=object(), frames=12)
    log.close()
    row = json.loads(path.read_text('utf-8'))
    assert len(row['error']) <= 4096
    assert 'status' not in row
    assert row['frames'] == 12
