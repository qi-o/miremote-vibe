from unittest import mock

import pytest

from miremote import __main__ as entry


def test_package_verification_exits_before_gui_or_hardware():
    with mock.patch.object(entry.sys, 'argv', ['candidate.exe', '--runtime-check', 'report.json']), \
            mock.patch('miremote.runtime_check.main', return_value=0) as check, \
            mock.patch.object(entry, 'app_main') as app, \
            mock.patch.object(entry, 'cmd_run') as run:
        with pytest.raises(SystemExit) as result:
            entry.main()
    assert result.value.code == 0
    check.assert_called_once()
    app.assert_not_called()
    run.assert_not_called()


def test_missing_runtime_dependencies_still_write_diagnostic_json(tmp_path, monkeypatch):
    import json
    from miremote import runtime_check, runtime
    original = runtime_check.importlib.import_module

    def missing(name):
        if name in {'winrt.windows.devices.bluetooth', 'miremote.gui'}:
            raise ModuleNotFoundError(name)
        return original(name)

    monkeypatch.setattr(runtime_check.importlib, 'import_module', missing)
    monkeypatch.setattr(runtime, 'candidate_conflict_reason', lambda: None)
    report = tmp_path / 'runtime.json'
    assert runtime_check.main([str(report)]) == 1
    result = json.loads(report.read_text('utf-8'))
    assert result['ok'] is False
    assert result['hardware_started'] is False
    assert 'winrt.windows.devices.bluetooth' in result['errors']
    assert 'miremote.gui' in result['errors']


def test_release_cli_run_uses_shared_service_instead_of_legacy_loop(monkeypatch):
    monkeypatch.setenv('MIREMOTE_RECOVERY_RELEASE', '1')
    with mock.patch.object(entry.sys, 'argv', ['remote.exe', 'run']), \
            mock.patch('miremote.service.MiRemoteService') as service, \
            mock.patch.object(entry, 'cmd_run') as legacy:
        service.return_value.start.return_value = False
        entry.main()
    service.return_value.start.assert_called_once()
    service.return_value.stop.assert_called_once()
    legacy.assert_not_called()
