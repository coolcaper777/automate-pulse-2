import indigo
indigo.server.log("Plugin file loaded - imports starting...")
import asyncio
import logging
from aiopulse2 import Hub
from threading import Thread
indigo.server.log("Imports completed successfully!")

class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.server.log("Entering __init__...")
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = self.pluginPrefs.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Initializing Automate Pulse 2 Plugin... ID: {pluginId}, Version: {pluginVersion}")
        self.logger.info(f"Indigo API Version detected: {indigo.server.apiVersion}")
        try:
            api_version = float(indigo.server.apiVersion)
        except (TypeError, ValueError):
            api_version = 0.0
        if api_version <= 3:
            self.logger.error(f"API version too old! Requires greater than 3.0, got {indigo.server.apiVersion}")
        self.hubs = {}
        self.shades = {}
        self.last_states = {}
        self.battery_refresh_tasks = {}  # Track battery refresh tasks
        self.heartbeat_tasks = {}  # Track hub heartbeat tasks
        try:
            self.loop = asyncio.new_event_loop()
            self.thread = Thread(target=self.run_loop, daemon=True)
            self.thread.start()
            self.logger.info("Async loop thread started successfully")
        except Exception:
            self.logger.exception("Failed to start async loop")
        indigo.server.log("Exiting __init__...")

    def run_loop(self):
        self.logger.info("Starting async event loop...")
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def startup(self):
        self.logger.info("Automate Pulse 2 Plugin starting up...")

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.debug = valuesDict.get("showDebugInfo", False)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.logger.info(f"Debug logging {'enabled' if self.debug else 'disabled'}")

    def shutdown(self):
        self.logger.info("Automate Pulse 2 Plugin shutting down...")
        # Cancel all battery refresh and heartbeat tasks
        for task in self.battery_refresh_tasks.values():
            if not task.done():
                task.cancel()
        self.battery_refresh_tasks.clear()
        for task in self.heartbeat_tasks.values():
            if not task.done():
                task.cancel()
        self.heartbeat_tasks.clear()
        # Stop all hub connections cleanly before killing the loop
        stop_futures = [asyncio.run_coroutine_threadsafe(hub.stop(), self.loop) for hub in self.hubs.values()]
        for future in stop_futures:
            try:
                future.result(timeout=5)
            except Exception:
                self.logger.exception("Error stopping hub during shutdown")
        self.hubs.clear()
        self.loop.call_soon_threadsafe(self.loop.stop)

    def deviceStartComm(self, dev):
        super().deviceStartComm(dev)
        self.logger.info(f"deviceStartComm called for device: {dev.name} (type: {dev.deviceTypeId})")
        # Refresh the state list in case Devices.xml gained new states since this
        # device was created (new devices pick them up automatically; existing
        # ones need this nudge).
        dev.stateListOrDisplayStateIdChanged()
        if dev.deviceTypeId == "pulseHub":
            hub_ip = dev.pluginProps.get("hubIP", "")
            self.logger.debug(f"Hub IP from device props: {hub_ip}")
            if hub_ip:
                dev.updateStateOnServer("hubStatus", "Connecting...")
                asyncio.run_coroutine_threadsafe(self.connect_to_hub(dev.id, hub_ip), self.loop)
            else:
                self.logger.error(f"No hub IP specified for {dev.name}!")
        elif dev.deviceTypeId == "shadeDevice":
            shade_id = dev.pluginProps.get("shadeID", "")
            self.shades[shade_id] = dev.id
            self.logger.info(f"Shade device started: {shade_id}")
            self.logger.debug(f"Initial states for {dev.name}: {dev.states}")
            asyncio.run_coroutine_threadsafe(self.poll_shade_status(dev), self.loop)

    def deviceStopComm(self, dev):
        super().deviceStopComm(dev)
        self.logger.info(f"deviceStopComm called for device: {dev.name} (type: {dev.deviceTypeId})")
        if dev.deviceTypeId == "pulseHub":
            self._disconnect_hub(dev)

    def deviceDeleted(self, dev):
        self.logger.debug(f"deviceDeleted called for device: {dev.name} (type: {dev.deviceTypeId})")
        if dev.deviceTypeId == "shadeDevice":
            shade_id = dev.pluginProps.get("shadeID", "")
            if shade_id and self.shades.get(shade_id) == dev.id:
                del self.shades[shade_id]
                self.last_states.pop(shade_id, None)
                for hub in self.hubs.values():
                    shade = hub.rollers.get(shade_id)
                    if shade:
                        shade.callback_unsubscribe(self._shade_update_callback)
                        break
                self.logger.info(f"Cleaned up tracking for deleted shade {dev.name} (ID: {shade_id})")

    def _disconnect_hub(self, dev):
        """Tear down an active hub connection (websocket + background tasks), if any."""
        if dev.id not in self.hubs:
            return
        hub = self.hubs[dev.id]
        for shade in hub.rollers.values():
            shade.callback_unsubscribe(self._shade_update_callback)
            self.logger.debug(f"Unsubscribed callback for shade {shade.id}")
        # Cancel the battery refresh task for this hub
        if dev.id in self.battery_refresh_tasks:
            task = self.battery_refresh_tasks[dev.id]
            if not task.done():
                task.cancel()
                self.logger.debug(f"Cancelled battery refresh task for hub {dev.id}")
            del self.battery_refresh_tasks[dev.id]
        # Cancel the heartbeat task for this hub (avoids a duplicate lingering
        # alongside the fresh one connect_to_hub() starts on reconnect)
        if dev.id in self.heartbeat_tasks:
            task = self.heartbeat_tasks[dev.id]
            if not task.done():
                task.cancel()
                self.logger.debug(f"Cancelled heartbeat task for hub {dev.id}")
            del self.heartbeat_tasks[dev.id]
        # Stop the hub's websocket connection/background task
        asyncio.run_coroutine_threadsafe(hub.stop(), self.loop)
        self.logger.info(f"Stopping hub connection for {dev.name}")
        del self.hubs[dev.id]
        dev.updateStateOnServer("hubStatus", "Disconnected")
        self.logger.debug(f"Stopped hub tracking for {dev.name}")

    def reconnectHub(self, action, dev):
        """Device action: force-disconnect and reconnect a hub (e.g. after network issues)."""
        hub_ip = dev.pluginProps.get("hubIP", "")
        if not hub_ip:
            self.logger.error(f"Cannot reconnect hub {dev.name}: no hub IP specified")
            return
        self.logger.info(f"Reconnecting hub {dev.name}...")
        self._disconnect_hub(dev)
        dev.updateStateOnServer("hubStatus", "Connecting...")
        asyncio.run_coroutine_threadsafe(self.connect_to_hub(dev.id, hub_ip), self.loop)

    def deviceUpdated(self, origDev, newDev):
        super().deviceUpdated(origDev, newDev)
        if newDev.pluginId != self.pluginId or newDev.deviceTypeId != "pulseHub":
            return
        old_ip = origDev.pluginProps.get("hubIP", "")
        new_ip = newDev.pluginProps.get("hubIP", "")
        if old_ip != new_ip and newDev.id in self.hubs:
            self.logger.info(f"Hub IP changed for {newDev.name} ({old_ip} -> {new_ip}), reconnecting...")
            self._disconnect_hub(newDev)
            if new_ip:
                newDev.updateStateOnServer("hubStatus", "Connecting...")
                asyncio.run_coroutine_threadsafe(self.connect_to_hub(newDev.id, new_ip), self.loop)

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors_dict = indigo.Dict()
        if typeId == "pulseHub":
            hub_ip = valuesDict.get("hubIP", "").strip()
            if not hub_ip:
                errors_dict["hubIP"] = "Hub IP address is required."
            elif " " in hub_ip:
                errors_dict["hubIP"] = "Hub IP address must not contain spaces."
        elif typeId == "shadeDevice":
            shade_id = valuesDict.get("shadeID", "").strip()
            if not shade_id:
                errors_dict["shadeID"] = "Shade ID is required."
            default_brightness = valuesDict.get("defaultBrightness", "").strip()
            if default_brightness:
                try:
                    brightness = int(default_brightness)
                    if not (1 <= brightness <= 100):
                        errors_dict["defaultBrightness"] = "Default brightness must be between 1 and 100."
                except ValueError:
                    errors_dict["defaultBrightness"] = "Default brightness must be a whole number between 1 and 100."
        if errors_dict:
            return (False, valuesDict, errors_dict)
        return (True, valuesDict)

    def validate_states(self, dev):
        """Validate and log the current states of the device."""
        states = dev.states
        self.logger.debug(f"Validated states for {dev.name}: {states}")
        if "BatteryVolts" not in states:
            self.logger.error(f"BatteryVolts state missing for {dev.name}")
        elif not states["BatteryVolts"]:
            self.logger.warning(f"BatteryVolts state is empty for {dev.name}")
        if "BlindSignalStrength" not in states:
            self.logger.error(f"BlindSignalStrength state missing for {dev.name}")
        elif states["BlindSignalStrength"] is None:
            self.logger.warning(f"BlindSignalStrength state is empty for {dev.name}")
        if "BlindPosition" not in states:
            self.logger.error(f"BlindPosition state missing for {dev.name}")
        elif states["BlindPosition"] is None:
            self.logger.warning(f"BlindPosition state is empty for {dev.name}")

    async def connect_to_hub(self, dev_id, hub_ip):
        try:
            self.logger.info(f"Attempting to initialize hub at {hub_ip}...")
            hub = Hub(hub_ip)
            self.hubs[dev_id] = hub
            self.logger.debug(f"Hub object created for {hub_ip}, starting hub in background...")
            asyncio.create_task(hub.run())
            self.logger.debug(f"Hub task started at {hub_ip}, waiting for shade list to sync...")
            try:
                await asyncio.wait_for(hub.rollers_known.wait(), timeout=15)
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"Hub at {hub_ip} did not report a complete shade list within 15s - "
                    "proceeding with whatever was discovered so far"
                )
            await self.discover_shades(dev_id)
            asyncio.create_task(self.poll_hub(dev_id))
            self.heartbeat_tasks[dev_id] = asyncio.create_task(self.hub_heartbeat(dev_id))
            # Start the battery refresh task and track it
            task = asyncio.create_task(self.battery_refresh(dev_id))
            self.battery_refresh_tasks[dev_id] = task
            self.logger.debug(f"Started battery refresh task for hub {dev_id}")
            await self.request_battery_info(hub)
        except Exception:
            self.logger.exception(f"Failed to initialize hub at {hub_ip}")

    async def request_battery_info(self, hub):
        """Force a shadow query to get battery info."""
        for roller_id, shade in hub.rollers.items():
            battery = getattr(shade, 'battery', None)
            if battery is None:
                self.logger.debug(f"Requesting battery info for roller {roller_id}")
                hub.payload_queue.append({
                    "method": "shadow",
                    "args": {
                        "desired": {"shades": {roller_id: {"query": True}}},
                        "timeStamp": indigo.server.getTime().timestamp()
                    }
                })

    async def battery_refresh(self, hub_dev_id):
        """Periodically refresh battery info."""
        hub = self.hubs.get(hub_dev_id)
        if not hub:
            self.logger.warning(f"Battery refresh skipped for {hub_dev_id} - hub not found")
            return
        try:
            while hub_dev_id in self.hubs:
                await asyncio.sleep(300)  # Every 5 minutes
                hub = self.hubs.get(hub_dev_id)
                if hub:
                    self.logger.debug(f"Refreshing battery info for hub {hub_dev_id}")
                    await self.request_battery_info(hub)
                else:
                    self.logger.warning(f"Battery refresh stopped for {hub_dev_id} - hub removed")
                    break
        except asyncio.CancelledError:
            self.logger.debug(f"Battery refresh task for hub {hub_dev_id} was cancelled")
            raise

    def _set_hub_status(self, hub_dev_id, connected):
        """Reflect hub connectivity and metadata onto the hub device's states and Indigo's error indicator."""
        if hub_dev_id not in indigo.devices:
            return
        dev = indigo.devices[hub_dev_id]
        if connected:
            states = [{"key": "hubStatus", "value": "Connected"}]
            hub = self.hubs.get(hub_dev_id)
            if hub and hub.model:
                states.append({"key": "hubModel", "value": hub.model})
            if hub and hub.firmware_ver:
                states.append({"key": "hubFirmware", "value": str(hub.firmware_ver)})
            if hub and hub.mac_address:
                states.append({"key": "hubMAC", "value": hub.mac_address})
            dev.updateStatesOnServer(states)
            dev.setErrorStateOnServer(None)
        else:
            dev.updateStateOnServer("hubStatus", "Disconnected")
            dev.setErrorStateOnServer("Unresponsive")

    async def hub_heartbeat(self, hub_dev_id):
        """Periodic check to ensure hub connectivity using hub.connected status.

        Auto-reconnects after repeated consecutive failures so a dropped
        connection recovers without requiring the user to notice and run
        the "Reconnect Hub" action manually. Also mirrors connectivity onto
        the hub device's `hubStatus` state and Indigo's error indicator.
        """
        max_consecutive_failures = 3  # ~90 seconds unresponsive before auto-reconnecting
        hub = self.hubs.get(hub_dev_id)
        if not hub:
            self.logger.warning(f"Hub heartbeat skipped for {hub_dev_id} - hub not found")
            return
        consecutive_failures = 0
        if hub.connected:
            self.logger.debug(f"Hub heartbeat for {hub_dev_id} - connected on startup")
            self._set_hub_status(hub_dev_id, connected=True)
        else:
            self.logger.warning(f"Hub at {hub_dev_id} unresponsive on startup")
            consecutive_failures += 1
            self._set_hub_status(hub_dev_id, connected=False)
        while hub_dev_id in self.hubs:
            await asyncio.sleep(30)  # 30 seconds
            hub = self.hubs.get(hub_dev_id)
            if not hub:
                self.logger.warning(f"Hub heartbeat stopped for {hub_dev_id} - hub removed")
                break
            if hub.connected:
                if consecutive_failures:
                    self.logger.info(f"Hub heartbeat for {hub_dev_id} - connection recovered")
                consecutive_failures = 0
                self.logger.debug(f"Hub heartbeat for {hub_dev_id} - connected")
                self._set_hub_status(hub_dev_id, connected=True)
                continue
            consecutive_failures += 1
            self.logger.warning(
                f"Hub at {hub_dev_id} unresponsive ({consecutive_failures}/{max_consecutive_failures})"
            )
            self._set_hub_status(hub_dev_id, connected=False)
            if consecutive_failures >= max_consecutive_failures:
                if hub_dev_id in indigo.devices:
                    dev = indigo.devices[hub_dev_id]
                    self.logger.warning(
                        f"Hub {dev.name} unresponsive for {max_consecutive_failures * 30}s - auto-reconnecting..."
                    )
                    self.reconnectHub(None, dev)
                else:
                    self.logger.error(f"Cannot auto-reconnect hub {hub_dev_id}: device not found")
                # connect_to_hub() starts a fresh heartbeat task for this hub, so stop this one.
                break

    def _should_log_changes(self, dev):
        """Whether this shade's per-device 'log changes' checkbox is enabled (defaults on)."""
        return str(dev.pluginProps.get("logChanges", True)).lower() == "true"

    def _shade_update_callback(self, shade):
        shade_id = shade.id
        dev_id = self.shades.get(shade_id)
        if dev_id is not None and dev_id in indigo.devices:
            dev = indigo.devices[dev_id]
            self.logger.debug(f"Processing callback for shade {shade_id} ({dev.name})")
            closed_percent = getattr(shade, 'closed_percent', 0)
            last_state = self.last_states.get(shade_id, None)
            position_changed = last_state != closed_percent
            # Always refresh state (battery/signal/position) on every callback,
            # even if position itself didn't change (e.g. battery-only updates)
            self.update_shade_state(dev, shade)
            if position_changed and self._should_log_changes(dev):
                self.logger.info(f"Shade {dev.name} moved to {100 - closed_percent}%")
            self.last_states[shade_id] = closed_percent

    async def discover_shades(self, hub_dev_id):
        hub = self.hubs.get(hub_dev_id)
        if hub:
            try:
                self.logger.debug("Checking for rollers...")
                if not hub.rollers:
                    self.logger.warning("No rollers found after delay - retrying in 5 seconds...")
                    await asyncio.sleep(5)
                if hub.rollers:
                    for shade in hub.rollers.values():
                        self._register_shade(shade)
                else:
                    self.logger.error("Still no rollers - hub may not be responding")
            except Exception:
                self.logger.exception("Error discovering shades")

    def _register_shade(self, shade):
        """Create the Indigo device for a newly-seen shade (if needed) and subscribe to its updates."""
        shade_id = shade.id
        if shade_id not in self.shades:
            new_dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId="shadeDevice",
                name=f"Shade {shade.name}",
                pluginId=self.pluginId,
                props={"shadeID": shade_id}
            )
            # Initialize all states with correct types
            new_dev.updateStatesOnServer([
                {"key": "brightnessLevel", "value": 0},
                {"key": "onOffState", "value": False},
                {"key": "BatteryVolts", "value": "0.0 V"},
                {"key": "batteryLevel", "value": 0},
                {"key": "BatteryPercent", "value": 0},
                {"key": "BlindSignalStrength", "value": 0},
                {"key": "BlindPosition", "value": 0},
                {"key": "shadeMoving", "value": False},
                {"key": "shadeAction", "value": "Stopped"},
            ])
            self.shades[shade_id] = new_dev.id
            self.logger.debug(f"Initialized states for {new_dev.name}: {new_dev.states}")
        shade.callback_subscribe(self._shade_update_callback)
        self.logger.info(f"Found shade: {shade.name} (ID: {shade_id})")
        self.logger.debug(f"Subscribed to updates for shade {shade_id}")

    async def poll_hub(self, hub_dev_id):
        hub = self.hubs.get(hub_dev_id)
        if hub and hub_dev_id in self.hubs:
            try:
                self.logger.debug("Polling hub once to set initial states...")
                if hub.rollers:
                    for shade in hub.rollers.values():
                        dev_id = self.shades.get(shade.id)
                        if dev_id is not None and dev_id in indigo.devices:
                            dev = indigo.devices[dev_id]
                            self.update_shade_state(dev, shade)
                            self.last_states[shade.id] = getattr(shade, 'closed_percent', 0)
            except Exception:
                self.logger.exception("Polling error")
        else:
            self.logger.debug("Hub not found, skipping poll")

    async def poll_shade_status(self, dev):
        shade_id = dev.pluginProps.get("shadeID", "")
        hub = next((hub for hub in self.hubs.values() if shade_id in hub.rollers), None)
        if hub:
            shade = hub.rollers.get(shade_id)
            if shade:
                self.logger.debug(f"Polling shade status for {dev.name} (ID: {shade_id})")
                self.update_shade_state(dev, shade)
                self.last_states[shade_id] = getattr(shade, 'closed_percent', 0)

    _ACTION_LABELS = {"up": "Opening", "down": "Closing", "stopped": "Stopped"}

    def update_shade_state(self, dev, shade):
        closed_percent = getattr(shade, 'closed_percent', 0)
        position = 100 - closed_percent
        battery = getattr(shade, 'battery', None)
        battery_percent = getattr(shade, 'battery_percent', None)
        signal = getattr(shade, 'signal', None)
        moving = bool(getattr(shade, 'moving', False))
        action = getattr(shade, 'action', None)
        action_label = self._ACTION_LABELS.get(getattr(action, "name", None), "Stopped")
        self.logger.debug(
            f"Updating state for {dev.name}: position={position}%, battery={battery} ({battery_percent}%), "
            f"signal={signal}, moving={moving} ({action_label})"
        )
        if battery is None:
            self.logger.debug(f"No battery info for {shade.id} - shade attrs: {shade.__dict__}")
        if signal is None:
            self.logger.debug(f"No signal info for {shade.id} - shade attrs: {shade.__dict__}")
        dev.updateStatesOnServer([
            {"key": "brightnessLevel", "value": position},
            {"key": "onOffState", "value": position > 0},
            {"key": "BlindPosition", "value": position},
            {"key": "BatteryVolts", "value": f"{battery:.1f} V" if battery is not None else "0.0 V"},
            {"key": "batteryLevel", "value": battery_percent if battery_percent is not None else 0},
            {"key": "BatteryPercent", "value": battery_percent if battery_percent is not None else 0},
            {"key": "BlindSignalStrength", "value": signal if signal is not None else 0},
            {"key": "shadeMoving", "value": moving},
            {"key": "shadeAction", "value": action_label},
        ])
        self.validate_states(dev)

    def actionControlDimmerRelay(self, action, dev):
        self.logger.debug(f"actionControlDimmerRelay called: {action.deviceAction}, value: {action.actionValue}")
        if dev.deviceTypeId == "shadeDevice":
            shade_id = dev.pluginProps.get("shadeID", "")
            hub = next((hub for hub in self.hubs.values() if shade_id in hub.rollers), None)
            if hub:
                shade = hub.rollers.get(shade_id)
                if shade:
                    if action.deviceAction == indigo.kDeviceAction.TurnOn:
                        default_brightness = dev.pluginProps.get("defaultBrightness", None)
                        if default_brightness:
                            try:
                                brightness = int(default_brightness)
                                if 1 <= brightness <= 100:
                                    target_closed_percent = 100 - brightness
                                    asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, target=target_closed_percent), self.loop)
                                else:
                                    self.logger.error(f"Default brightness {default_brightness} out of range (1-100)")
                                    asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, fully_open=True), self.loop)
                            except ValueError:
                                self.logger.error(f"Invalid default brightness value: {default_brightness}")
                                asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, fully_open=True), self.loop)
                        else:
                            asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, fully_open=True), self.loop)
                    elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                        asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, fully_close=True), self.loop)
                    elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
                        target_closed_percent = 100 - action.actionValue
                        asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, target=target_closed_percent), self.loop)
                    elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
                        current_brightness = dev.states.get("brightnessLevel", 0)
                        new_brightness = min(100, current_brightness + action.actionValue)
                        target_closed_percent = 100 - new_brightness
                        asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, target=target_closed_percent), self.loop)
                    elif action.deviceAction == indigo.kDeviceAction.DimBy:
                        current_brightness = dev.states.get("brightnessLevel", 0)
                        new_brightness = max(0, current_brightness - action.actionValue)
                        target_closed_percent = 100 - new_brightness
                        asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, target=target_closed_percent), self.loop)
                    elif action.deviceAction == indigo.kDeviceAction.RequestStatus:
                        asyncio.run_coroutine_threadsafe(self.poll_shade_status(dev), self.loop)
                    if self._should_log_changes(dev):
                        self.logger.info(f"Shade {dev.name}: {action.deviceAction}")
                else:
                    self.logger.error(f"No shade found with ID {shade_id} in hub")
            else:
                self.logger.error(f"No hub found for shade {shade_id}")

    def stopShade(self, action, dev):
        """Device action: stop a shade mid-travel."""
        shade_id = dev.pluginProps.get("shadeID", "")
        hub = next((hub for hub in self.hubs.values() if shade_id in hub.rollers), None)
        if hub:
            shade = hub.rollers.get(shade_id)
            if shade:
                asyncio.run_coroutine_threadsafe(self.move_shade(shade, dev, stop=True), self.loop)
                if self._should_log_changes(dev):
                    self.logger.info(f"Shade {dev.name}: Stop")
            else:
                self.logger.error(f"No shade found with ID {shade_id} in hub")
        else:
            self.logger.error(f"No hub found for shade {shade_id}")

    async def move_shade(self, shade, dev, fully_open=False, fully_close=False, target=None, stop=False):
        try:
            if stop:
                await shade.move_stop()
                self.logger.debug(f"Stop command sent to shade {dev.name}")
                return
            if fully_open:
                await shade.move_up()
                target = 0
            elif fully_close:
                await shade.move_down()
                target = 0
            elif target is not None:
                await shade.move_to(target)
            self.logger.debug(f"Command sent to shade {dev.name}: target closed_percent={target}")
        except Exception:
            self.logger.exception(f"Error moving shade {dev.name}")