import io
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

import neb


ORIGIN = "https://fleet.fixture.invalid"
NODE = "0123456789abcdef01234567"
CODE = "fixture_code_0123456789X"
LOCAL_KEY = "fixture_node_key_" + "x" * 26


class NodeCliTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        directory = Path(root.name).resolve()
        self.cfg, self.state = directory / "config", directory / "state"
        self.cfg.mkdir()
        self.state.mkdir()
        self.link = self.cfg / "fleet-link.json"
        neb.core.store(self.cfg / "login.json", {"fixture": True})
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.client = stack.enter_context(patch("neb_fleet.Client"))
        self.prompt = stack.enter_context(patch.object(neb.getpass, "getpass"))
        self.local_key = stack.enter_context(patch.object(neb.secrets, "token_urlsafe", return_value=LOCAL_KEY))
        self.private = stack.enter_context(patch.object(neb, "private", side_effect=lambda path: path))
        self.lock = stack.enter_context(patch.object(neb, "lock_state", side_effect=lambda _: nullcontext()))
        stack.enter_context(patch.object(neb.sys, "platform", "linux"))
        stack.enter_context(patch.object(neb.os, "geteuid", return_value=1000, create=True))
        stack.enter_context(patch.object(neb.os, "umask"))

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["apollo-neb", "--config-dir", str(self.cfg), "--state-dir", str(self.state), *args]
        with patch.object(neb.sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = neb.main()
            except SystemExit as error:
                status = error.code
        output, errors = stdout.getvalue(), stderr.getvalue()
        self.assertNotIn(CODE, output + errors)
        self.assertNotIn(LOCAL_KEY, output + errors)
        return status, output, errors

    def write_paired(self, **changes):
        stored = {"origin": ORIGIN, "id": NODE, "key": LOCAL_KEY, "allow_loopback": False, "paired": True}
        neb.core.store(self.link, {**stored, **changes})

    def test_short_pair_reads_both_invitation_parts_from_the_masked_prompt(self):
        self.prompt.return_value = f"  {NODE}.{CODE}  "
        status, output, error = self.run_cli("node-pair", "--origin", ORIGIN, "--share-diagnostics")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Нода подключена", output)
        self.assertIn("Ключ подключения", self.prompt.call_args.args[0])
        self.client.return_value.join.assert_called_once_with(CODE)
        self.client.assert_called_once_with(
            ORIGIN, NODE, LOCAL_KEY,
            neb.link_receipts(self.state, {"origin": ORIGIN, "id": NODE, "key": LOCAL_KEY}),
            allow_loopback=False,
        )
        self.assertEqual(neb.core.read(self.link), {
            "origin": ORIGIN, "id": NODE, "key": LOCAL_KEY, "allow_loopback": False,
            "paired": True, "share_diagnostics": True,
        })
        self.lock.assert_called_once_with(self.state)

    def test_legacy_pair_reuses_a_partially_paired_local_key_after_failure(self):
        self.prompt.return_value = CODE
        self.client.return_value.join.side_effect = (ValueError("Связь прервана."), {"ok": True})
        args = ("node-pair", "--origin", ORIGIN, "--id", NODE)
        status, _, error = self.run_cli(*args)
        self.assertEqual(status, 1)
        self.assertIn("Связь прервана", error)
        partial = neb.core.read(self.link)
        self.assertIs(partial["paired"], False)
        self.assertEqual(partial["key"], LOCAL_KEY)
        status, _, error = self.run_cli(*args)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(neb.core.read(self.link), {**partial, "paired": True, "share_diagnostics": False})
        self.assertEqual(self.client.call_args_list[0], self.client.call_args_list[1])
        self.client.return_value.join.assert_has_calls([call(CODE), call(CODE)])
        self.local_key.assert_called_once()
        self.assertTrue(all("Одноразовый код" in prompt.args[0] for prompt in self.prompt.call_args_list))

    def test_malformed_short_invitation_does_not_store_or_echo_the_key(self):
        self.prompt.return_value = NODE + "." + CODE + ".extra"
        status, output, error = self.run_cli("node-pair", "--origin", ORIGIN)
        self.assertEqual((status, output), (1, ""))
        self.assertIn("ключ подключения", error.lower())
        self.client.assert_not_called()
        self.local_key.assert_not_called()
        self.assertFalse(self.link.exists())

    def test_check_verifies_a_link_without_opening_a_receipt_or_taking_the_service_lock(self):
        self.write_paired()
        saved = self.link.read_bytes()
        self.client.return_value.check.return_value = {"ok": True, "last_seen": 1700000000}
        status, output, error = self.run_cli("node-check")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Связь с центральной панелью подтверждена", output)
        self.assertIn("2023-11-14 22:13:20 UTC", output)
        self.client.assert_called_once_with(ORIGIN, NODE, LOCAL_KEY, None, allow_loopback=False)
        self.client.return_value.check.assert_called_once_with()
        self.client.return_value.cycle.assert_not_called()
        self.client.return_value.join.assert_not_called()
        self.prompt.assert_not_called()
        self.private.assert_not_called()
        self.lock.assert_not_called()
        self.assertEqual(self.link.read_bytes(), saved)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_check_reports_no_first_sample_separately_from_a_rejected_connection(self):
        self.write_paired()
        self.client.return_value.check.return_value = {"ok": True, "last_seen": None}
        status, output, error = self.run_cli("node-check")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Первый отчёт", output)
        self.client.return_value.check.side_effect = ValueError("Fleet отклонил запрос.")
        status, output, error = self.run_cli("node-check")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("отклонил запрос", error)
        self.lock.assert_not_called()

    def test_check_does_not_contact_a_node_with_an_incomplete_local_pair(self):
        self.write_paired(paired=False)
        status, output, error = self.run_cli("node-check")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("node-pair", error)
        self.client.assert_not_called()
        self.lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
