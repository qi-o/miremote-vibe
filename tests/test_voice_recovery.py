import asyncio
import sys
import time
import types
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miremote import voice


class _Status:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class _FakeCccd:
    NOTIFY = object()
    NONE = object()


@pytest.fixture
def fake_winrt(monkeypatch):
    module = types.ModuleType("winrt.windows.devices.bluetooth.genericattributeprofile")
    module.GattClientCharacteristicConfigurationDescriptorValue = _FakeCccd
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


class FakeChannel:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.tokens = []
        self.removed = []
        self.write_values = []

    def add_value_changed(self, callback):
        token = (len(self.tokens) + 1, callback)
        self.tokens.append(token)
        return token

    def remove_value_changed(self, token):
        self.removed.append(token)

    async def write_client_characteristic_configuration_descriptor_async(self, value):
        self.write_values.append(value)
        if self.statuses:
            return self.statuses.pop(0)
        return _Status("SUCCESS")


class FakeAudioStopped:
    def __init__(self, is_set=False):
        self._event = asyncio.Event()
        if is_set:
            self._event.set()

    def is_set(self):
        return self._event.is_set()

    def clear(self):
        if self._event.is_set():
            self._event.clear()

    def set(self):
        self._event.set()

    async def wait(self):
        await self._event.wait()


class FakeLoopClient:
    instances = []

    def __init__(self, addr):
        self.addr = addr
        self.index = len(type(self).instances)
        type(self).instances.append(self)
        self.audio_stopped = FakeAudioStopped()
        self.audio_started = FakeAudioStopped()
        self.audio_frames = []
        self.dev = SimpleNamespace(connection_status="CONNECTED")
        self.frame_size = 120
        self.protocol_version = 0x0100
        self.stream_reason = 3
        self.stream_id = self.index + 1
        self.selected_codec = 2
        self.stop_reason = 2
        self.closed = False
        self.resubscribed = 0
        self._items = []
        self._disconnect_event = asyncio.Event()

    async def connect(self):
        pass

    async def get_caps(self):
        return b"\x0b\x01\x00\x02\x03\x00\x78\x00\x00"

    def parse_caps(self, _resp):
        return {
            "adpcm_16k": True,
            "version_byte": "0x0100",
            "frame_size": 120,
            "interaction": 3,
        }

    async def subscribe_audio(self):
        pass

    async def mic_close(self):
        pass

    async def resubscribe_audio(self):
        self.resubscribed += 1

    async def close(self):
        self.closed = True
        self.audio_stopped.set()

    def drain_audio_items(self):
        items, self._items = self._items, []
        return items

    def disconnect(self):
        self.dev.connection_status = "DISCONNECTED"
        self._disconnect_event.set()

    def disconnected_event(self):
        return self._disconnect_event

    def connection_status(self):
        return str(self.dev.connection_status).lower()

    def is_disconnected(self):
        return self.connection_status() == "disconnected"


async def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


async def _cancel(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_subscribe_audio_checks_cccd_failure_and_rolls_back(fake_winrt):
    client = voice.AtvvClient(addr=1)
    client.audio_ch = FakeChannel([_Status("UNREACHABLE")])

    with pytest.raises(RuntimeError, match="音频.*订阅"):
        asyncio.run(client.subscribe_audio())

    assert client._audio_subscribed is False
    assert client._tokens == []
    assert len(client.audio_ch.removed) == 1


def test_resubscribe_failure_propagates_to_daemon_recovery(fake_winrt):
    class Cli(FakeLoopClient):
        async def resubscribe_audio(self):
            raise RuntimeError("音频通知重订阅失败: UNREACHABLE")

    daemon = voice.VoiceDaemon(
        on_text=lambda _text: None,
        log=lambda _msg: None,
        mode="wechat",
        live=False,
    )
    daemon._collecting = True
    cli = Cli(addr=1)
    cli._items = [("audio", b"\x00" * 120)]

    with pytest.raises(RuntimeError, match="重订阅失败"):
        asyncio.run(daemon._end_session(cli))

    assert daemon._collecting is False


def test_idle_silent_disconnect_rebuilds_client(monkeypatch):
    async def scenario():
        FakeLoopClient.instances = []
        daemon = voice.VoiceDaemon(
            on_text=lambda _text: None,
            log=lambda _msg: None,
            mode="wechat",
            live=False,
        )
        monkeypatch.setattr(voice, "AtvvClient", FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            FakeLoopClient.instances[-1].disconnect()
            await _wait_until(lambda: len(FakeLoopClient.instances) >= 2, timeout=3.5)
            assert FakeLoopClient.instances[0].closed
            assert daemon.ready
        finally:
            daemon._stop.set()
            await _cancel(task)

    asyncio.run(scenario())


def test_capture_disconnect_and_stop_do_not_leave_wait_hung(monkeypatch):
    async def scenario():
        FakeLoopClient.instances = []
        daemon = voice.VoiceDaemon(
            on_text=lambda _text: None,
            log=lambda _msg: None,
            mode="wechat",
            live=False,
        )
        monkeypatch.setattr(voice, "AtvvClient", FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            daemon.begin()
            await _wait_until(lambda: daemon._collecting)
            FakeLoopClient.instances[-1].disconnect()
            daemon._stop.set()
            await asyncio.wait_for(task, timeout=1.0)
            assert daemon._collecting is False
        finally:
            if not task.done():
                await _cancel(task)

    asyncio.run(scenario())


def test_closed_atvv_client_ignores_late_audio_callbacks(fake_winrt):
    seen = []
    client = voice.AtvvClient(addr=1)
    client.on_audio_live = seen.append
    client.audio_ch = FakeChannel([_Status("SUCCESS")])
    asyncio.run(client.subscribe_audio())
    asyncio.run(client.close())

    args = SimpleNamespace(characteristic_value=b"\x01" * 120)
    client._on_audio(None, args)

    assert seen == []
    assert client.audio_frames == []


def test_stale_old_client_callbacks_do_not_trigger_new_daemon_session(monkeypatch):
    async def scenario():
        FakeLoopClient.instances = []
        daemon = voice.VoiceDaemon(
            on_text=lambda _text: None,
            log=lambda _msg: None,
            mode="wechat",
            live=True,
        )
        monkeypatch.setattr(voice, "AtvvClient", FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            old = FakeLoopClient.instances[-1]
            old.disconnect()
            await _wait_until(lambda: len(FakeLoopClient.instances) >= 2, timeout=3.5)
            old.on_audio_live(b"\x00" * 120)
            await asyncio.sleep(0.05)
            assert not daemon._collecting
        finally:
            daemon._stop.set()
            await _cancel(task)

    asyncio.run(scenario())


def test_stable_wechat_capture_has_no_fixed_two_second_total_limit(monkeypatch):
    async def scenario():
        FakeLoopClient.instances = []
        daemon = voice.VoiceDaemon(
            on_text=lambda _text: None,
            log=lambda _msg: None,
            mode="wechat",
            live=False,
        )
        monkeypatch.setattr(voice, "AtvvClient", FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            daemon.begin()
            await _wait_until(lambda: daemon._collecting)
            await asyncio.sleep(2.2)
            assert daemon._collecting
        finally:
            daemon._stop.set()
            FakeLoopClient.instances[-1].audio_stopped.set()
            await _cancel(task)

    asyncio.run(scenario())


@pytest.mark.parametrize('capturing', [False, True])
def test_public_stop_releases_client_and_clears_readiness(monkeypatch, capturing):
    async def scenario():
        FakeLoopClient.instances = []
        daemon = voice.VoiceDaemon(on_text=lambda _: None, log=lambda _: None,
                                   mode='wechat', live=False)
        monkeypatch.setattr(voice, 'AtvvClient', FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            if capturing:
                daemon.begin()
                await _wait_until(lambda: daemon._collecting)
            daemon.stop()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
            assert FakeLoopClient.instances[-1].closed
            assert not daemon.ready
            assert not daemon._collecting
            assert daemon.recovery_status()['state'] == 'stopped'
        finally:
            if not task.done():
                await _cancel(task)
    asyncio.run(scenario())


def test_stop_cancels_hung_connection_and_releases_partial_client(monkeypatch):
    class HungConnect(FakeLoopClient):
        async def connect(self):
            await asyncio.Event().wait()

    async def scenario():
        HungConnect.instances = []
        daemon = voice.VoiceDaemon(on_text=lambda _: None, log=lambda _: None,
                                   mode='wechat', live=False)
        monkeypatch.setattr(voice, 'AtvvClient', HungConnect)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: bool(HungConnect.instances))
            daemon.stop()
            done, _ = await asyncio.wait({task}, timeout=0.4)
            assert task in done
            with suppress(asyncio.CancelledError):
                await task
            assert HungConnect.instances[-1].closed
        finally:
            if not task.done():
                await _cancel(task)
    asyncio.run(scenario())


def test_connect_timeout_retries_without_manual_bluetooth_reset(monkeypatch):
    class HungConnect(FakeLoopClient):
        async def connect(self):
            if self.index == 0:
                await asyncio.Event().wait()

    async def scenario():
        HungConnect.instances = []
        daemon = voice.VoiceDaemon(on_text=lambda _: None, log=lambda _: None,
                                   mode='wechat', live=False)
        daemon.CONNECT_TIMEOUT = 0.04
        daemon.RECONNECT_INITIAL = 0.01
        monkeypatch.setattr(voice, 'AtvvClient', HungConnect)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready, timeout=0.5)
            assert len(HungConnect.instances) == 2
            assert HungConnect.instances[0].closed
        finally:
            daemon.stop()
            await _cancel(task)
    asyncio.run(scenario())


def test_missing_disconnect_event_is_polled_while_recording(monkeypatch):
    async def scenario():
        FakeLoopClient.instances = []
        daemon = voice.VoiceDaemon(on_text=lambda _: None, log=lambda _: None,
                                   mode='wechat', live=False)
        monkeypatch.setattr(voice, 'AtvvClient', FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            daemon.begin()
            await _wait_until(lambda: daemon._collecting)
            client = FakeLoopClient.instances[-1]
            client.dev.connection_status = 'DISCONNECTED'  # Deliberately omit callback.
            await _wait_until(lambda: client.closed, timeout=1.5)
            assert not daemon._collecting
            assert not daemon.ready
        finally:
            daemon.stop()
            await _cancel(task)
    asyncio.run(scenario())


def test_cancelled_subscription_removes_callback_token(fake_winrt):
    class HungChannel(FakeChannel):
        async def write_client_characteristic_configuration_descriptor_async(self, value):
            await asyncio.Event().wait()

    async def scenario():
        client = voice.AtvvClient(addr=1)
        client.audio_ch = HungChannel([])
        task = asyncio.create_task(client.subscribe_audio())
        await _wait_until(lambda: bool(client.audio_ch.tokens))
        await _cancel(task)
        assert client.audio_ch.removed == client.audio_ch.tokens
        assert client._tokens == []
        assert not client._audio_subscribed
    asyncio.run(scenario())


def test_new_physical_session_delivers_once_after_reconnect(monkeypatch):
    async def scenario():
        FakeLoopClient.instances = []
        delivered = []
        daemon = voice.VoiceDaemon(on_text=lambda _: None, log=lambda _: None,
                                   mode='wechat', live=False)
        daemon.RECONNECT_INITIAL = 0.01
        daemon._wechat_playback = lambda frames, meta: delivered.append((frames, meta))
        monkeypatch.setattr(voice, 'AtvvClient', FakeLoopClient)
        task = asyncio.create_task(daemon._main())
        try:
            await _wait_until(lambda: daemon.ready)
            old = FakeLoopClient.instances[-1]
            daemon.begin()
            await _wait_until(lambda: daemon._collecting)
            old.disconnect()
            await _wait_until(lambda: len(FakeLoopClient.instances) == 2 and daemon.ready, 1.0)
            current = FakeLoopClient.instances[-1]
            daemon.begin()
            await _wait_until(lambda: daemon._collecting)
            current._items = [('audio', b'\x12' * 120)]
            current.audio_stopped.set()
            await _wait_until(lambda: bool(delivered))
            assert len(delivered) == 1
            assert delivered[0][1]['stream_id'] == current.stream_id
            assert old.closed
        finally:
            daemon.stop()
            await _cancel(task)
    asyncio.run(scenario())


def test_winrt_notifications_are_processed_on_owner_loop_thread():
    import threading
    async def scenario():
        client = voice.AtvvClient(addr=1)
        client._loop = asyncio.get_running_loop()
        owner = threading.get_ident()
        seen = []
        client.on_audio_live = lambda data: seen.append((threading.get_ident(), data))
        payload = b'\x01' * 120
        await asyncio.to_thread(client._on_audio, None,
                                SimpleNamespace(characteristic_value=payload))
        await _wait_until(lambda: bool(seen))
        assert seen == [(owner, payload)]
        await client.close()
    asyncio.run(scenario())


def test_stop_during_playback_releases_hotkey_and_closes_stream_in_owner(monkeypatch):
    from unittest import mock
    daemon = voice.VoiceDaemon(on_text=lambda _: None, log=lambda _: None,
                               mode='wechat', live=False, ready_delay=0)
    hotkeys = []
    writes = []
    stream = mock.Mock()
    def write(data):
        writes.append(data)
        daemon._stop.set()
    stream.write.side_effect = write
    daemon._cable_stream = stream
    daemon._decode_session = lambda frames, meta: (
        [10] * 16000,
        {'notifications': 67, 'raw_bytes': 8040, 'sync_count': 0},
        {'duration': 1, 'rms': 10, 'peak': 10})
    daemon._write_capture_diagnostics = lambda *args: None
    daemon._ensure_cable_stream = lambda: True
    daemon._find_cable_input = lambda: 0
    daemon._press_hotkey = lambda down: hotkeys.append(down) or True
    monkeypatch.setitem(sys.modules, 'sounddevice',
                        SimpleNamespace(query_devices=lambda _: {'default_samplerate': 48000}))
    daemon._wechat_playback([b'\x00' * 120])
    assert hotkeys == [True, False]
    assert len(writes) == 1
    assert len(writes[0]) <= 4800
    stream.stop.assert_called_once()
    stream.close.assert_called_once()
