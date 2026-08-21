# Architecture notes

Internal notes on how `plugin.py` is put together, for future maintenance. See `README.md` for user-facing documentation.

## Threading model

Indigo plugins run on a single "main" thread that calls the standard callbacks (`deviceStartComm`, `actionControlDimmerRelay`, etc.). `aiopulse2`'s `Hub` is `asyncio`-based, so this plugin runs its own `asyncio` event loop on a dedicated background thread:

- `Plugin.__init__` creates `self.loop` (a fresh `asyncio` event loop) and starts `self.run_loop` on a daemon `Thread`.
- Every call into hub/shade objects from the main thread is marshalled onto that loop with `asyncio.run_coroutine_threadsafe(...)` (see `reconnectHub`, `move_shade`, `actionControlDimmerRelay`, etc.).
- `shutdown()` cancels the background tasks, stops each hub cleanly, then calls `self.loop.call_soon_threadsafe(self.loop.stop)` to unwind the thread.

## Per-hub background tasks

`connect_to_hub(dev_id, hub_ip)` is the entry point for bringing a hub online (called from `deviceStartComm`, `reconnectHub`, and `deviceUpdated` when the IP changes). It:

1. Creates an `aiopulse2.Hub` and starts its `run()` coroutine as a task.
2. Waits (up to 15s) for `hub.rollers_known` before proceeding, so discovery has a real shade list to work with.
3. Calls `discover_shades()` to register/subscribe every shade the hub reports.
4. Starts two long-running per-hub tasks, tracked in dicts keyed by hub device ID so `_disconnect_hub` can cancel them cleanly:
   - `self.heartbeat_tasks[dev_id]` → `hub_heartbeat()`
   - `self.battery_refresh_tasks[dev_id]` → `battery_refresh()`
5. Sends an initial `request_battery_info()` shadow query.

`_disconnect_hub(dev)` is the inverse: unsubscribes all shade callbacks, cancels both background tasks, stops the hub's websocket, and clears it from `self.hubs`.

### Heartbeat / auto-reconnect (`hub_heartbeat`)

Polls `hub.connected` every 30s. After **3 consecutive** unresponsive checks (~90s), it calls `self.reconnectHub(None, dev)` to force a reconnect — note this reuses the same callback the "Reconnect Hub" action uses, just invoked with `action=None`. A fresh reconnect starts a brand new heartbeat task, so the old one `break`s out rather than continuing.

### Battery refresh (`battery_refresh`)

Every 5 minutes, re-sends a shadow query (`request_battery_info`) for any roller that doesn't currently report a `battery` attribute — the hub only pushes battery data in response to an explicit query, not proactively.

## Shade state flow

Two paths update Indigo device state, both converging on `update_shade_state(dev, shade)`:

- **Push:** `aiopulse2` calls `_shade_update_callback(shade)` whenever the hub reports a change. This is subscribed per-shade in `_register_shade` (on discovery) and unsubscribed in `_disconnect_hub`/`deviceDeleted`.
- **Pull:** `poll_hub()` (once, right after a hub connects) and `poll_shade_status()` (on `RequestStatus` action) read current shade attributes directly.

`update_shade_state` reads `closed_percent`, `battery`, `battery_percent`, `signal`, `moving`, and `action` off the `aiopulse2` shade object (all via `getattr(..., default)` since the library doesn't guarantee every attribute is populated yet), maps them onto the Indigo state list, and calls `validate_states()` afterward to log a warning/error if a critical state (`BatteryVolts`, `BlindSignalStrength`, `BlindPosition`) is missing or empty.

Position is inverted: `aiopulse2` reports `closed_percent` (0 = fully open), Indigo's dimmer `brightnessLevel`/`BlindPosition` use "how open" (0 = fully closed) — the `100 - closed_percent` conversion happens at every read/write boundary (`update_shade_state`, `actionControlDimmerRelay`).

`self.last_states[shade_id]` tracks the last-seen `closed_percent` purely so `_shade_update_callback` can decide whether to emit the info-level "Shade X moved to Y%" log line (state is refreshed on *every* callback regardless, since battery-only pushes carry no position change).

## Device registry dicts

- `self.hubs`: `{hub_dev_id: aiopulse2.Hub}` — the live hub connections.
- `self.shades`: `{shade_id (hub-assigned): indigo_dev_id}` — maps the hub's own shade identifier to the Indigo device wrapping it. Populated both by `_register_shade` (discovery) and `deviceStartComm` (existing device restart), kept in sync by `deviceDeleted`.
- `self.last_states`: `{shade_id: last closed_percent}`.
- `self.heartbeat_tasks` / `self.battery_refresh_tasks`: `{hub_dev_id: asyncio.Task}`.

## Debug logging

`self.debug` / `self.indigo_log_handler` level is set from the `showDebugInfo` plugin preference (`PluginConfig.xml`) in `__init__` and re-applied live in `closedPrefsConfigUi` — no restart needed to toggle it. Debug-level calls are dense throughout (hub lifecycle, shade discovery/callback processing, battery/heartbeat polling) since this plugin's failure modes are almost always connectivity-related and hard to reproduce on demand.
