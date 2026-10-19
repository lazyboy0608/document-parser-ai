import sys
import tempfile
import unittest
import threading
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestion.folder_watcher import HotFolderWatcher


class TestWatcherHeartbeat(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.watch_dir = self.base_path / "hot_folder"
        self.output_dir = self.base_path / "output_folder"
        self.watch_dir.mkdir(parents=True)
        self.output_dir.mkdir(parents=True)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_watcher_initialization_and_stop(self):
        """Verify HotFolderWatcher can start and stop gracefully without leaking threads."""
        watcher = HotFolderWatcher(
            watch_dir=self.watch_dir,
            output_dir=self.output_dir,
            stable_seconds=0.5,
            mode="copy"
        )
        t = threading.Thread(target=watcher.start, kwargs={"run_initial_scan": False}, daemon=True)
        t.start()
        time.sleep(1.0)
        self.assertTrue(watcher.observer.is_alive())

        # Stop
        watcher.stop()
        t.join(timeout=3.0)
        self.assertFalse(watcher.observer.is_alive())


if __name__ == "__main__":
    unittest.main()
