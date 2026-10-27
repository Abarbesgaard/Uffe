"""Regression checks for the complete main-view log selection workflow."""
import time

import pytest
from textual.widgets import Input, Static, TextArea

from uffe.app import UffeApp
from uffe.config import demo_config
from uffe.model import Note, capture, utcnow
from uffe.screens import EvidenceEditor, ReviewRequest
from test_core import line


@pytest.mark.asyncio
async def test_selection_freezes_across_environments_then_tab_resumes():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        app.backend.tick()
        await pilot.press('tab', 'down')
        selected = app.selected_log()
        app.backend.tick()
        assert app.selected_log() == selected
        await pilot.press('right', 'down', 'v')
        assert app.focus_region == 'logs'
        assert app.selected_log().context == 'demo-uat'
        assert len(app.marked_logs) == 1
        await pilot.press('left')
        assert app.selected_log() == selected
        await pilot.press('tab')
        assert app.focus_region == 'pods' and not app.paused
        assert len(app.logs_for('demo-prod')) > len(app.frozen['demo-prod'])


@pytest.mark.asyncio
async def test_page_history_keeps_log_cursor_visible():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        for index in range(60):
            app.backend.logs.append('demo-prod', line(str(index)))
        await pilot.press('tab', 'pagedown', 'pagedown', 'pagedown')
        assert app.log_cursor['demo-prod'] >= app.offsets['demo-prod'] > 0
        assert '▶' in app.query_one('#logs-0', Static).render().plain
        chosen = app.selected_log()
        await pilot.press('v')
        assert tuple(app.marked_logs.values()) == (chosen,)


@pytest.mark.asyncio
async def test_filter_shrink_keeps_selected_log_highlighted():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        for index in range(15):
            app.backend.logs.append('demo-prod', line(str(index)))
        app.backend.logs.append('demo-prod', line('unique needle'))
        await pilot.press('tab', *(['down'] * 10), '/')
        app.query_one('#search', Input).value = 'unique needle'
        await pilot.press('enter')
        assert app.selected_log().message == 'unique needle'
        assert app.log_cursor['demo-prod'] == 0
        assert '▶' in app.query_one('#logs-0', Static).render().plain


@pytest.mark.asyncio
async def test_marked_logs_can_be_added_to_existing_notes():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        app.notes.append(Note('Existing request', capture(app.resource, ())))
        await pilot.press('tab', 'v', 's')
        assert isinstance(app.screen, EvidenceEditor)
        app.screen.query_one('#instruction', TextArea).load_text('Also investigate this log')
        await pilot.click('#save')
        assert isinstance(app.screen, ReviewRequest)
        assert 'Existing request' in app.screen.payload
        assert 'Also investigate this log' in app.screen.payload
        assert not app.marked_logs
        await pilot.press('escape')
        assert app.check_action('send', ()) is True, repr(app.focused)
        await pilot.press('s')
        assert isinstance(app.screen, ReviewRequest)
        assert len(app.notes) == 2  # Reviewing again must not recapture the same log.
        app.notes.clear()


@pytest.mark.asyncio
async def test_empty_logs_and_cancel_keep_navigation_and_marks_safe():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        await pilot.press('tab', 'v', 's')
        assert isinstance(app.screen, EvidenceEditor)
        marked = dict(app.marked_logs)
        await pilot.press('escape')
        assert app.marked_logs == marked and not app.notes
        await pilot.press('/')
        app.query_one('#search', Input).value = 'no such record'
        await pilot.press('enter', 'down', 'up', 'v', 'enter')
        assert app.selected_log() is None
        assert app.marked_logs == marked
        await pilot.press('home')
        assert app.focus_region == 'pods' and not app.paused


@pytest.mark.asyncio
async def test_preexisting_pause_is_preserved_when_leaving_log_selection():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        await pilot.press('space', 'tab', 'tab')
        assert app.paused and app.focus_region == 'pods'


@pytest.mark.asyncio
async def test_received_log_appears_within_one_second_without_refresh():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        start = time.monotonic()
        app.backend.logs.append('demo-prod', line('fresh live arrival', stamp=utcnow().isoformat()))
        while 'fresh live arrival' not in app.query_one('#logs-0', Static).render().plain:
            assert time.monotonic()-start < 1
            await pilot.pause(.02)
        print(f'Log receipt-to-display: {(time.monotonic()-start)*1000:.1f}ms')


@pytest.mark.asyncio
async def test_log_navigation_p95_under_100ms_with_full_buffer():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        for index in range(5000):
            app.backend.logs.append('demo-prod', line(str(index)))
        await pilot.press('tab')
        samples = []
        for _ in range(40):
            start = time.monotonic()
            app.action_resource(1)
            samples.append(time.monotonic()-start)
        p95 = sorted(samples)[37]
        assert p95 < .1
        assert app.log_cursor['demo-prod'] == 40
        print(f'Log navigation action/render p95: {p95*1000:.1f}ms')
