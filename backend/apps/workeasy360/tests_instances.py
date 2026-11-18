"""
Two SendStack instances, one database: a WorkEasy360 sign-out reaches both at once.

Instance A is this test process. Instance B is a second Python process
running the same Django project on 127.0.0.1, against the same test
database, with its own in-process caches. The fake WorkEasy360 provider is a
real HTTP server both of them talk to.

TransactionTestCase, because B must see what A commits.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase

from apps.workeasy360.tests import BACKCHANNEL, ME, WorkEasy360Mixin

INSTANCE = r"""
import os, sys
import django
django.setup()
from django.core.wsgi import get_wsgi_application
from wsgiref.simple_server import WSGIRequestHandler, make_server

class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass

server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=Quiet)
with open(sys.argv[1], "w") as ready:
    ready.write(str(server.server_port))
server.serve_forever()
"""


class TwoInstanceTests(WorkEasy360Mixin, TransactionTestCase):
    serialized_rollback = True

    def start_instance(self):
        workdir = tempfile.mkdtemp(prefix="w360-instance-")
        ready = os.path.join(workdir, "port")
        log = open(os.path.join(workdir, "log"), "w")
        env = dict(os.environ)
        env.update(self.provider.environment())
        env.update({
            "DB_NAME": connection.settings_dict["NAME"],
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "PYTHONUNBUFFERED": "1",
        })
        process = subprocess.Popen(
            [sys.executable, "-c", INSTANCE, ready], cwd=str(settings.BASE_DIR), env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )

        def stop():
            process.kill()
            process.wait(timeout=30)
            log.close()
            shutil.rmtree(workdir, ignore_errors=True)

        self.addCleanup(stop)
        deadline = time.time() + 90
        while time.time() < deadline:
            if process.poll() is not None:
                log.flush()
                with open(os.path.join(workdir, "log")) as output:
                    self.fail(f"instance B exited: {output.read()[-3000:]}")
            if os.path.exists(ready) and open(ready).read().strip():
                return f"http://127.0.0.1:{open(ready).read().strip()}"
            time.sleep(0.2)
        self.fail("instance B did not start within 90 s")

    def call(self, base, path, *, cookie=None, form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(base + path, data=data, method="POST" if form is not None else "GET")
        if cookie:
            request.add_header("Cookie", f"sessionid={cookie}")
        if form is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_a_logout_delivered_to_either_instance_ends_the_session_on_both_at_once(self):
        other = self.start_instance()

        # Signed in through A; B accepts the same session from the shared database.
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        cookie = client.cookies["sessionid"].value
        self.assertEqual(client.get(ME).status_code, 200)
        self.assertEqual(self.call(other, ME, cookie=cookie), 200)

        # WorkEasy360 delivers the logout token to B: A refuses on its very next request.
        token = self.provider.logout_token(sid=person["sid"], sub=person["sub"])
        self.assertEqual(self.call(other, BACKCHANNEL, form={"logout_token": token}), 200)
        self.assertEqual(client.get(ME).status_code, 401)
        self.assertEqual(self.call(other, ME, cookie=cookie), 401)

        # And the other way round: delivered to A, refused by B at once.
        client2, person2, response2 = self.sign_in()
        self.assert_signed_in(response2)
        cookie2 = client2.cookies["sessionid"].value
        self.assertEqual(self.call(other, ME, cookie=cookie2), 200)
        self.assertEqual(self.deliver(self.provider.logout_token(sub=person2["sub"])).status_code, 200)
        self.assertEqual(self.call(other, ME, cookie=cookie2), 401)
        self.assertEqual(client2.get(ME).status_code, 401)

        # A token replayed to the other instance is still a replay.
        self.assertEqual(self.call(other, BACKCHANNEL, form={"logout_token": token}), 400)
