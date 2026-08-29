import asyncio
import os
import queue
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miremote import service
from miremote.voice import FrameAccumulator, StreamingLinearResampler, VoiceDaemon
from miremote.llhook import F5SuppressHook, WM_KEYDOWN


class FakeDecoder:
    def __init__(self):
        self.next_sample = 0
        self.predictor = 0
        self.step_index = 0

    def decode(self, data):
        out = list(range(self.next_sample, self.next_sample + len(data)))
        self.next_sample += len(data)
        return out


class FakeAudioStopped:
    def __init__(self, is_set=False):
        self._is_set = is_set
        self.cleared = 0

    def is_set(self):
        return self._is_set

    def clear(self):
        self._is_set = False
        self.cleared += 1


class FakeAudioStarted:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


class FakeRawEngine:
    def __init__(self, remote=True):
        self.remote = remote

    def recent_remote_key(self, _vkey, within_ms=80.0):
        return self.remote and within_ms >= 0


class FakeCli:
    def __init__(self, items=None, stopped=False):
        self.audio_stopped = FakeAudioStopped(stopped)
        self.audio_started = FakeAudioStarted()
        self.frame_size = 120
        self.protocol_version = 0x0100
        self.stream_reason = 0x03
        self.stream_id = 1
        self.selected_codec = 0x02
        self.stop_reason = 0x02
        self._items = list(items or [])
        self.mic_closed = 0
        self.resubscribed = 0

    def drain_audio_items(self):
        items, self._items = self._items, []
        return items

    async def mic_close(self):
        self.mic_closed += 1

    async def resubscribe_audio(self):
        self.resubscribed += 1


def make_live_daemon():
    vd = VoiceDaemon(on_text=lambda _text: None, log=lambda _msg: None, mode="wechat", live=True)
    vd._live_q = queue.Queue()
    vd._live_decoder = FakeDecoder()
    vd._live_rate = 48000
    vd._live_accumulator = FrameAccumulator(120)
    vd._live_resampler = StreamingLinearResampler(16000, 48000)
    vd._live_failed = False
    return vd


def queued_samples(vd):
    chunks = []
    while not vd._live_q.empty():
        chunks.append(vd._live_q.get_nowait())
    data = b"".join(chunks)
    return list(struct.unpack(f"<{len(data) // 2}h", data))


class VoiceLiveTests(unittest.TestCase):
    def test_raw_input_fast_path_completes_f5_hook_without_delay(self):
        notifications = []
        hook = F5SuppressHook(
            raw_engine=FakeRawEngine(remote=True),
            voice_probe=lambda: False,
            on_voice=notifications.append,
            log=lambda _msg: None,
        )

        started = time.monotonic()
        swallowed = hook._decide(0x74, 0, WM_KEYDOWN)

        self.assertTrue(swallowed)
        self.assertEqual([True], notifications)
        self.assertEqual("remote", hook._state)
        self.assertLess(time.monotonic() - started, 0.05)

    def test_live_provider_waits_for_f5_gate(self):
        vd = VoiceDaemon(on_text=lambda _text: None, log=lambda _msg: None,
                         mode="wechat", live=True)
        vd._live_request_at = time.monotonic()

        def mark_gate():
            time.sleep(0.02)
            vd.note_remote_f5(True)

        thread = __import__("threading").Thread(target=mark_gate)
        thread.start()
        self.assertTrue(vd._wait_remote_f5_gate(timeout=0.2))
        thread.join()

    def test_eager_f5_resolution_notifies_before_hook_budget(self):
        started = time.monotonic()
        notifications = []

        def probe():
            return time.monotonic() - started >= 0.02

        hook = F5SuppressHook(
            voice_probe=probe,
            on_voice=notifications.append,
            eager_resolve=True,
            log=lambda _msg: None,
        )

        swallowed = hook._decide(0x74, 0, WM_KEYDOWN)

        self.assertTrue(swallowed)
        self.assertEqual([True], notifications)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_live_provider_toggle_is_idempotent_and_taps_on_stop(self):
        vd = VoiceDaemon(on_text=lambda _text: None, log=lambda _msg: None,
                         mode="wechat", live=True,
                         wechat_hotkey=["VK_CONTROL", "VK_MENU", "VK_V"])
        calls = []
        vd._tap_hotkey = lambda hold_ms=80: calls.append(hold_ms) or True

        self.assertTrue(vd._start_live_provider())
        self.assertTrue(vd._start_live_provider())
        self.assertTrue(vd._stop_live_provider("test"))

        self.assertEqual([80, 80], calls)
        self.assertFalse(vd._live_provider_started)

    def test_tap_hotkey_completes_key_up_before_returning(self):
        vd = VoiceDaemon(on_text=lambda _text: None, log=lambda _msg: None,
                         mode="wechat", live=True,
                         wechat_hotkey=["VK_CONTROL", "VK_MENU", "VK_V"])
        calls = []
        vd._press_hotkey = lambda down: calls.append(down) or True

        self.assertTrue(vd._tap_hotkey(30))

        self.assertEqual([True, False], calls)

    def test_live_write_outputs_about_three_samples_per_16k_sample_at_48k(self):
        vd = make_live_daemon()

        vd._live_write(bytes([0]) * 240)

        self.assertEqual(718, len(queued_samples(vd)))

    def test_live_write_keeps_resampler_position_across_frames(self):
        vd = make_live_daemon()

        vd._live_write(bytes([0]) * 120)
        first_len = len(queued_samples(vd))
        vd._live_write(bytes([0]) * 120)
        second = queued_samples(vd)

        self.assertEqual(358, first_len)
        self.assertEqual(360, len(second))
        self.assertGreater(second[0], 0)

    def test_begin_session_ignores_duplicate_begin_while_collecting(self):
        vd = VoiceDaemon(
            on_text=lambda _text: None,
            log=lambda _msg: None,
            mode="wechat",
            live=True,
            wechat_hotkey=["VK_CONTROL", "VK_MENU", "VK_V"],
        )
        cli = FakeCli()
        vd._collecting = True

        asyncio.run(vd._begin_session(cli))

        self.assertTrue(vd._collecting)
        self.assertIsNone(vd._live_q)

    def test_begin_session_discards_short_press_when_stop_arrived_first(self):
        logs = []
        vd = VoiceDaemon(on_text=lambda _text: None, log=logs.append, mode="wechat", live=True)
        cli = FakeCli(stopped=True)

        asyncio.run(vd._begin_session(cli))

        self.assertFalse(vd._collecting)
        self.assertEqual(1, cli.audio_stopped.cleared)
        self.assertIn("(超短按，丢弃)", logs)

    def test_end_session_sets_live_drain_to_release_realtime_session(self):
        vd = VoiceDaemon(on_text=lambda _text: None, log=lambda _msg: None, mode="wechat", live=True)
        vd._collecting = True
        vd._live_active = True
        vd._live_thread = mock.Mock()
        vd._live_thread.is_alive.return_value = False
        cli = FakeCli(items=[("audio", b"\x00" * 120)])

        asyncio.run(vd._end_session(cli))

        self.assertTrue(vd._live_drain.is_set())
        self.assertFalse(vd._collecting)

    def test_end_session_replays_buffered_audio_when_live_fallback_is_needed(self):
        vd = VoiceDaemon(on_text=lambda _text: None, log=lambda _msg: None, mode="wechat", live=True)
        vd._collecting = True
        vd._live_active = False
        vd._live_fallback_needed = True
        calls = []
        vd._wechat_playback = lambda frames, meta: calls.append((frames, meta))
        cli = FakeCli(items=[("audio", b"\x00" * 120)])

        asyncio.run(vd._end_session(cli))

        self.assertEqual(1, len(calls))
        self.assertEqual([("audio", b"\x00" * 120)], calls[0][0])

    def test_live_session_toggles_provider_off_when_cable_write_raises(self):
        vd = VoiceDaemon(
            on_text=lambda _text: None,
            log=lambda _msg: None,
            mode="wechat",
            live=True,
            wechat_hotkey=["VK_CONTROL", "VK_MENU", "VK_V"],
        )
        calls = []
        vd._live_q = queue.Queue()
        vd._live_q.put(b"\x00\x00")
        vd._live_drain.clear()
        vd.ready_delay = 0
        vd._live_rate = 48000
        vd._cable_stream = mock.Mock()
        vd._cable_stream.write.side_effect = RuntimeError("boom")
        vd._ensure_cable_stream = mock.Mock(return_value=True)
        vd._reset_live_pipeline = mock.Mock()
        vd._wait_wetype_ready = mock.Mock(return_value=True)
        vd._press_hotkey = lambda down: calls.append(down) or True
        vd._live_started.set()
        vd._live_drain.set()

        vd._live_session(vd._live_generation)

        self.assertEqual([True, False, True, False], calls)
        self.assertTrue(vd._live_fallback_needed)

    def test_load_config_accepts_realtime_dev_environment_overrides(self):
        with mock.patch.dict(os.environ, {
            "MIREMOTE_VOICE_MODE": "wechat",
            "MIREMOTE_WECHAT_LIVE": "1",
            "MIREMOTE_AUTO_START_SERVICE": "0",
        }, clear=False), mock.patch.object(service, "config_path", return_value=Path("missing-config.json")):
            cfg = service.MiRemoteService().config

        self.assertEqual("wechat", cfg["voice_mode"])
        self.assertTrue(cfg["wechat_live"])
        self.assertFalse(cfg["auto_start_service"])

    def test_realtime_dev_forces_wetype_ai_toggle_hotkey(self):
        with mock.patch.dict(os.environ, {
            "MIREMOTE_REALTIME_DEV": "1",
        }, clear=False), mock.patch.object(
            service, "config_path", return_value=Path("missing-config.json")
        ):
            cfg = service.MiRemoteService().config

        self.assertEqual(
            ["VK_CONTROL", "VK_LWIN", "VK_SHIFT"],
            cfg["wechat_hotkey"],
        )


if __name__ == "__main__":
    unittest.main()
