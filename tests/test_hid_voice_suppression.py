import json
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miremote.backkey import (  # noqa: E402
    BackKeyTap,
    GADGET_PROTOCOL_VERSION,
    GADGET_SCRIPT_VERSION,
    suppress_voice_usage_report,
)
from miremote import service  # noqa: E402
from miremote.tapinject import GADGET_JS, gadget_config_text  # noqa: E402


class FakeSocket:
    def __init__(self):
        self.sent = bytearray()

    def sendall(self, data):
        self.sent.extend(data)


def test_report_suppression_only_clears_f5_usage_slots():
    report = bytes.fromhex("010000f1003e008100")

    transformed, changed = suppress_voice_usage_report(report)

    assert changed is True
    assert transformed == bytes.fromhex("010000f10000008100")


def test_report_suppression_is_fail_open_when_disabled_or_unknown():
    report = bytes.fromhex("0100003e0000000000")
    invalid = bytes.fromhex("0200003e0000000000")

    assert suppress_voice_usage_report(report, enabled=False) == (report, False)
    assert suppress_voice_usage_report(invalid) == (invalid, False)


def test_control_message_is_newline_delimited_and_defaults_to_fail_open():
    client = FakeSocket()
    tap = BackKeyTap(on_edge=lambda _name, _down: None)

    assert tap._send_control(client) is True

    assert json.loads(client.sent.decode("ascii")) == {"suppress_voice": False}
    assert client.sent.endswith(b"\n")


def test_legacy_gadget_is_refreshed_for_stable_and_realtime_modes():
    normal = BackKeyTap(on_edge=lambda _name, _down: None)
    realtime = BackKeyTap(
        on_edge=lambda _name, _down: None,
        suppress_voice=True,
        log=lambda _message: None,
    )

    assert normal._handle_line({"kind": "ready"}) == "refresh"
    assert realtime._handle_line({"kind": "ready"}) == "refresh"


def test_current_gadget_is_accepted_in_stable_mode():
    tap = BackKeyTap(on_edge=lambda _name, _down: None, log=lambda _message: None)

    result = tap._handle_line({
        "kind": "ready",
        "protocol": GADGET_PROTOCOL_VERSION,
        "script_version": GADGET_SCRIPT_VERSION,
        "suppression_supported": True,
    })

    assert result is None


def test_dead_key_reports_emit_back_and_volume_edges():
    edges = []
    tap = BackKeyTap(on_edge=lambda name, down: edges.append((name, down)))

    for raw in (
        "010000f10000000000", "010000000000000000",
        "010000800000000000", "010000000000000000",
        "010000810000000000", "010000000000000000",
    ):
        tap._handle_line({"kind": "gatt_read", "raw": raw})

    assert edges == [
        ("back", True), ("back", False),
        ("volume_up", True), ("volume_up", False),
        ("volume_down", True), ("volume_down", False),
    ]


def test_current_protocol_ack_confirms_requested_suppression():
    states = []
    tap = BackKeyTap(
        on_edge=lambda _name, _down: None,
        suppress_voice=True,
        on_suppression_state=states.append,
        log=lambda _message: None,
    )

    tap._handle_line({
        "kind": "control_ack",
        "protocol": GADGET_PROTOCOL_VERSION,
        "suppress_voice": True,
    })

    assert tap._suppression_ack is True
    assert states == [True]
    tap._set_suppression_ack(False)
    assert states == [True, False]


def test_current_protocol_ack_confirms_stable_tap_channel():
    tap = BackKeyTap(on_edge=lambda _name, _down: None, log=lambda _message: None)

    tap._handle_line({
        "kind": "control_ack",
        "protocol": GADGET_PROTOCOL_VERSION,
        "suppress_voice": False,
    })

    assert tap._control_ack is True


def test_atvv_arm_command_is_bounded_and_newline_delimited():
    client = FakeSocket()
    tap = BackKeyTap(
        on_edge=lambda _name, _down: None,
        suppress_voice=True,
        log=lambda _message: None,
    )
    tap._client = client
    tap._suppression_ack = True

    assert tap.arm_voice_suppression(window_ms=5000) is True

    assert json.loads(client.sent.decode("ascii")) == {"arm_voice_ms": 2000}
    assert client.sent.endswith(b"\n")


def test_channel_binding_message_updates_runtime_state():
    tap = BackKeyTap(
        on_edge=lambda _name, _down: None,
        suppress_voice=True,
        log=lambda _message: None,
    )

    tap._handle_line({
        "kind": "voice_channel_bound",
        "handle": "0x123",
        "fingerprint": "8:01020304",
        "reason": "first_voice_report",
    })

    assert tap._voice_channel_bound is True


def test_realtime_dev_uses_global_f5_hook_as_provider_gate():
    svc = service.MiRemoteService(on_log=lambda _message: None)
    svc.config.update({
        "voice_mode": "wechat",
        "wechat_live": True,
        "device": {"voice": True, "hid_tap": True},
    })
    svc._voice = mock.Mock(ready=True)

    with mock.patch.object(service, "realtime_dev_build", return_value=True), \
            mock.patch("miremote.llhook.F5SuppressHook") as hook:
        svc._start_f5_hook_if_needed(mock.Mock())

    hook.assert_called_once()
    kwargs = hook.call_args.kwargs
    assert kwargs["on_voice"] == svc._voice.note_remote_f5
    assert kwargs["eager_resolve"] is False
    hook.return_value.start.assert_called_once()
    assert svc._llhook is hook.return_value


def test_backkey_tap_defaults_to_voice_suppression_disabled():
    tap = BackKeyTap(on_edge=lambda _name, _down: None)
    assert tap.suppress_voice is False


def test_gadget_contract_enables_reload_and_device_scoped_fail_open():
    config = json.loads(gadget_config_text())

    assert config["interaction"]["on_change"] == "reload"
    assert "protocol: PROTOCOL_VERSION" in GADGET_JS
    assert f'const PROTOCOL_VERSION = {GADGET_PROTOCOL_VERSION}' in GADGET_JS
    assert f'const SCRIPT_VERSION = "{GADGET_SCRIPT_VERSION}"' in GADGET_JS
    assert 'const RC003_TOKEN = "vid&012717_pid&32b8"' in GADGET_JS
    assert "suppressVoice = false" in GADGET_JS
    assert 'kind: "control_ack"' in GADGET_JS
    assert 'kind: "voice_channel_bound"' in GADGET_JS
    assert 'reason: "first_voice_report"' not in GADGET_JS
    assert "let connectionPending = false" in GADGET_JS
    assert "if (output !== null || connectionPending) return" in GADGET_JS
