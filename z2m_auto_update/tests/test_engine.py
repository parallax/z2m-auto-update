from __future__ import annotations

from conftest import DEVICES, available, check_requests, update_requests, updating

from z2m_auto_update.config import Settings
from z2m_auto_update.engine import Engine


def test_devices_loaded_and_coordinator_skipped(engine):
    assert set(engine.devices) == {"0x0001", "0x0002", "0x0003", "0x0004"}
    assert engine.devices["0x0004"].supports_ota is False
    assert engine.devices["0x0003"].battery is True


def test_queue_orders_mains_before_battery_then_alphabetically(engine):
    available(engine, "Hall Motion")
    available(engine, "Kitchen Sink")
    available(engine, "Attic 05")
    assert [d.friendly_name for d in engine.queue()] == ["Attic 05", "Kitchen Sink", "Hall Motion"]


def test_only_one_update_started_at_a_time(engine, clock):
    available(engine, "Kitchen Sink")
    available(engine, "Attic 05")
    out = engine.tick()
    reqs = update_requests(out)
    assert len(reqs) == 1
    assert reqs[0]["id"] == "0x0002"
    assert "transaction" in reqs[0]
    # Nothing more while the first is active
    clock.advance(5)
    assert update_requests(engine.tick()) == []
    assert engine.status() == "updating"


def test_success_via_response_moves_to_next_device(engine, clock):
    available(engine, "Kitchen Sink")
    available(engine, "Attic 05")
    tx = update_requests(engine.tick())[0]["transaction"]
    updating(engine, "Attic 05", 50.0)
    engine.on_update_response(
        {
            "status": "ok",
            "transaction": tx,
            "data": {"id": "Attic 05", "from": {"software_build_id": "1"}, "to": {"software_build_id": "2"}},
        }
    )
    attic = engine.devices["0x0002"]
    assert attic.last_result == "success"
    assert attic.attempts == 0
    assert "0x0002" not in engine.active
    clock.advance(1)
    assert update_requests(engine.tick())[0]["id"] == "0x0001"


def test_duplicate_completion_is_ignored(engine, clock):
    available(engine, "Attic 05")
    tx = update_requests(engine.tick())[0]["transaction"]
    updating(engine, "Attic 05", 99.0)
    # Device state reports completion first...
    engine.on_device_state("Attic 05", {"update": {"state": "idle", "installed_version": 2, "latest_version": 2}})
    assert engine.devices["0x0002"].last_result == "success"
    events_before = len(engine.events)
    # ...then the bridge response arrives for the same update.
    engine.on_update_response({"status": "ok", "transaction": tx, "data": {"id": "Attic 05"}})
    assert len(engine.events) == events_before


def test_failure_response_counts_attempt_and_retries_up_to_max(engine, clock):
    available(engine, "Attic 05")
    for attempt in range(1, 4):
        clock.advance(1)
        reqs = update_requests(engine.tick())
        assert len(reqs) == 1, f"attempt {attempt} should start"
        engine.on_update_response(
            {
                "status": "error",
                "transaction": reqs[0]["transaction"],
                "data": {},
                "error": "Update of 'Attic 05' failed (boom)",
            }
        )
        dev = engine.devices["0x0002"]
        assert dev.attempts == attempt
        assert dev.last_error and "boom" in dev.last_error
        assert dev.update_state == "available"
    clock.advance(1)
    assert update_requests(engine.tick()) == []
    assert engine.ineligible_reason(engine.devices["0x0002"]).startswith("gave up")
    engine.retry_device("0x0002")
    clock.advance(1)
    assert len(update_requests(engine.tick())) == 1


def test_retry_delay_is_respected(clock):
    settings = Settings(cooldown_seconds=0, retry_delay_minutes=10)
    engine = Engine(settings, now=clock)
    engine.on_bridge_state({"state": "online"})
    engine.on_devices(DEVICES)
    available(engine, "Attic 05")
    tx = update_requests(engine.tick())[0]["transaction"]
    engine.on_update_response({"status": "error", "transaction": tx, "data": {}, "error": "nope"})
    clock.advance(60)
    assert update_requests(engine.tick()) == []
    assert "retry in" in engine.ineligible_reason(engine.devices["0x0002"])
    clock.advance(600)
    assert len(update_requests(engine.tick())) == 1


def test_start_timeout_skips_device_and_adopts_late_start(engine, clock):
    engine.settings.retry_delay_minutes = 60
    available(engine, "Attic 05")
    available(engine, "Kitchen Sink")
    tx = update_requests(engine.tick())[0]["transaction"]
    clock.advance(61)
    out = engine.tick()
    attic = engine.devices["0x0002"]
    assert attic.last_result == "failed"
    assert "Did not start" in attic.last_error
    # The next device is started immediately
    assert update_requests(out)[0]["id"] == "0x0001"
    # The first one starts late: it is adopted, so now two are active but no third starts
    updating(engine, "Attic 05", 1.0)
    assert set(engine.active) == {"0x0001", "0x0002"}
    assert engine.active["0x0002"].transaction == tx
    # ...and its late response is accepted
    engine.on_update_response({"status": "ok", "transaction": tx, "data": {"id": "Attic 05"}})
    assert attic.last_result == "success"
    assert "0x0002" not in engine.active


def test_stall_timeout(engine, clock):
    engine.settings.retry_delay_minutes = 60
    available(engine, "Attic 05")
    engine.tick()
    updating(engine, "Attic 05", 10.0)
    clock.advance(800)
    updating(engine, "Attic 05", 12.0)
    clock.advance(800)
    engine.tick()
    assert "0x0002" in engine.active  # progress within the last 900s
    clock.advance(200)
    engine.tick()
    assert "0x0002" not in engine.active
    assert "No progress" in engine.devices["0x0002"].last_error


def test_external_update_is_tracked_and_blocks_queue(engine, clock):
    available(engine, "Attic 05")
    updating(engine, "Kitchen Sink", 5.0)
    assert "0x0001" in engine.active
    assert engine.active["0x0001"].external is True
    assert update_requests(engine.tick()) == []
    engine.on_device_state("Kitchen Sink", {"update": {"state": "idle", "installed_version": 2, "latest_version": 2}})
    assert "0x0001" not in engine.active
    clock.advance(1)
    assert update_requests(engine.tick())[0]["id"] == "0x0002"


def test_already_in_progress_error_keeps_tracking(engine):
    available(engine, "Attic 05")
    tx = update_requests(engine.tick())[0]["transaction"]
    engine.on_update_response(
        {
            "status": "error",
            "transaction": tx,
            "data": {},
            "error": "Update or check for update already in progress for 'Attic 05'",
        }
    )
    assert "0x0002" in engine.active
    assert engine.devices["0x0002"].last_result is None


def test_pause_and_resume(engine, clock):
    available(engine, "Attic 05")
    engine.pause()
    assert update_requests(engine.tick()) == []
    assert engine.status() == "paused"
    engine.resume()
    assert len(update_requests(engine.tick())) == 1


def test_update_window_and_run_now(clock):
    import datetime as dt
    settings = Settings(cooldown_seconds=0, update_window_start="02:00", update_window_end="05:00")
    engine = Engine(settings, now=clock)
    engine.on_bridge_state({"state": "online"})
    engine.on_devices(DEVICES)
    available(engine, "Attic 05")
    noon = dt.datetime.combine(dt.date.today(), dt.time(12, 0)).timestamp()
    clock.t = noon
    assert engine.in_window() is False
    assert update_requests(engine.tick()) == []
    assert engine.status() == "waiting_for_window"
    clock.t = dt.datetime.combine(dt.date.today(), dt.time(3, 0)).timestamp()
    assert engine.in_window() is True
    clock.t = noon
    engine.trigger_run_now()
    assert len(update_requests(engine.tick())) == 1


def test_wrapping_window(clock):
    import datetime as dt
    settings = Settings(update_window_start="22:00", update_window_end="06:00")
    engine = Engine(settings, now=clock)
    for hour, expected in [(23, True), (1, True), (6, False), (12, False), (21, False), (22, True)]:
        clock.t = dt.datetime.combine(dt.date.today(), dt.time(hour, 0)).timestamp()
        assert engine.in_window() is expected, hour


def test_allow_and_deny_lists(clock):
    settings = Settings(cooldown_seconds=0, deny=["Attic *"], allow=["Philips *"])
    engine = Engine(settings, now=clock)
    engine.on_bridge_state({"state": "online"})
    engine.on_devices(DEVICES)
    for name in ("Attic 05", "Kitchen Sink", "Hall Motion"):
        available(engine, name)
    assert engine.ineligible_reason(engine.devices["0x0002"]) == "in deny list"
    assert [d.friendly_name for d in engine.queue()] == ["Kitchen Sink", "Hall Motion"]
    settings.allow = ["IKEA *"]
    assert engine.queue() == []
    assert engine.ineligible_reason(engine.devices["0x0001"]) == "not in allow list"


def test_battery_exclusion_and_skip(clock):
    settings = Settings(cooldown_seconds=0, include_battery=False)
    engine = Engine(settings, now=clock)
    engine.on_bridge_state({"state": "online"})
    engine.on_devices(DEVICES)
    available(engine, "Hall Motion")
    available(engine, "Kitchen Sink")
    assert [d.ieee for d in engine.queue()] == ["0x0001"]
    engine.skip_device("0x0001")
    assert engine.queue() == []
    engine.unskip_device("0x0001")
    assert len(engine.queue()) == 1


def test_offline_devices_are_not_queued(engine):
    available(engine, "Attic 05")
    engine.on_availability("Attic 05", {"state": "offline"})
    assert engine.queue() == []
    engine.on_availability("Attic 05", "online")
    assert len(engine.queue()) == 1


def test_update_device_jumps_queue_and_ignores_window(engine):
    available(engine, "Attic 05")
    available(engine, "Kitchen Sink")
    engine.update_device("0x0001")
    assert engine.queue()[0].ieee == "0x0001"
    assert engine.run_now is True
    reqs = update_requests(engine.tick())
    assert reqs[0]["id"] == "0x0001"


def test_bridge_offline_fails_active_updates(engine, clock):
    available(engine, "Attic 05")
    engine.tick()
    updating(engine, "Attic 05", 30.0)
    engine.on_bridge_state({"state": "offline"})
    assert engine.active == {}
    assert "offline" in engine.devices["0x0002"].last_error
    assert engine.status() == "offline"
    assert engine.tick() == []


def test_startup_checks_unknown_devices_one_at_a_time(engine, clock):
    available(engine, "Kitchen Sink")  # known state, should not be checked
    clock.advance(31)
    out = engine.tick()
    assert len(check_requests(out)) == 1
    assert check_requests(out)[0]["id"] == "0x0002"  # mains first
    assert len(engine.check_queue) == 1  # Hall Motion still waiting
    # No second check until the first is answered and the spacing has elapsed
    clock.advance(11)
    assert check_requests(engine.tick()) == []
    tx = next(iter(engine.pending_checks))
    engine.on_check_response({"status": "ok", "transaction": tx, "data": {"id": "Attic 05", "update_available": False}})
    assert engine.devices["0x0002"].update_state == "idle"
    assert check_requests(engine.tick())[0]["id"] == "0x0003"


def test_check_response_marks_available(engine, clock):
    engine.check_device("0x0002")
    out = engine.tick()
    tx = check_requests(out)[0]["transaction"]
    engine.on_check_response({"status": "ok", "transaction": tx, "data": {"id": "Attic 05", "update_available": True}})
    assert [d.ieee for d in engine.queue()] == ["0x0002"]


def test_check_error_without_id_is_matched_by_name(engine):
    engine.check_device("0x0002")
    engine.tick()
    engine.on_check_response({"status": "error", "data": {}, "error": "Device 'Attic 05' does not support OTA updates"})
    assert engine.pending_checks == {}
    assert "does not support" in engine.devices["0x0002"].last_error


def test_periodic_full_check(clock):
    settings = Settings(check_interval_hours=1, startup_grace_seconds=0)
    engine = Engine(settings, now=clock)
    engine.on_bridge_state({"state": "online"})
    engine.on_devices(DEVICES)
    for name in ("Attic 05", "Kitchen Sink", "Hall Motion"):
        engine.on_device_state(name, {"update": {"state": "idle"}})
    engine.tick()
    assert len(engine.check_queue) == 0
    clock.advance(3601)
    engine.tick()
    assert len(engine.check_queue) == 2  # the third was sent immediately
    assert len(engine.pending_checks) == 1


def test_persisted_state_round_trip(engine, clock):
    available(engine, "Attic 05")
    engine.pause()
    engine.skip_device("0x0001")
    engine.resume()
    tx = update_requests(engine.tick())[0]["transaction"]
    engine.on_update_response({"status": "error", "transaction": tx, "data": {}, "error": "nope"})
    data = engine.persisted_state()
    fresh = Engine(engine.settings, now=clock)
    fresh.restore_state(data)
    fresh.on_devices(DEVICES)
    assert fresh.devices["0x0001"].skipped is True
    assert fresh.devices["0x0002"].attempts == 1
    assert fresh.devices["0x0002"].last_error == "nope"


def test_rename_and_removal(engine):
    renamed = [dict(d) for d in DEVICES]
    renamed[1] = {**renamed[1], "friendly_name": "Kitchen Tap"}
    del renamed[2]
    engine.on_devices(renamed)
    assert "0x0002" not in engine.devices
    assert engine.name_index["Kitchen Tap"] == "0x0001"
    available(engine, "Kitchen Tap")
    assert engine.devices["0x0001"].update_state == "available"


def test_snapshot_shape(engine):
    available(engine, "Attic 05")
    snap = engine.snapshot()
    assert snap["status"] == "queued"
    assert snap["queue"] == ["0x0002"]
    assert {d["ieee"] for d in snap["devices"]} == {"0x0001", "0x0002", "0x0003"}
    assert snap["devices"][0]["ineligible_reason"] is None
