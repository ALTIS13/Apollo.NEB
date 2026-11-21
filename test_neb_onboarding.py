"""End-to-end HTTP contracts; no network mining or production credentials."""
import http.client
import io
import json
import secrets
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

import neb_app
import neb_core as core
import neb_fleet
import neb_release
from test_neb_release import release
from neb import persisted_totp
from test_neb_server import ExampleWorker
from test_neb_core import OWNER


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        cfg = self.base/"config"
        core.store(cfg/"profile.json", {"wallet":OWNER,"worker":"test","pool":"suprnova-eu"})
        self.runtime = ExampleWorker()
        self.fleet = neb_fleet.Fleet(self.base/"fleet.sqlite")
        self.auth = core.Auth(core.new_login("test password only"), self.base/"auth.sqlite")
        self.service = neb_app.Service(cfg,self.base,self.runtime,core.DEFAULT_POOLS,None,fleet=self.fleet)
        self.service.poll()
        self.server = neb_app.Server(("127.0.0.1",0),self.service,self.auth,neb_app.HERE/"web",None)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def shutdown(self):
        self.server.shutdown()
        self.thread.join()

    def send(self, operation, route, body=None, cookie=None, *, browser=True):
        headers = {"Origin":self.origin} if browser else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        with closing(http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=5)) as cn:
            cn.request(operation,route,json.dumps(body) if body is not None else None,headers=headers)
            res = cn.getresponse()
            protected = res.getheader("Set-Cookie")
            return res.status,json.load(res),protected.split(";",1)[0] if protected else None

    def login(self, code=None):
        return self.send("POST","/api/login",{"password":"test password only","code":code})

    def test_browser_enables_totp_and_rotates_the_original_cookie(self):
        self.assertEqual(self.send("GET","/api/auth/info")[1],{"totp":False})
        _,_,session = self.login()
        status,begin,_ = self.send("POST","/api/auth/totp/begin",{"password":"test password only"},session)
        self.assertEqual(status,200)
        self.assertTrue(begin["uri"].startswith("otpauth://"))
        code = core.totp(begin["secret"],time.time())
        changed,result,fresh = self.send("POST","/api/auth/totp/confirm",{"code":code},session)
        self.assertEqual(changed,200)
        self.assertNotIn("secret",result)
        self.assertEqual(self.send("GET","/api/status",cookie=session)[0],401)
        self.assertEqual(self.send("GET","/api/status",cookie=fresh)[0],200)
        self.assertEqual(self.send("GET","/api/auth/info")[1],{"totp":True})
        rescue = result["recovery_codes"][0]
        self.assertEqual(self.login(rescue)[0],200)
        self.assertEqual(self.login(rescue)[0],401)

    def test_groups_reject_unknown_workers_without_partial_execution(self):
        _,_,session = self.login()
        self.assertEqual(self.send("POST","/api/group/start",{"ids":["gpu","other"]},session)[0],409)
        self.assertEqual(self.runtime.calls,[])
        self.assertEqual(self.send("POST","/api/group/start",{"ids":["gpu"]},session)[1]["results"],
                         [{"id":"gpu","ok":True}])

    def test_agent_pairing_observation_command_ack_and_revocation_over_http(self):
        _,_,session = self.login()
        self.assertEqual(self.send("POST","/api/fleet/enroll",{"label":"Offline QA node"})[0],401)
        status, invite,_ = self.send("POST","/api/fleet/enroll",{"label":"Offline QA node"},session)
        self.assertEqual(status,200)
        id_ = invite["id"]
        edge = neb_fleet.Client(self.origin,id_,secrets.token_urlsafe(32),self.base/"agent.sqlite",
                                allow_loopback=True)
        edge.join(invite["code"])
        stages = [{"id":"gpu","state":"stopped"}]
        calls=[]
        edge.cycle(stages,lambda a,ids:calls.append((a,ids)))
        self.assertEqual(self.send("GET","/api/fleet",cookie=session)[1]["nodes"][0]["status"],"online")
        status,_,_ = self.send("POST","/api/fleet/command",{"id":id_,"action":"start","ids":["gpu"]},session)
        self.assertEqual(status,200)
        for _ in range(2):
            edge.cycle(stages,lambda a,ids:calls.append((a,ids)))
        self.assertEqual(calls,[("start",["gpu"])])
        node = self.send("GET","/api/fleet",cookie=session)[1]["nodes"][0]
        self.assertEqual(node["commands"][0]["status"],"acknowledged")
        self.assertEqual(self.send("POST","/api/fleet/revoke",{"id":id_},session)[0],200)
        with self.assertRaises((ValueError,RuntimeError)):
            edge.cycle(stages,lambda a,ids:calls.append((a,ids)))


class ReleaseTests(unittest.TestCase):
    def test_release_catalog_excludes_versions_before_first_mandatory_devfee(self):
        retired = [release("v0.6.0-rc.1"), release("v0.7.0-rc.1")]
        self.assertIsNone(neb_release.select(retired, channel="testing"))
        current = release("v0.7.1-rc.1")
        self.assertEqual(neb_release.select([*retired, current], channel="testing").tag, "v0.7.1-rc.1")

    def test_staging_reads_legacy_auth_without_migrating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"auth.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE auth (id INTEGER PRIMARY KEY,last_tick INTEGER NOT NULL)")
                db.execute("INSERT INTO auth VALUES(1,15)")
            self.assertTrue(persisted_totp({"totp":"legacy fixture"},path))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT OR IGNORE INTO auth VALUES(1,-1)")
                self.assertEqual(db.execute("SELECT * FROM auth").fetchall(),[(1,15)])

    def test_decay_to_alternative_only_when_exact_release_asset_is_valid(self):
        resolved = neb_release.select([release("v2.0.0", "wrong.tar.gz"),
                                       release("v1.0.1-rc.2"), release("v1.0.1")])
        self.assertEqual(resolved.tag, "v1.0.1")
        self.assertEqual(resolved.asset.url,
                         "https://github.com/ALTIS13/Apollo.NEB/releases/download/v1.0.1/apollo-neb-1.0.1-linux.tar.gz")

    def test_release_import_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base/"archive"
            with tarfile.open(archive,"w:gz") as tar:
                member = tarfile.TarInfo("Apollo.NEB-1.0.0/install.sh")
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                tar.addfile(member)
            with self.assertRaises(ValueError):
                neb_release.safe_unpack(archive,base/"output")
