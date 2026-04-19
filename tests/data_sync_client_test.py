import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mobileraker.client.moonraker_client import MoonrakerClient
from mobileraker.data.dtos.moonraker.printer_objects import DisplayStatus, FilamentSensor, PrintStats, ServerInfo, VirtualSDCard
from mobileraker.data.dtos.moonraker.printer_snapshot import PrinterSnapshot
from mobileraker.service.data_sync_service import DataSyncService, KlippyNotReadyError


class TestDataSyncService(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.jrpc = MagicMock()
        # resync_retries=1 keeps the not-ready timeout test fast (only one sleep cycle)
        self.data_sync_service = DataSyncService(self.jrpc, "test_printer", self.loop, resync_retries=1)
        # Replace _loop with a mock so create_task calls in synchronous tests don't fail
        self.data_sync_service._loop = MagicMock()

    def tearDown(self):
        self.loop.close()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def test_initialization(self):
        self.assertFalse(self.data_sync_service.klippy_ready)
        self.assertIsInstance(self.data_sync_service.server_info, ServerInfo)
        self.assertIsInstance(self.data_sync_service.print_stats, PrintStats)
        self.assertIsInstance(self.data_sync_service.display_status, DisplayStatus)
        self.assertIsInstance(self.data_sync_service.virtual_sdcard, VirtualSDCard)

    # ------------------------------------------------------------------
    # _parse_objects
    # ------------------------------------------------------------------

    def test_parse_objects_with_print_stats(self):
        status_objects = {
            "print_stats": {"filename": "test.gcode", "state": "printing"}
        }
        self.data_sync_service._parse_objects(status_objects)
        self.assertEqual(self.data_sync_service.print_stats.filename, "test.gcode")
        self.assertEqual(self.data_sync_service.print_stats.state, "printing")

    def test_parse_objects_with_display_status(self):
        status_objects = {
            "display_status": {"message": "Printing in progress"}
        }
        self.data_sync_service._parse_objects(status_objects)
        self.assertEqual(self.data_sync_service.display_status.message, "Printing in progress")

    def test_parse_objects_with_virtual_sdcard(self):
        status_objects = {
            "virtual_sdcard": {"progress": 0.5}
        }
        self.data_sync_service._parse_objects(status_objects)
        self.assertEqual(self.data_sync_service.virtual_sdcard.progress, 0.5)

    def test_parse_objects_with_all_status_objects(self):
        status_objects = {
            "print_stats": {"filename": "test.gcode", "state": "printing"},
            "display_status": {"message": "Printing in progress"},
            "virtual_sdcard": {"progress": 0.5}
        }
        self.data_sync_service._parse_objects(status_objects)
        self.assertEqual(self.data_sync_service.print_stats.filename, "test.gcode")
        self.assertEqual(self.data_sync_service.print_stats.state, "printing")
        self.assertEqual(self.data_sync_service.display_status.message, "Printing in progress")
        self.assertEqual(self.data_sync_service.virtual_sdcard.progress, 0.5)

    def test_parse_objects_with_no_status_objects(self):
        status_objects = {}
        self.data_sync_service._parse_objects(status_objects)
        self.assertIsNone(self.data_sync_service.print_stats.filename)
        self.assertEqual(self.data_sync_service.print_stats.state, "error")
        self.assertIsNone(self.data_sync_service.display_status.message)
        self.assertEqual(self.data_sync_service.virtual_sdcard.progress, 0)

    def test_parse_objects_filament_switch_sensor(self):
        """Filament switch sensor objects are parsed and stored by sensor name."""
        status_objects = {
            "filament_switch_sensor extruder_sensor": {"enabled": True, "filament_detected": False}
        }
        self.data_sync_service._parse_objects(status_objects)
        self.assertIn("extruder_sensor", self.data_sync_service.filament_sensors)
        sensor = self.data_sync_service.filament_sensors["extruder_sensor"]
        self.assertTrue(sensor.enabled)
        self.assertFalse(sensor.filament_detected)

    def test_parse_objects_filament_sensor_appears_in_snapshot(self):
        """Filament sensor state parsed from objects is visible in the next snapshot."""
        status_objects = {
            "filament_switch_sensor runout_sensor": {"enabled": True, "filament_detected": False}
        }
        self.data_sync_service._parse_objects(status_objects)
        snap = self.data_sync_service.take_snapshot()
        self.assertIn("runout_sensor", snap.filament_sensors)

    # ------------------------------------------------------------------
    # take_snapshot — the method that maps klippy_ready → print_state
    # ------------------------------------------------------------------

    def test_take_snapshot_when_klippy_ready(self):
        """klippy_ready=True → print_state equals the actual print_stats.state."""
        self.data_sync_service.klippy_ready = True
        self.data_sync_service.print_stats = self.data_sync_service.print_stats.updateWith(
            {"state": "printing"})

        snap = self.data_sync_service.take_snapshot()

        self.assertIsInstance(snap, PrinterSnapshot)
        self.assertTrue(snap.klippy_ready)
        self.assertEqual(snap.print_state, "printing")

    def test_take_snapshot_when_klippy_not_ready(self):
        """klippy_ready=False → print_state is forced to 'error' regardless of print_stats.state.

        This is the root mapping that caused spurious 'Error while printing' notifications
        during WebSocket reconnect cycles where klippy_ready briefly flipped False.
        """
        self.data_sync_service.klippy_ready = False
        self.data_sync_service.print_stats = self.data_sync_service.print_stats.updateWith(
            {"state": "printing"})

        snap = self.data_sync_service.take_snapshot()

        self.assertFalse(snap.klippy_ready)
        self.assertEqual(snap.print_state, "error",
                         "klippy_ready=False must force print_state='error' "
                         "regardless of the underlying print_stats.state")

    # ------------------------------------------------------------------
    # Listener notification
    # ------------------------------------------------------------------

    def test_notify_listeners_calls_registered_callbacks(self):
        """_notify_listeners dispatches a PrinterSnapshot to every registered callback."""
        received = []
        self.data_sync_service.register_snapshot_listener(lambda snap: received.append(snap))

        self.data_sync_service._notify_listeners()

        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], PrinterSnapshot)

    def test_notify_listeners_calls_multiple_callbacks(self):
        received_a, received_b = [], []
        self.data_sync_service.register_snapshot_listener(lambda s: received_a.append(s))
        self.data_sync_service.register_snapshot_listener(lambda s: received_b.append(s))

        self.data_sync_service._notify_listeners()

        self.assertEqual(len(received_a), 1)
        self.assertEqual(len(received_b), 1)

    # ------------------------------------------------------------------
    # Klippy event handlers
    # ------------------------------------------------------------------

    def test_on_klippy_disconnected_sets_not_ready_and_notifies(self):
        """notify_klippy_disconnected sets klippy_ready=False and fires listeners with an error snapshot."""
        self.data_sync_service.klippy_ready = True
        received = []
        self.data_sync_service.register_snapshot_listener(lambda snap: received.append(snap))

        self.data_sync_service._on_klippy_disconnected()

        self.assertFalse(self.data_sync_service.klippy_ready)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].print_state, "error")

    def test_on_klippy_shutdown_sets_not_ready_and_notifies(self):
        """notify_klippy_shutdown sets klippy_ready=False and fires listeners with an error snapshot."""
        self.data_sync_service.klippy_ready = True
        received = []
        self.data_sync_service.register_snapshot_listener(lambda snap: received.append(snap))

        self.data_sync_service._on_klippy_shutdown()

        self.assertFalse(self.data_sync_service.klippy_ready)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].print_state, "error")

    # ------------------------------------------------------------------
    # resync
    # ------------------------------------------------------------------

    def _setup_resync_jrpc_mocks(self):
        """Make send_method awaitable for resync tests that reach _subscribe_for_object_updates."""
        self.jrpc.send_method = AsyncMock()

    def test_resync_with_parse_objects(self):
        self._setup_resync_jrpc_mocks()
        status_objects = {
            "print_stats": {"filename": "test.gcode", "state": "printing"},
            "display_status": {"message": "Printing in progress"},
            "virtual_sdcard": {"progress": 0.5}
        }

        async def mock_send_and_receive_method(method, params=None):
            if method == "server.info":
                return {"result": {"klippy_state": "ready"}}, None
            elif method == "printer.objects.list":
                return {"result": {"objects": ["print_stats", "display_status", "virtual_sdcard"]}}, None
            elif method == "printer.objects.query":
                return {"result": {"status": status_objects}}, None
            return {"result": {}}, None

        self.jrpc.send_and_receive_method.side_effect = mock_send_and_receive_method
        self.loop.run_until_complete(self.data_sync_service.resync())

        self.assertEqual(self.data_sync_service.print_stats.filename, "test.gcode")
        self.assertEqual(self.data_sync_service.print_stats.state, "printing")
        self.assertEqual(self.data_sync_service.display_status.message, "Printing in progress")
        self.assertEqual(self.data_sync_service.virtual_sdcard.progress, 0.5)

    def test_resync_klippy_ready(self):
        self._setup_resync_jrpc_mocks()
        async def mock_send_and_receive_method(method, params=None):
            if method == "server.info":
                return {"result": {"klippy_state": "ready"}}, None
            elif method == "printer.objects.list":
                return {"result": {"objects": []}}, None
            elif method == "printer.objects.query":
                return {"result": {"status": {}}}, None
            return {"result": {}}, None

        self.jrpc.send_and_receive_method.side_effect = mock_send_and_receive_method
        self.loop.run_until_complete(self.data_sync_service.resync())

        self.assertTrue(self.data_sync_service.klippy_ready)

    def test_resync_klippy_not_ready_exhausts_retries(self):
        """When Klippy never becomes ready, resync exhausts retries without raising to the caller."""
        async def mock_send_and_receive_method(method, params=None):
            return {"result": {"klippy_state": "not_ready"}}, None

        self.jrpc.send_and_receive_method.side_effect = mock_send_and_receive_method

        # Patch sleep so the exponential backoff doesn't slow down the test suite
        with patch('mobileraker.service.data_sync_service.sleep'):
            self.loop.run_until_complete(self.data_sync_service.resync())

        self.assertFalse(self.data_sync_service.klippy_ready)


if __name__ == '__main__':
    unittest.main()
