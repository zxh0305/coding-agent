import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db
from browser_tools import BrowserManager, browser_type
from tests.test_session_model import ApiTestBase


class BrowserActorTest(unittest.TestCase):
    def test_serial_calls_use_single_thread_and_reopen_after_close(self):
        manager=BrowserManager('actor')
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                ids=list(pool.map(lambda _:manager._call(threading.get_ident),range(20)))
            self.assertEqual(len(set(ids)),1)
            self.assertNotEqual(ids[0],threading.get_ident())
            manager.close()
            self.assertFalse(manager.state()['open'])
            self.assertEqual(manager._call(threading.get_ident),ids[0])
        finally:manager.close()

    def test_manual_wait_is_cancellable_and_release_resumes(self):
        manager=BrowserManager('manual');cancel=threading.Event()
        def takeover():manager._manual=True;manager._released.clear()
        manager._call(takeover)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                call=pool.submit(browser_type,'name','value',SimpleNamespace(browser=manager,cancel_event=cancel))
                self.assertFalse(call.done());cancel.set()
                self.assertFalse(json.loads(call.result(2))['ok'])
            manager.control('release')
            self.assertTrue(manager._released.is_set())
            self.assertFalse(manager.state()['manual'])
        finally:manager.close()

    def test_release_continues_waiting_tool_without_duplicate_action(self):
        manager=BrowserManager('release');cancel=threading.Event()
        manager._call(lambda:(setattr(manager,'_manual',True),manager._released.clear()))
        try:
            with patch.object(manager,'type_text',return_value='{"ok":true}') as action,ThreadPoolExecutor(max_workers=1) as pool:
                call=pool.submit(browser_type,'Name','value',SimpleNamespace(browser=manager,cancel_event=cancel))
                self.assertFalse(call.done());action.assert_not_called()
                manager.control('release')
                self.assertTrue(json.loads(call.result(2))['ok'])
                action.assert_called_once_with('Name','value')
        finally:manager.close()

    def test_takeover_race_does_not_dispatch_agent_action(self):
        manager=BrowserManager('race')
        try:
            manager._call(lambda:setattr(manager,'_manual',True))
            with patch.object(manager,'_ensure',side_effect=AssertionError('must not operate')):
                self.assertFalse(json.loads(manager.open('http://localhost'))['ok'])
        finally:manager.close()

    def test_new_profile_does_not_copy_system_browser(self):
        manager=BrowserManager('isolated')
        with tempfile.TemporaryDirectory() as tmp,patch('browser_tools.Path',side_effect=lambda path:Path(tmp)/path):
            path=manager._profile_dir()
            self.assertTrue(path.is_dir());self.assertEqual(list(path.iterdir()),[])


class BrowserApiTest(ApiTestBase):
    def test_owner_and_archive_checks(self):
        manager=SimpleNamespace(state=lambda:{'open':False},control=lambda *args:{'open':True})
        with patch('browser_tools.manager_for',return_value=manager) as factory:
            self.assertEqual(self.call('GET','/api/sessions/foreign/browser')[0],404)
            factory.assert_not_called()
            self.assertEqual(self.call('GET','/api/sessions/s1/browser')[1],{'open':False})
            self.assertEqual(self.call('POST','/api/sessions/s1/browser',{'action':'takeover'})[0],200)
            db.archive_session('s1',1)
            self.assertEqual(self.call('POST','/api/sessions/s1/browser',{'action':'takeover'})[0],409)
            self.assertEqual(self.call('GET','/api/sessions/s1/browser')[0],200)
