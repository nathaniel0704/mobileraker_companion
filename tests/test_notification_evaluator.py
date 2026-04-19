import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from mobileraker.data.dtos.mobileraker.companion_request_dto import NotificationContentDto, ProgressNotificationContentDto, LiveActivityContentDto
from mobileraker.data.dtos.mobileraker.notification_config_dto import DeviceNotificationEntry, NotificationSettings, NotificationSnap, APNs
from mobileraker.data.dtos.moonraker.printer_objects import FilamentSensor
from mobileraker.data.dtos.moonraker.printer_snapshot import PrinterSnapshot
from mobileraker.service.notification_evaluator import NotificationEvaluator
from mobileraker.util.configs import CompanionLocalConfig, CompanionRemoteConfig


class TestNotificationEvaluator(unittest.TestCase):
    
    def setUp(self):
        # Create mock configs for testing
        from dateutil import tz
        self.companion_config = MagicMock()
        self.companion_config.language = "en"
        self.companion_config.timezone_str = "UTC"
        self.companion_config.timezone = tz.UTC
        self.companion_config.eta_format = "%d.%m.%Y, %H:%M:%S"
        self.companion_config.include_snapshot = True
        
        self.remote_config = CompanionRemoteConfig()
        self.evaluator = NotificationEvaluator(self.companion_config, self.remote_config)
        
        # Create test device configuration
        self.device_cfg = self._create_test_device_config()
        
        # Create test printer snapshots
        self.printing_snapshot = self._create_test_snapshot(print_state="printing", progress=50)
        self.completed_snapshot = self._create_test_snapshot(print_state="complete", progress=100)
        self.paused_snapshot = self._create_test_snapshot(print_state="paused", progress=75)
        self.error_snapshot = self._create_test_snapshot(print_state="error", progress=30)

    def _create_test_device_config(self) -> DeviceNotificationEntry:
        """Create a test device configuration."""
        cfg = DeviceNotificationEntry()
        cfg.machine_id = "12345678-1234-1234-1234-123456789012"  # Valid UUID format
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
        snap.last_progress = datetime.now() - timedelta(minutes=10)
        snap.last_progress_progressbar = datetime.now() - timedelta(minutes=10)
        snap.last_progress_live_activity = datetime.now() - timedelta(minutes=10)
        snap.m117 = ""
        snap.gcode_response = None
        snap.filament_sensors = []
        cfg.snap = snap
        
        # APNS
        apns = APNs()
        apns.liveActivity = "test-live-activity-token"
        cfg.apns = apns
        
        return cfg

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

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    def test_evaluate_state_notification_printing_to_complete(self, mock_translate):
        """Test state notification when transitioning from printing to complete."""
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        
        # Set previous state to printing
        self.device_cfg.snap.state = "printing"
        
        result = self.evaluator.evaluate_state_notification(self.device_cfg, self.completed_snapshot)
        
        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.channel, "12345678-1234-1234-1234-123456789012-statusUpdates")
        self.assertEqual(result.title, "translated_state_title")
        self.assertEqual(result.body, "translated_state_completed_body")

    def test_evaluate_state_notification_no_change(self):
        """Test state notification when state hasn't changed."""
        # Set previous state to same as current
        self.device_cfg.snap.state = "printing"
        
        result = self.evaluator.evaluate_state_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsNone(result)

    def test_evaluate_state_notification_state_not_configured(self):
        """Test state notification when state is not in user configuration."""
        # Remove 'complete' from state config
        self.device_cfg.settings.state_config = ["printing", "paused"]
        
        result = self.evaluator.evaluate_state_notification(self.device_cfg, self.completed_snapshot)
        
        self.assertIsNone(result)

    def test_evaluate_state_notification_timelapse_pause(self):
        """Test state notification ignores timelapse pauses."""
        timelapse_snapshot = self._create_test_snapshot(print_state="paused")
        timelapse_snapshot.timelapse_pause = True
        
        result = self.evaluator.evaluate_state_notification(self.device_cfg, timelapse_snapshot)
        
        self.assertIsNone(result)

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    @patch('mobileraker.service.notification_evaluator.normalized_progress_interval_reached')
    def test_evaluate_progress_notification_threshold_reached(self, mock_interval_reached, mock_translate):
        """Test progress notification when threshold is reached."""
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        mock_interval_reached.return_value = True
        
        # Set previous state to printing with lower progress
        self.device_cfg.snap.state = "printing"
        self.device_cfg.snap.progress = 25
        
        result = self.evaluator.evaluate_progress_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.channel, "12345678-1234-1234-1234-123456789012-progressUpdates")
        mock_interval_reached.assert_called_once()

    def test_evaluate_progress_notification_disabled(self):
        """Test progress notification when disabled."""
        self.device_cfg.settings.progress_config = -1
        
        result = self.evaluator.evaluate_progress_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsNone(result)

    def test_evaluate_progress_notification_wrong_state(self):
        """Test progress notification in wrong printer state."""
        result = self.evaluator.evaluate_progress_notification(self.device_cfg, self.completed_snapshot)
        
        self.assertIsNone(result)

    def test_evaluate_progress_notification_100_percent(self):
        """Test progress notification at 100% (should be skipped)."""
        snapshot_100 = self._create_test_snapshot(print_state="printing", progress=100)
        
        result = self.evaluator.evaluate_progress_notification(self.device_cfg, snapshot_100)
        
        self.assertIsNone(result)

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    @patch('mobileraker.service.notification_evaluator.normalized_progress_interval_reached')
    def test_evaluate_progressbar_notification_android(self, mock_interval_reached, mock_translate):
        """Test progressbar notification for Android device."""
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        mock_interval_reached.return_value = True
        
        # Set previous state to printing with lower progress
        self.device_cfg.snap.state = "printing"
        self.device_cfg.snap.progress_progressbar = 25
        
        result = self.evaluator.evaluate_progressbar_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsInstance(result, ProgressNotificationContentDto)
        self.assertEqual(result.progress, 50)
        # Check that the channel is correct - should be progressBarUpdates for version 2.7.2+
        self.assertEqual(result.channel, "12345678-1234-1234-1234-123456789012-progressBarUpdates")

    def test_evaluate_progressbar_notification_ios_device(self):
        """Test progressbar notification for iOS device (should be skipped)."""
        self.device_cfg.version = "2.6.10-ios"
        
        result = self.evaluator.evaluate_progressbar_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsNone(result)

    def test_evaluate_progressbar_notification_disabled(self):
        """Test progressbar notification when disabled."""
        self.device_cfg.settings.android_progressbar = False
        
        result = self.evaluator.evaluate_progressbar_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsNone(result)

    def test_evaluate_progressbar_notification_old_version(self):
        """Test progressbar notification with version below 2.6.10."""
        self.device_cfg.version = "2.5.0-android"
        
        result = self.evaluator.evaluate_progressbar_notification(self.device_cfg, self.printing_snapshot)
        
        self.assertIsNone(result)

    @patch('mobileraker.service.notification_evaluator.normalized_progress_interval_reached')
    def test_evaluate_live_activity_update(self, mock_interval_reached):
        """Test live activity update evaluation."""
        mock_interval_reached.return_value = True
        
        # Set previous state to printing with lower progress
        self.device_cfg.snap.state = "printing"
        self.device_cfg.snap.progress_live_activity = 25
        
        last_snapshot = self._create_test_snapshot(print_state="printing", progress=25)
        
        result = self.evaluator.evaluate_live_activity_update(self.device_cfg, self.printing_snapshot, last_snapshot)
        
        self.assertIsInstance(result, LiveActivityContentDto)
        self.assertEqual(result.token, "test-live-activity-token")
        self.assertEqual(result.progress, 50)
        self.assertEqual(result.live_activity_event, "update")
        self.assertEqual(result.print_state, "printing")

    def test_evaluate_live_activity_no_apns(self):
        """Test live activity when APNS is not configured."""
        self.device_cfg.apns = None
        
        result = self.evaluator.evaluate_live_activity_update(self.device_cfg, self.printing_snapshot, None)
        
        self.assertIsNone(result)

    def test_evaluate_live_activity_no_live_activity_token(self):
        """Test live activity when live activity token is not set."""
        self.device_cfg.apns.liveActivity = ""
        
        result = self.evaluator.evaluate_live_activity_update(self.device_cfg, self.printing_snapshot, None)
        
        self.assertIsNone(result)

    def test_evaluate_live_activity_end_event(self):
        """Test live activity end event when print completes."""
        self.device_cfg.snap.state = "printing"
        self.device_cfg.snap.progress_live_activity = 25
        
        result = self.evaluator.evaluate_live_activity_update(self.device_cfg, self.completed_snapshot, None)
        
        self.assertIsInstance(result, LiveActivityContentDto)
        self.assertEqual(result.live_activity_event, "end")

    @patch('mobileraker.service.notification_evaluator.replace_placeholders')
    def test_evaluate_custom_notification_m117(self, mock_replace):
        """Test custom M117 notification."""
        mock_replace.side_effect = lambda text, *args, **kwargs: f"replaced_{text}"
        
        m117_snapshot = self._create_test_snapshot(m117="$MR$:Custom Title|Custom Body")
        
        result = self.evaluator.evaluate_custom_notification(self.device_cfg, m117_snapshot, is_m117=True)
        
        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.channel, "12345678-1234-1234-1234-123456789012-m117")
        self.assertEqual(result.title, "replaced_Custom Title")
        self.assertEqual(result.body, "replaced_Custom Body")

    @patch('mobileraker.service.notification_evaluator.replace_placeholders')
    def test_evaluate_custom_notification_gcode_response(self, mock_replace):
        """Test custom GCode response notification."""
        mock_replace.side_effect = lambda text, *args, **kwargs: f"replaced_{text}"
        
        gcode_snapshot = self._create_test_snapshot(gcode_response="MR_NOTIFY:Alert Message")
        
        result = self.evaluator.evaluate_custom_notification(self.device_cfg, gcode_snapshot, is_m117=False)
        
        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.channel, "12345678-1234-1234-1234-123456789012-m117")
        self.assertEqual(result.body, "replaced_Alert Message")

    def test_evaluate_custom_notification_no_prefix(self):
        """Test custom notification without proper prefix."""
        m117_snapshot = self._create_test_snapshot(m117="Regular M117 message")
        
        result = self.evaluator.evaluate_custom_notification(self.device_cfg, m117_snapshot, is_m117=True)
        
        self.assertIsNone(result)

    def test_evaluate_custom_notification_already_sent(self):
        """Test custom notification that was already sent."""
        m117_snapshot = self._create_test_snapshot(m117="$MR$:Test Message")
        self.device_cfg.snap.m117 = m117_snapshot.m117_hash
        
        result = self.evaluator.evaluate_custom_notification(self.device_cfg, m117_snapshot, is_m117=True)
        
        self.assertIsNone(result)

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    def test_evaluate_filament_sensor_notifications(self, mock_translate):
        """Test filament sensor notifications."""
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        
        # Create filament sensors
        sensor1 = FilamentSensor(name="extruder", kind="switch", enabled=True, filament_detected=False)
        sensor2 = FilamentSensor(name="runout", kind="switch", enabled=True, filament_detected=True)
        
        filament_snapshot = self._create_test_snapshot(
            filament_sensors={"sensor1": sensor1, "sensor2": sensor2}
        )
        
        result = self.evaluator.evaluate_filament_sensor_notifications(
            self.device_cfg, filament_snapshot, exclude_sensors=[]
        )
        
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)  # Only sensor1 should trigger
        self.assertEqual(result[0].channel, "12345678-1234-1234-1234-123456789012-filamentSensor")

    def test_evaluate_filament_sensor_notifications_excluded(self):
        """Test filament sensor notifications with excluded sensors."""
        sensor1 = FilamentSensor(name="extruder", kind="switch", enabled=True, filament_detected=False)
        
        filament_snapshot = self._create_test_snapshot(
            filament_sensors={"sensor1": sensor1}
        )
        
        result = self.evaluator.evaluate_filament_sensor_notifications(
            self.device_cfg, filament_snapshot, exclude_sensors=["sensor1"]
        )
        
        self.assertIsNone(result)

    def test_evaluate_filament_sensor_notifications_disabled_sensor(self):
        """Test filament sensor notifications with disabled sensor."""
        sensor1 = FilamentSensor(name="extruder", kind="switch", enabled=False, filament_detected=False)
        
        filament_snapshot = self._create_test_snapshot(
            filament_sensors={"sensor1": sensor1}
        )
        
        result = self.evaluator.evaluate_filament_sensor_notifications(
            self.device_cfg, filament_snapshot, exclude_sensors=[]
        )
        
        self.assertIsNone(result)

    def test_evaluate_filament_sensor_notifications_already_triggered(self):
        """Test filament sensor notifications that were already triggered."""
        sensor1 = FilamentSensor(name="extruder", kind="switch", enabled=True, filament_detected=False)
        
        filament_snapshot = self._create_test_snapshot(
            filament_sensors={"sensor1": sensor1}
        )
        
        # Mark sensor as already triggered
        self.device_cfg.snap.filament_sensors = ["sensor1"]
        
        result = self.evaluator.evaluate_filament_sensor_notifications(
            self.device_cfg, filament_snapshot, exclude_sensors=[]
        )
        
        self.assertIsNone(result)

    def test_evaluate_filament_sensor_notifications_no_sensors(self):
        """Test filament sensor notifications with no sensors."""
        empty_snapshot = self._create_test_snapshot()
        
        result = self.evaluator.evaluate_filament_sensor_notifications(
            self.device_cfg, empty_snapshot, exclude_sensors=[]
        )
        
        self.assertIsNone(result)

    def test_evaluate_all_notifications_for_device_comprehensive(self):
        """All distinct notification types fire when their conditions are simultaneously met."""
        snapshot = self._create_test_snapshot(
            print_state="printing",
            progress=75,
            m117="$MR$:Print Progress|75% Complete"
        )
        sensor_runout = FilamentSensor(name="extruder", kind="switch", enabled=True, filament_detected=False)
        snapshot.filament_sensors = {"extruder": sensor_runout}

        self.device_cfg.snap.state = "paused"      # triggers statusUpdates
        self.device_cfg.snap.progress = 50          # triggers progressUpdates (25% interval)
        self.device_cfg.snap.progress_progressbar = 50
        self.device_cfg.snap.progress_live_activity = 50

        last_snapshot = self._create_test_snapshot(print_state="paused", progress=50)

        result = self.evaluator.evaluate_all_notifications_for_device(
            self.device_cfg, snapshot, last_snapshot, exclude_sensors=[]
        )

        channels = {
            n.channel.split('-')[-1]
            for n in result.notifications
            if hasattr(n, 'channel') and n.channel
        }

        self.assertIn('statusUpdates', channels, "State change must produce a statusUpdates notification")
        self.assertIn('progressUpdates', channels, "25% progress jump must produce a progressUpdates notification")
        self.assertIn('m117', channels, "New $MR$ M117 message must produce an m117 notification")
        self.assertIn('filamentSensor', channels, "Runout sensor must produce a filamentSensor notification")
        self.assertTrue(result.has_live_activity, "Live activity flag must be set")
        self.assertTrue(result.has_progress_notification, "Progress notification flag must be set")

    # ------------------------------------------------------------------
    # State notification — missing transition directions
    # ------------------------------------------------------------------

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    def test_evaluate_state_notification_standby_to_printing(self, mock_translate):
        """standby → printing generates a 'started printing' notification."""
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        self.device_cfg.snap.state = "standby"

        result = self.evaluator.evaluate_state_notification(self.device_cfg, self.printing_snapshot)

        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.body, "translated_state_printing_body")

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    def test_evaluate_state_notification_printing_to_error(self, mock_translate):
        """printing → error generates an 'error while printing' notification.

        This is one side of the oscillation that caused the bug report's notification storm.
        """
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        self.device_cfg.snap.state = "printing"

        result = self.evaluator.evaluate_state_notification(self.device_cfg, self.error_snapshot)

        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.body, "translated_state_error_body")

    @patch('mobileraker.service.notification_evaluator.translate_replace_placeholders')
    def test_evaluate_state_notification_error_to_printing(self, mock_translate):
        """error → printing generates a 'started printing' notification.

        This is the other side of the oscillation. After the fix this transition should
        only fire when the state genuinely changes (not from a transient reconnect blip).
        """
        mock_translate.side_effect = lambda key, *args, **kwargs: f"translated_{key}"
        self.device_cfg.snap.state = "error"

        result = self.evaluator.evaluate_state_notification(self.device_cfg, self.printing_snapshot)

        self.assertIsInstance(result, NotificationContentDto)
        self.assertEqual(result.body, "translated_state_printing_body")

    # ------------------------------------------------------------------
    # Progress notification — threshold not reached
    # ------------------------------------------------------------------

    @patch('mobileraker.service.notification_evaluator.normalized_progress_interval_reached')
    def test_evaluate_progress_notification_threshold_not_reached(self, mock_interval_reached):
        """No progress notification when the interval threshold has not been crossed."""
        mock_interval_reached.return_value = False
        self.device_cfg.snap.state = "printing"
        self.device_cfg.snap.progress = 49  # just below 50

        result = self.evaluator.evaluate_progress_notification(self.device_cfg, self.printing_snapshot)

        self.assertIsNone(result)
        mock_interval_reached.assert_called_once()

    # ------------------------------------------------------------------
    # Progressbar notification — progress unchanged
    # ------------------------------------------------------------------

    @patch('mobileraker.service.notification_evaluator.normalized_progress_interval_reached')
    def test_evaluate_progressbar_notification_progress_unchanged(self, mock_interval_reached):
        """No progressbar notification when neither the progress interval nor the time interval is reached."""
        mock_interval_reached.return_value = False
        self.device_cfg.snap.state = "printing"
        self.device_cfg.snap.progress_progressbar = 49
        # Prevent the secondary time-interval condition from triggering
        self.device_cfg.snap.last_progress_progressbar = datetime.now()

        result = self.evaluator.evaluate_progressbar_notification(self.device_cfg, self.printing_snapshot)

        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # evaluate_all_notifications_for_device — precise assertions
    # ------------------------------------------------------------------

    def test_evaluate_all_notifications_for_device_no_notifications(self):
        """Test when minimal notifications should be generated."""
        # Create snapshot with same state and progress
        same_snapshot = self._create_test_snapshot(print_state="printing", progress=50)
        self.device_cfg.snap.state = "printing" 
        self.device_cfg.snap.progress = 50
        self.device_cfg.snap.progress_live_activity = 50
        self.device_cfg.snap.progress_progressbar = 50
        
        result = self.evaluator.evaluate_all_notifications_for_device(
            self.device_cfg, same_snapshot, same_snapshot, exclude_sensors=[]
        )
        
        # Should have minimal notifications - no state change, no progress change, etc.
        # Live activity might still update, so we check for specific notification types that shouldn't be there
        state_notifications = [n for n in result.notifications if hasattr(n, 'channel') and 'statusUpdates' in n.channel]
        progress_notifications = [n for n in result.notifications if hasattr(n, 'channel') and 'progressUpdates' in n.channel]
        
        self.assertEqual(len(state_notifications), 0, "No state notifications when state hasn't changed")
        self.assertEqual(len(progress_notifications), 0, "No progress notifications when progress hasn't changed significantly")
        self.assertFalse(result.has_progress_notification, "Should not have progress notification flag")

    def test_evaluate_all_notifications_for_device_with_excluded_sensors(self):
        """Test that excluded sensors are properly filtered out."""
        # Create snapshot with filament sensor trigger
        sensor1 = FilamentSensor(name="extruder", kind="switch", enabled=True, filament_detected=False)
        sensor2 = FilamentSensor(name="runout", kind="switch", enabled=True, filament_detected=False)
        
        filament_snapshot = self._create_test_snapshot(
            filament_sensors={"sensor1": sensor1, "sensor2": sensor2}
        )
        
        # Exclude sensor1
        result = self.evaluator.evaluate_all_notifications_for_device(
            self.device_cfg, filament_snapshot, None, exclude_sensors=["sensor1"]
        )
        
        # Should have filament sensor notification for sensor2 only
        filament_notifications = [n for n in result.notifications if hasattr(n, 'channel') and 'filamentSensor' in n.channel]
        
        # Should have exactly 1 filament sensor notification (for sensor2)
        self.assertEqual(len(filament_notifications), 1)


if __name__ == '__main__':
    unittest.main()