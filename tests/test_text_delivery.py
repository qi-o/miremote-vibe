"""Text delivery failures must never paste the previous clipboard contents."""
from unittest import mock

import pytest

from miremote import actions


def test_clipboard_failure_cancels_paste():
    with mock.patch.object(actions, 'set_clipboard_text', return_value=False), \
            mock.patch.object(actions, 'send_combo') as send, \
            mock.patch.object(actions.time, 'sleep') as sleep:
        with pytest.raises(RuntimeError, match='剪贴板'):
            actions.type_text('new dictation')
        send.assert_not_called()
        sleep.assert_not_called()


def test_type_action_does_not_report_success_after_clipboard_failure():
    with mock.patch.object(actions, 'set_clipboard_text', return_value=False), \
            mock.patch.object(actions, 'send_combo') as send, \
            mock.patch.object(actions.time, 'sleep'):
        with pytest.raises(RuntimeError, match='剪贴板'):
            actions.perform({'type': 'type', 'text': 'new dictation'})
        send.assert_not_called()


def test_successful_clipboard_write_precedes_one_paste():
    events = []
    with mock.patch.object(actions, 'set_clipboard_text',
                           side_effect=lambda text: events.append(('write', text)) or True), \
            mock.patch.object(actions, 'send_combo',
                              side_effect=lambda combo: events.append(('paste', combo))), \
            mock.patch.object(actions.time, 'sleep'):
        result = actions.perform({'type': 'type', 'text': 'new dictation'})
    assert result == 'type 13 chars'
    assert events == [('write', 'new dictation'), ('paste', ['VK_CONTROL', 'VK_V'])]
