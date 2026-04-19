import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import time

from mobileraker.mobileraker_companion import MobilerakerCompanion
from mobileraker.client.mobileraker_fcm_client import MobilerakerFcmClient
from mobileraker.client.moonraker_client import MoonrakerClient
from mobileraker.client.webcam_snapshot_client import WebcamSnapshotClient
from mobileraker.data.dtos.mobileraker.companion_request_dto import NotificationContentDto, ProgressNotificationContentDto, LiveActivityContentDto
from mobileraker.data.dtos.mobileraker.notification_config_dto import DeviceNotificationEntry, NotificationSettings, NotificationSnap, APNs
from mobileraker.data.dtos.moonraker.printer_objects import FilamentSensor
from mobileraker.data.dtos.moonraker.printer_snapshot import PrinterSnapshot
from mobileraker.service.data_sync_service import DataSyncService
from mobileraker.util.configs import CompanionLocalConfig


class TestMobilerakerCompanionIntegration(unittest.TestCase):
    
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        # Create mock dependencies
        self.mock_jrpc = AsyncMock(spec=MoonrakerClient)
        self.mock_data_sync = AsyncMock(spec=DataSyncService)
        self.mock_fcm_client = MagicMock(spec=MobilerakerFcmClient)
        self.mock_webcam_client = MagicMock(spec=WebcamSnapshotClient)
        self.mock_companion_config = MagicMock(spec=CompanionLocalConfig)
        
        # Setup companion config
        from dateutil import tz
        self.mock_companion_config.include_snapshot = True
        self.mock_companion_config.timezone_str = "UTC"
        self.mock_companion_config.timezone = tz.UTC
        self.mock_companion_config.eta_format = "%d.%m.%Y, %H:%M:%S"
        self.mock_companion_config.language = "en"
        
        # Create companion instance
        self.companion = MobilerakerCompanion(
            jrpc=self.mock_jrpc,
            data_sync_service=self.mock_data_sync,
            fcm_client=self.mock_fcm_client,
            webcam_snapshot_client=self.mock_webcam_client,
            printer_name="test_printer",
            loop=self.loop,
            companion_config=self.mock_companion_config,
            exclude_sensors=[]
        )
        
        # Mock the initial state
        self.companion._last_snapshot = None
        
    def tearDown(self):
        self.loop.close()

    def _create_test_snapshot(self, print_state="printing", progress=50, m117=None, gcode_response=None, filament_sensors=None) -> PrinterSnapshot:
        """Create a test printer snapshot."""
        snapshot = PrinterSnapshot(klippy_ready=True, print_state=print_state)
        
        # Mock virtual_sdcard for progress
        virtual_sdcard = MagicMock()
        virtual_sdcard.progress = progress / 100.0 if progress is not None else None
        snapshot.virtual_sdcard = virtual_sdcard
        
        # Mock print_stats
        print_stats = MagicMock()
        print_stats.print_duration = 3600  # 1 hour
        print_stats.filament_used = 50.0  # 50mm
        snapshot.print_stats = print_stats
        
        # Mock current_file
        current_file = MagicMock()
        current_file.filename = "test_file.gcode"
        current_file.estimated_time = 7200  # 2 hours
        current_file.filament_total = 100.0  # 100mm total
        snapshot.current_file = current_file
        
        snapshot.m117 = m117
        snapshot.m117_hash = str(hash(m117)) if m117 else ""
        snapshot.gcode_response = gcode_response
        snapshot.gcode_response_hash = str(hash(gcode_response)) if gcode_response else None
        snapshot.timelapse_pause = False
        snapshot.filament_sensors = filament_sensors or {}
        
        return snapshot

    def _create_test_device_config(self) -> DeviceNotificationEntry:
        """Create a test device configuration."""
        cfg = DeviceNotificationEntry()
        cfg.machine_id = "12345678-1234-1234-1234-123456789012"
        cfg.fcm_token = "test-token"
        cfg.machine_name = "Test Machine"
        cfg.language = "en"
        cfg.version = "2.7.2-android"
        
        # Settings
        settings = NotificationSettings()
        settings.progress_config = 25
        settings.state_config = ["printing", "paused", "complete", "error", "standby", "cancelled"]
        settings.android_progressbar = True
        settings.eta_sources = ["filament", "slicer"]
        cfg.settings = settings
        
        # Snap (previous state)
        snap = NotificationSnap()
        snap.state = "standby"
        snap.progress = 0
        snap.progress_progressbar = 0
        snap.progress_live_activity = 0
        snap.last_progress = time.time() - 600  # 10 minutes ago
        snap.last_progress_progressbar = time.time() - 600
        snap.last_progress_live_activity = time.time() - 600
        snap.m117 = ""
        snap.gcode_response = None
        snap.filament_sensors = []
        cfg.snap = snap
        
        # APNS
        apns = APNs()
        apns.liveActivity = "test-live-activity-token"
        cfg.apns = apns
        
        return cfg

    def _create_mock_device_json(self, device_cfg: DeviceNotificationEntry, snap_overrides=None):
        """Create mock JSON data for a device configuration."""
        snap = {
            "progress": 0.0,
            "progress_live_activity": 0.0,
            "progress_progressbar": 0.0,
            "state": "standby",
            "m117": "",
            "filament_sensors": []
        }
        
        if snap_overrides:
            snap.update(snap_overrides)
        
        return {
            "created": "2022-11-25T23:03:47.656260",
            "lastModified": "2022-11-26T19:46:59.083649",
            "fcmToken": device_cfg.fcm_token,
            "machineName": device_cfg.machine_name,
            "language": device_cfg.language,
            "version": device_cfg.version,
            "settings": {
                "created": "2022-11-25T23:03:47.656261",
                "lastModified": "2022-11-26T19:46:59.083595",
                "progress": 0.25,
                "states": device_cfg.settings.state_config,
                "androidProgressbar": True,
                "etaSources": ["filament", "slicer"]
            },
            "snap": snap,
            "apns": {
                "liveActivity": "test-live-activity-token"
            }
        }

    async def test_evaluate_state_change_notification(self):
        """Test that _evaluate creates proper state change notifications."""
        # Setup: mock fetching device configs
        device_cfg = self._create_test_device_config()
        mock_device_json = self._create_mock_device_json(device_cfg)
        self.mock_jrpc.send_and_receive_method.return_value = (
            {"result": {"value": {device_cfg.machine_id: mock_device_json}}}, None
        )
        
        # Mock webcam snapshot capture
        self.mock_webcam_client.capture_snapshot.return_value = b"fake_image_data"
        
        # Mock FCM push
        self.mock_fcm_client.push.return_value = MagicMock()
        
        # Create snapshot with state change (standby -> printing)
        snapshot = self._create_test_snapshot(print_state="printing", progress=25)
        
        # Execute the evaluate method
        await self.companion._evaluate(snapshot)
        
        # Verify that FCM push was called
        self.mock_fcm_client.push.assert_called_once()
        
        # Get the push request
        push_request = self.mock_fcm_client.push.call_args[0][0]
        
        # Verify device request structure
        self.assertEqual(len(push_request.device_requests), 1)
        device_request = push_request.device_requests[0]
        
        # Check that notifications include state and progress
        self.assertGreater(len(device_request.notifcations), 0)
        
        # Find state notification using isinstance() for clean type checking
        state_notification = None
        progress_notification = None
        for notif in device_request.notifcations:
            if isinstance(notif, NotificationContentDto):
                if 'statusUpdates' in notif.channel:
                    state_notification = notif
                elif 'progressUpdates' in notif.channel:
                    progress_notification = notif
        
        # Verify state notification was created
        self.assertIsNotNone(state_notification)
        self.assertEqual(state_notification.channel, f"{device_cfg.machine_id}-statusUpdates")
        
        # Verify progress notification was created
        self.assertIsNotNone(progress_notification)
        
        # Verify image was added to notifications
        if hasattr(state_notification, 'image'):
            self.assertIsNotNone(state_notification.image)

    async def test_evaluate_no_notifications_when_no_changes(self):
        """Test that _evaluate doesn't send notifications when there are no significant changes."""
        # Setup: mock fetching device configs
        device_cfg = self._create_test_device_config()
        device_cfg.snap.state = "printing"  # Same as snapshot state
        device_cfg.snap.progress = 50      # Same as snapshot progress
        device_cfg.snap.progress_live_activity = 50  # Same as live activity progress
        device_cfg.snap.progress_progressbar = 50    # Same as progressbar progress
        device_cfg.apns = None  # Disable live activity to avoid updates
        
        mock_device_json = self._create_mock_device_json(device_cfg, {
            "progress": 0.5,  # 50%
            "progress_live_activity": 0.5,
            "progress_progressbar": 0.5,
            "state": "printing",
        })
        # Remove apns from mock JSON since we disabled it
        del mock_device_json["apns"]
        
        self.mock_jrpc.send_and_receive_method.return_value = (
            {"result": {"value": {device_cfg.machine_id: mock_device_json}}}, None
        )
        
        # Create snapshot with same state and progress
        snapshot = self._create_test_snapshot(print_state="printing", progress=50)
        
        # Set up companion to have a previous snapshot with identical values
        self.companion._last_snapshot = self._create_test_snapshot(print_state="printing", progress=50)
        
        # Execute the evaluate method
        await self.companion._evaluate(snapshot)
        
        # Verify that FCM push was NOT called (no significant changes)
        self.mock_fcm_client.push.assert_not_called()

    async def test_evaluate_filament_sensor_notification(self):
        """Test that _evaluate creates filament sensor notifications."""
        # Setup: mock fetching device configs
        device_cfg = self._create_test_device_config()
        mock_device_json = self._create_mock_device_json(device_cfg, {
            "state": "printing",
            "filament_sensors": []  # No previous sensor triggers
        })
        self.mock_jrpc.send_and_receive_method.return_value = (
            {"result": {"value": {device_cfg.machine_id: mock_device_json}}}, None
        )
        
        # Mock webcam snapshot capture
        self.mock_webcam_client.capture_snapshot.return_value = b"fake_image_data"
        
        # Mock FCM push
        self.mock_fcm_client.push.return_value = MagicMock()
        
        # Create snapshot with filament sensor trigger
        sensor = FilamentSensor(name="extruder", kind="switch", enabled=True, filament_detected=False)
        filament_sensors = {"sensor1": sensor}
        snapshot = self._create_test_snapshot(
            print_state="printing", 
            progress=50, 
            filament_sensors=filament_sensors
        )
        
        # Execute the evaluate method
        await self.companion._evaluate(snapshot)
        
        # Verify that FCM push was called
        self.mock_fcm_client.push.assert_called_once()
        
        # Get the push request and find filament sensor notification
        push_request = self.mock_fcm_client.push.call_args[0][0]
        device_request = push_request.device_requests[0]
        
        filament_notification = None
        for notif in device_request.notifcations:
            if hasattr(notif, 'channel') and 'filamentSensor' in notif.channel:
                filament_notification = notif
                break
        
        # Verify filament sensor notification was created
        self.assertIsNotNone(filament_notification)
        self.assertEqual(filament_notification.channel, f"{device_cfg.machine_id}-filamentSensor")

    async def test_evaluate_custom_m117_notification(self):
        """Test that _evaluate creates custom M117 notifications."""
        # Setup: mock fetching device configs
        device_cfg = self._create_test_device_config()
        mock_device_json = self._create_mock_device_json(device_cfg, {
            "state": "printing",
            "m117": "",  # Different from snapshot
            "filament_sensors": []
        })
        self.mock_jrpc.send_and_receive_method.return_value = (
            {"result": {"value": {device_cfg.machine_id: mock_device_json}}}, None
        )
        
        # Mock webcam snapshot capture
        self.mock_webcam_client.capture_snapshot.return_value = b"fake_image_data"
        
        # Mock FCM push
        self.mock_fcm_client.push.return_value = MagicMock()
        
        # Create snapshot with custom M117 message
        snapshot = self._create_test_snapshot(
            print_state="printing",
            progress=50,
            m117="$MR$:Custom Title|Custom Message"
        )
        
        # Execute the evaluate method
        await self.companion._evaluate(snapshot)
        
        # Verify that FCM push was called
        self.mock_fcm_client.push.assert_called_once()
        
        # Get the push request and find M117 notification
        push_request = self.mock_fcm_client.push.call_args[0][0]
        device_request = push_request.device_requests[0]
        
        m117_notification = None
        for notif in device_request.notifcations:
            if hasattr(notif, 'channel') and 'm117' in notif.channel:
                m117_notification = notif
                break
        
        # Verify M117 notification was created
        self.assertIsNotNone(m117_notification)
        self.assertEqual(m117_notification.channel, f"{device_cfg.machine_id}-m117")

    async def test_evaluate_database_update_calls(self):
        """Test that _evaluate updates the database with new snapshot data."""
        # Setup: mock fetching device configs
        device_cfg = self._create_test_device_config()
        
        # Mock send_and_receive_method for different calls
        async def mock_send_and_receive(method, params=None):
            if method == "server.database.get_item" and params.get("key") == "fcm":
                mock_device_json = self._create_mock_device_json(device_cfg, {
                    "state": "standby",
                    "m117": "",
                    "filament_sensors": []
                })
                return ({"result": {"value": {device_cfg.machine_id: mock_device_json}}}, None)
            elif method == "server.database.post_item" and "snap" in params.get("key", ""):
                return ({"result": "success"}, None)
            else:
                return ({"result": "unknown"}, None)
        
        self.mock_jrpc.send_and_receive_method.side_effect = mock_send_and_receive
        
        # Mock webcam snapshot capture
        self.mock_webcam_client.capture_snapshot.return_value = b"fake_image_data"
        
        # Mock FCM push
        self.mock_fcm_client.push.return_value = MagicMock()
        
        # Create snapshot with state change
        snapshot = self._create_test_snapshot(print_state="printing", progress=25)
        
        # Execute the evaluate method
        await self.companion._evaluate(snapshot)
        
        # Verify that database update was called
        update_calls = []
        for call in self.mock_jrpc.send_and_receive_method.call_args_list:
            if (len(call[0]) > 0 and call[0][0] == 'server.database.post_item' and 
                len(call[0]) > 1 and 'snap' in str(call[0][1])):
                update_calls.append(call)
        
        self.assertGreater(len(update_calls), 0, "Database should be updated with new snapshot data")

    def _create_error_snapshot(self, progress=97) -> PrinterSnapshot:
        """Create a snapshot representing a transient klippy_ready=False error state."""
        snapshot = PrinterSnapshot(klippy_ready=False, print_state="error")
        virtual_sdcard = MagicMock()
        virtual_sdcard.progress = progress / 100.0
        snapshot.virtual_sdcard = virtual_sdcard
        print_stats = MagicMock()
        print_stats.print_duration = 3600
        print_stats.filament_used = 50.0
        print_stats.state = "printing"
        snapshot.print_stats = print_stats
        current_file = MagicMock()
        current_file.filename = "test_file.gcode"
        current_file.estimated_time = 7200
        current_file.filament_total = 100.0
        snapshot.current_file = current_file
        snapshot.m117 = None
        snapshot.m117_hash = ""
        snapshot.gcode_response = None
        snapshot.gcode_response_hash = ""
        snapshot.timelapse_pause = False
        snapshot.filament_sensors = {}
        return snapshot

    async def test_transient_error_suppressed_when_printing_recovers(self):
        """
        Regression test: spurious 'Error while printing' notifications when a WebSocket
        reconnect briefly makes klippy_ready=False while the print is running fine.

        Scenario (from bug report):
          - Print running at 97% with no issues
          - User leaves LAN; WebSocket drops and reconnects every ~30s
          - Each reconnect: klippy_ready=False -> print_state="error" snapshot
          - Seconds later: klippy_ready=True -> print_state="printing" snapshot
          - Old behaviour: alternating error/printing notifications fired every cycle
          - Expected: transient error snapshot replaced by recovery snapshot during
            the debounce window -> no notification sent
        """
        evaluated_snapshots = []

        async def spy_evaluate(snapshot):
            evaluated_snapshots.append(snapshot)

        self.companion._evaluate = spy_evaluate
        self.companion._error_debounce_seconds = 0  # Skip real wait in tests

        # Simulate the transient error from klippy briefly becoming not-ready
        error_snapshot = self._create_error_snapshot(progress=97)
        # Simulate the recovery snapshot that arrives during the debounce window
        recovery_snapshot = self._create_test_snapshot(print_state="printing", progress=97)

        # Fire the error snapshot first (WebSocket reconnect, klippy not yet ready)
        self.companion._create_eval_task(error_snapshot)
        # Before the debounce expires, the recovery arrives (klippy back to ready)
        self.companion._pending_snapshot = recovery_snapshot

        # Let the event loop process the evaluation loop
        for _ in range(5):
            await asyncio.sleep(0)

        self.assertEqual(len(evaluated_snapshots), 1,
                         "Should evaluate exactly once (debounce coalesces error+recovery)")
        self.assertEqual(evaluated_snapshots[0].print_state, "printing",
                         "Should evaluate the recovery snapshot, not the transient error")

    async def test_rapid_snapshots_no_task_buildup(self):
        """
        Regression test: 50+ rapid snapshots (as seen during WebSocket reconnect storms)
        should not spawn 50+ evaluation tasks. The latest-wins pattern means only one
        evaluation loop runs at a time and intermediate snapshots are discarded.
        """
        evaluation_count = [0]

        async def counting_evaluate(snapshot):
            evaluation_count[0] += 1

        self.companion._evaluate = counting_evaluate
        self.companion._error_debounce_seconds = 0

        # Simulate the burst of 50+ snapshot events that previously caused lock timeout spam
        for i in range(50):
            snapshot = self._create_test_snapshot(print_state="printing", progress=i)
            self.companion._create_eval_task(snapshot)

        # Let the event loop drain
        for _ in range(20):
            await asyncio.sleep(0)

        self.assertLess(evaluation_count[0], 10,
                        "Latest-wins pattern should discard intermediate snapshots; "
                        "far fewer than 50 evaluations should run")
        self.assertFalse(self.companion._evaluation_running,
                         "Evaluation loop should have exited cleanly with no pending work")

    async def test_evaluate_threshold_check(self):
        """Test that _evaluate respects threshold checks before processing."""
        # Set up a previous snapshot
        self.companion._last_snapshot = self._create_test_snapshot(print_state="printing", progress=50)
        
        # Create a new snapshot with minimal change (should not trigger evaluation)
        snapshot = self._create_test_snapshot(print_state="printing", progress=51)
        
        # Execute the evaluate method
        await self.companion._evaluate(snapshot)
        
        # Verify that no database calls were made (threshold not met)
        self.mock_jrpc.send_and_receive_method.assert_not_called()
        
        # Verify that FCM push was not called
        self.mock_fcm_client.push.assert_not_called()


def async_test(coro):
    """Decorator to run async tests."""
    def wrapper(self):
        return self.loop.run_until_complete(coro(self))
    return wrapper


# Apply async decorator to all test methods
for attr_name in dir(TestMobilerakerCompanionIntegration):
    if attr_name.startswith('test_') and callable(getattr(TestMobilerakerCompanionIntegration, attr_name)):
        attr = getattr(TestMobilerakerCompanionIntegration, attr_name)
        if asyncio.iscoroutinefunction(attr):
            setattr(TestMobilerakerCompanionIntegration, attr_name, async_test(attr))


if __name__ == '__main__':
    unittest.main()