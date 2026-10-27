import asyncio
from dataclasses import replace
import json
import time

import pytest
from textual.widgets import DataTable, TextArea

from uffe.app import UffeApp
from uffe.config import demo_config
from uffe.herdr import Delivery, HandoffError, UncertainDelivery
from uffe.model import Note, capture
from uffe.screens import AgentPicker, EventsScreen, EvidenceEditor, LogsScreen, NotesScreen, ReviewRequest
from test_core import agent, line


@pytest.mark.asyncio
async def test_arrow_keys_still_select_pods_after_logs():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        before = app.resource.key
        await pilot.press("l", "down")
        assert app.resource.key != before
        assert app.follow_pod
        assert isinstance(app.focused, DataTable)
        assert app.resource.name in {r.pod for r in app.logs_for(app.environment.context)}


@pytest.mark.asyncio
async def test_all_levels_feed_includes_info_records():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)):
        app.backend.logs.append('demo-prod', line('ordinary info', level='info'))
        assert not app.warnings
        assert any(r.message == 'ordinary info' for r in app.logs_for('demo-prod'))


@pytest.mark.asyncio
async def test_namespace_change_clears_pod_filter_without_losing_buffer():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        original_count = len(app.backend.logs.records)
        await pilot.press('l', '2')
        assert not app.follow_pod
        assert app.namespace == 'support'
        assert app.logs_for('demo-prod')
        await pilot.press('1')
        assert app.namespace == 'payments'
        assert app.logs_for('demo-prod')
        await pilot.press('0')
        assert {r.namespace for r in app.logs_for('demo-prod')} == {'payments', 'support'}
        assert len(app.backend.logs.records) >= original_count


@pytest.mark.asyncio
async def test_warning_toggles_survive_info_flood_and_reset_history():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        expected = {r.id for r in app.logs_for('demo-prod')}
        await pilot.press('w')
        assert app.log_level == 'warn'
        assert {r.id for r in app.logs_for('demo-prod')} == expected
        await pilot.press('w')
        assert app.log_level == 'all'
        for index in range(5010):
            app.backend.logs.append('demo-prod', line(str(index), level='info'))
        app.offsets['demo-prod'] = 100
        await pilot.press('w')
        assert app.log_level == 'warn'
        assert {r.id for r in app.logs_for('demo-prod')} == expected
        assert app.offsets['demo-prod'] == 0
        await pilot.press('i')
        assert app.log_level == 'info' and all(r.level == 'info' for r in app.logs_for('demo-prod'))
        app.backend.logs.append('demo-prod', line('an error', level='error'))
        await pilot.press('x')
        assert app.log_level == 'error' and {r.message for r in app.logs_for('demo-prod')} == {'an error'}


@pytest.mark.asyncio
async def test_v_mark_multiple_then_s_drafts_exact_records():
    host = Host()
    app = UffeApp(demo_config(), demo=True, allow_demo_handoff=True, host=host)
    async with app.run_test(size=(170, 55)) as pilot:
        app.backend.tick()
        original_ids = {r.id for r in app.logs_for('demo-prod')[:2]}
        original_pod = app.resource.key
        await pilot.press('tab')
        assert app.focus_region == 'logs'
        await pilot.press('v', 'down', 'v')
        assert set(app.marked_logs) == original_ids
        assert app.resource.key == original_pod
        app.backend.tick()
        await pilot.press('s')
        assert isinstance(app.screen, EvidenceEditor)
        app.screen.query_one('#instruction', TextArea).load_text('Explain these two selected entries')
        await pilot.click('#save')
        assert isinstance(app.screen, ReviewRequest)
        assert app.screen.payload.count('Synthetic request timed out') == 2
        await pilot.click('#choose')
        await wait_for(lambda: isinstance(app.screen, AgentPicker), pilot)
        await pilot.press('enter')
        await wait_for(lambda: len(host.sent) == 1 and not app.sending, pilot)
        assert not app.notes
        assert host.sent[0].count('Synthetic request timed out') == 2


@pytest.mark.asyncio
async def test_selection_identity_survives_sort_and_disappearance():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        selected = app.resource
        key = (selected.context, selected.namespace)
        scope = app.backend.scopes[key]
        changed = replace(selected, name="aaa-sort-first")
        app.backend.scopes[key] = replace(scope, resources=tuple(changed if r.key == selected.key else r for r in scope.resources))
        app.paint_data()
        assert app.resource.uid == selected.uid
        app.backend.scopes[key] = replace(scope, resources=tuple(r for r in scope.resources if r.key != selected.key))
        app.paint_data()
        assert app.resource.uid == selected.uid
        assert "disappeared" in app.query_one("#resources-0", DataTable).border_subtitle
        await pilot.press("down")
        assert app.resource.uid != selected.uid


@pytest.mark.asyncio
async def test_capture_editor_freezes_evidence_and_saves_note():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        original = app.resource
        await pilot.press("c")
        assert isinstance(app.screen, EvidenceEditor)
        captured = app.screen.evidence
        app.backend.tick()
        app.screen.query_one("#instruction", TextArea).load_text("Explain this retry; propose a code change")
        await pilot.click("#save")
        assert len(app.notes) == 1
        assert app.notes[0].evidence == captured
        assert app.notes[0].evidence.resource_uid == original.uid
        app.backend.tick()
        assert app.notes[0].evidence == captured
        await pilot.press("n")
        assert isinstance(app.screen, NotesScreen)
        await pilot.click("#edit")
        assert isinstance(app.screen, EvidenceEditor)
        app.screen.query_one("#instruction", TextArea).load_text("Edited request")
        await pilot.click("#save")
        assert len(app.notes) == 1 and app.notes[0].instruction == "Edited request"
        app.notes.clear()


@pytest.mark.asyncio
async def test_logs_inspector_preserves_selection_and_multi_capture():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        app.backend.tick()
        await pilot.press("ctrl+l")
        assert isinstance(app.screen, LogsScreen)
        frozen = app.screen.records
        app.backend.tick()
        await pilot.press("m", "down", "m", "c")
        assert isinstance(app.screen, EvidenceEditor)
        assert app.screen.evidence.source == "Logs"
        assert len(frozen) == 4
        assert app.screen.evidence.excerpt.count("Synthetic request") == 2


@pytest.mark.asyncio
async def test_event_capture_keeps_event_uid():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        await pilot.press("e")
        assert isinstance(app.screen, EventsScreen)
        event = app.screen.events[0]
        await pilot.press("enter")
        assert isinstance(app.screen, EvidenceEditor)
        data = json.loads(app.screen.evidence.snapshot_json)
        assert data["event"]["uid"] == event.uid
        assert app.screen.evidence.resource_uid == event.resource_uid


class Host:
    def __init__(self, failure=""):
        self.failure = failure
        self.sent = []

    def discover(self):
        return (agent(),)

    def draft(self, target, payload):
        self.sent.append(payload)
        if self.failure == "failure":
            raise HandoffError("refused")
        if self.failure == "uncertain":
            raise UncertainDelivery("check before retrying")
        return Delivery(target.name, self.failure != "focus")


async def wait_for(predicate, pilot, timeout=3):
    start = time.monotonic()
    while not predicate():
        if time.monotonic()-start > timeout:
            raise AssertionError("Timed out waiting for UI state")
        await pilot.pause(.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "failure", "uncertain", "focus"])
async def test_full_review_choose_and_draft_preserves_correct_batch(failure):
    host = Host(failure)
    app = UffeApp(demo_config(), demo=True, allow_demo_handoff=True, host=host)
    async with app.run_test(size=(170, 55)) as pilot:
        app.notes.append(Note("Investigate", capture(app.resource, ())))
        await pilot.press("s")
        assert isinstance(app.screen, ReviewRequest)
        await pilot.click("#choose")
        await wait_for(lambda: isinstance(app.screen, AgentPicker), pilot)
        assert host.sent == []  # Never send merely because a single agent exists.
        await pilot.press("enter")
        await wait_for(lambda: len(host.sent) == 1 and not app.sending, pilot)
        assert len(app.notes) == (1 if failure in ("failure", "uncertain") else 0)
        assert app.uncertain == (failure == "uncertain")
        assert "Investigate" in host.sent[0]
        app.notes.clear()


@pytest.mark.asyncio
async def test_demo_never_discovers_agent_without_optin():
    class ForbiddenHost:
        def discover(self):
            raise AssertionError("must not discover real agents in the offline demo")
    app = UffeApp(demo_config(), demo=True, host=ForbiddenHost())
    async with app.run_test(size=(150, 50)) as pilot:
        app.notes.append(Note("Demo", capture(app.resource, ())))
        await pilot.press("s")
        await pilot.click("#choose")
        await pilot.pause(.1)
        assert len(app.notes) == 1
        app.notes.clear()


@pytest.mark.asyncio
async def test_background_latency_does_not_block_keyboard():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)) as pilot:
        for i in range(5000):
            app.backend.logs.append("demo-prod", line(str(i)))
        before = app.resource.key
        start = time.monotonic()
        app.action_resource(1)
        elapsed = time.monotonic()-start
        assert app.resource.key != before
        assert elapsed < .1
        await pilot.press("space")
        frozen = app.frozen
        app.backend.tick()
        assert app.frozen == frozen
        await pilot.press("home")
        assert not app.paused


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(110, 35), (248, 60)])
async def test_resize_and_help(size):
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press("escape")
        assert app.resource is not None
        assert " _   _" in app.query_one("#brand").render().plain


@pytest.mark.asyncio
async def test_failed_context_keeps_previous_data_visible():
    app = UffeApp(demo_config(), demo=True)
    async with app.run_test(size=(170, 55)):
        selected = app.resource
        key = (selected.context, selected.namespace)
        app.backend.scopes[key] = replace(app.backend.scopes[key], error="unavailable")
        app.paint_data()
        assert app.resource.uid == selected.uid
        assert "STALE" in app.query_one("#summary-0").render().plain
        assert "LIVE" in app.query_one("#summary-1").render().plain


@pytest.mark.asyncio
async def test_discover_is_background_and_notes_survive():
    class SlowHost:
        def discover(self):
            time.sleep(.3)
            raise HandoffError("no connection")
    app = UffeApp(demo_config(), demo=True, host=SlowHost(), allow_demo_handoff=True)
    async with app.run_test(size=(170, 55)) as pilot:
        app.notes.append(Note("Investigate", capture(app.resource, ())))
        app.discover_agents(tuple(app.notes), "payload")
        await asyncio.sleep(.02)
        before = app.resource.key
        app.action_resource(1)
        assert app.resource.key != before
        await pilot.pause(.4)
        assert len(app.notes) == 1
        app.notes.clear()
