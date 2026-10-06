"""Offline tests: no screen capture, mouse movement, keys, or Windows notifications."""
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PIL import Image
from PySide6.QtWidgets import QApplication

fake_mss = types.ModuleType('mss')
fake_mss.tools = types.ModuleType('mss.tools')
fake_pynput = types.ModuleType('pynput')
fake_pynput.mouse = types.SimpleNamespace(Controller=Mock, Button=types.SimpleNamespace(left='left'))
fake_keyboard = types.ModuleType('pynput.keyboard')
fake_keyboard.Controller = Mock
fake_keyboard.Key = types.SimpleNamespace(right='right', f5='f5')
spec = importlib.util.spec_from_file_location('capture_app', Path(__file__).with_name('eBookToPdf.py'))
module = importlib.util.module_from_spec(spec)
with patch.dict('sys.modules', {'mss': fake_mss, 'mss.tools': fake_mss.tools,
                               'pynput': fake_pynput, 'pynput.keyboard': fake_keyboard}):
    spec.loader.exec_module(module)


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.folder = Path(self.temp.name)
        self.keyboard = Mock()
        self.captured = []
        owner = self

        class Screen:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, region):
                owner.captured.append(region)
                return types.SimpleNamespace(rgb=b'\xff' * 12, size=(2, 2))

        def save_png(rgb, size, output):
            with Image.frombytes('RGB', size, rgb) as image:
                image.save(output)

        self.patches = [patch.object(module, 'Controller', return_value=self.keyboard),
                        patch.object(fake_mss, 'mss', Screen, create=True),
                        patch.object(fake_mss.tools, 'to_png', save_png, create=True)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def worker(self, total=2):
        worker = module.CaptureWorker(dict(
            pdf_path=str(self.folder / 'result.pdf'), total=total, speed=0.001,
            refresh_interval=35, refresh_wait=10, refresh_enabled=True,
            region=dict(top=0, left=0, width=2, height=2)))
        worker.focus_viewer = Mock()
        return worker

    def pump_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail('Timed out waiting for worker')
            time.sleep(0.005)
        self.app.processEvents()

    def test_pause_freezes_active_wait_and_cancel_wakes_pause(self):
        control = module.JobControl()
        control.set_paused(True)
        paused = threading.Event()
        outcome = []
        def wait():
            try: control.wait(10, paused.set)
            except module.CaptureCancelled: outcome.append('cancelled')
        thread = threading.Thread(target=wait)
        thread.start()
        try:
            self.assertTrue(paused.wait(1))
            self.assertTrue(thread.is_alive())
            control.cancel()
            thread.join(1)
            self.assertEqual(outcome, ['cancelled'])
        finally:
            control.cancel()
            thread.join(1)

    def test_paused_time_does_not_consume_remaining_wait(self):
        control = module.JobControl()
        control.set_paused(True)
        complete = threading.Event()
        thread = threading.Thread(target=lambda: (control.wait(0.1), complete.set()))
        thread.start()
        time.sleep(0.15)
        self.assertFalse(complete.is_set())
        control.set_paused(False)
        self.assertFalse(complete.wait(0.04))
        thread.join(1)
        self.assertTrue(complete.is_set())

    def test_refresh_boundaries_and_real_pdf(self):
        for total, enabled, expected in [(35, True, 0), (36, True, 1),
                                          (71, True, 2), (71, False, 0)]:
            worker = self.worker(total)
            worker.settings['refresh_enabled'] = enabled
            self.keyboard.reset_mock()
            waits = []
            worker.wait_active = lambda seconds=0: waits.append(seconds)
            result = []
            worker.outcome.connect(lambda *args: result.append(args))
            worker.run()
            self.assertEqual(result[0][0], 'success')
            keys = [call.args[0] for call in self.keyboard.press.call_args_list]
            self.assertEqual(keys.count('f5'), expected)
            self.assertEqual(keys.count('right'), total - 1)
            self.assertEqual(waits.count(10), expected)
            self.assertTrue((self.folder / 'result.pdf').read_bytes().startswith(b'%PDF'))
            self.assertFalse(list(self.folder.glob('.ebook-capture-*')))

    def test_cancel_during_pdf_write_preserves_existing_pdf(self):
        target = self.folder / 'result.pdf'
        target.write_bytes(b'original')
        worker = self.worker()
        result = []
        worker.outcome.connect(lambda *args: result.append(args))
        original_write = module.ControlledWriter.write
        def cancel_write(writer, data):
            worker.control.cancel()
            return original_write(writer, data)
        with patch.object(module.ControlledWriter, 'write', cancel_write):
            worker.run()
        self.assertEqual(result[0][0], 'cancelled')
        self.assertEqual(target.read_bytes(), b'original')
        self.assertFalse(list(self.folder.glob('.ebook-capture-*')))

    def test_gui_pause_resume_stop_and_restart(self):
        window = module.MainWindow()
        window.input1.setText('500')
        window.input2.setText('result')
        window.output_directory.setText(str(self.folder))
        window.posX2 = window.posY2 = 2
        window.speed = 0.02
        with patch.object(module.CaptureWorker, 'focus_viewer') as focus, \
                patch.object(window, 'notify_pdf_complete') as notify:
            try:
                window.btn_click()
                worker = window.worker
                self.assertFalse(window.button3.isEnabled())
                window.btn_click()
                self.assertIs(window.worker, worker)
                self.pump_until(lambda: len(self.captured) >= 1)
                window.pause_button.click()
                self.pump_until(lambda: worker.control.paused)
                time.sleep(0.08)
                count = len(self.captured)
                time.sleep(0.08)
                self.assertEqual(len(self.captured), count)
                window.pause_button.click()
                self.pump_until(lambda: len(self.captured) > count)
                self.assertGreaterEqual(focus.call_count, 2)
                window.pause_button.click()
                window.stop_button.click()
                self.pump_until(lambda: window.worker is None)
                notify.assert_not_called()
                self.assertTrue(window.button3.isEnabled())
                self.assertFalse((self.folder / 'result.pdf').exists())
                self.assertFalse(list(self.folder.glob('.ebook-capture-*')))
                window.input1.setText('1')
                window.btn_click()
                self.pump_until(lambda: window.worker is None)
                notify.assert_called_once()
            finally:
                if window.worker:
                    window.stop_capture()
                    self.pump_until(lambda: window.worker is None)
                window.close()

    def test_stop_during_refresh_wait(self):
        worker = self.worker(36)
        worker.start()
        try:
            self.pump_until(lambda: any(c.args[0] == 'f5' for c in self.keyboard.press.call_args_list))
            worker.control.cancel()
            self.assertTrue(worker.wait(1000))
            self.assertEqual(len(self.captured), 35)
            self.assertFalse((self.folder / 'result.pdf').exists())
        finally:
            worker.control.cancel()
            worker.wait(1000)

    def test_close_cancels_worker_and_cleans_up(self):
        window = module.MainWindow()
        window.input1.setText('500')
        window.input2.setText('result')
        window.output_directory.setText(str(self.folder))
        window.posX2 = window.posY2 = 2
        window.show()
        with patch.object(module.CaptureWorker, 'focus_viewer'), \
                patch.object(window, 'notify_pdf_complete') as notify:
            window.btn_click()
            window.close()
            self.pump_until(lambda: window.worker is None)
            self.assertFalse(window.isVisible())
            self.assertFalse(list(self.folder.glob('.ebook-capture-*')))
            notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
