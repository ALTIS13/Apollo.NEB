import io
import unittest
from threading import Event
from unittest.mock import Mock, patch

from neb_devfee import DevFeePolicy


def response(body, status=200):
    stream = io.BytesIO(body)
    stream.status = status
    return stream


class DevFeePolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 25.0
        clock = patch("neb_devfee.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        transport = patch("neb_devfee.HTTPSConnection")
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.connection = self.transport.return_value
        self.policy = DevFeePolicy()

    def poll(self, *responses):
        states = []
        stop = Mock(spec=Event)
        stop.is_set.return_value = False
        self.connection.getresponse.side_effect = responses

        def wait(seconds):
            self.assertEqual(seconds, 60)
            states.append(self.policy.state())
            if len(states) == len(responses):
                return True
            self.now += seconds
            return False

        stop.wait.side_effect = wait
        self.policy.loop(stop)
        return states

    def test_initial_state_is_unavailable_without_network_request(self):
        self.assertEqual(self.policy.state(), "unavailable")
        self.transport.assert_not_called()

    def test_stopped_worker_does_not_contact_creator_service(self):
        stop = Event()
        stop.set()
        self.policy.loop(stop)
        self.transport.assert_not_called()

    def test_fetches_official_policy_immediately_without_credentials(self):
        self.assertEqual(self.poll(response(b'{"paused":true}')), ["paused"])
        self.transport.assert_called_once_with("neb.apollot.ru", timeout=4)
        self.connection.request.assert_called_once_with(
            "GET", "/devfee/policy", headers={"Cache-Control": "no-cache"})
        self.connection.close.assert_called_once()

    def test_next_polls_resume_pause_and_fail_closed(self):
        self.assertEqual(
            self.poll(response(b'{"paused":true}'), response(b'{"paused":false}'),
                      TimeoutError(), response(b'{"paused":false}')),
            ["paused", "enabled", "unavailable", "enabled"],
        )

    def test_unrefreshed_enabled_snapshot_expires_at_120_seconds(self):
        self.poll(response(b'{"paused":false}'))
        self.now += 119.9
        self.assertEqual(self.policy.state(), "enabled")
        self.now += 0.1
        self.assertEqual(self.policy.state(), "unavailable")
        self.transport.assert_called_once()

    def test_rejects_malformed_policy_replies_and_extra_fields(self):
        for invalid in [
            b"null",
            b"false",
            b'["paused",false]',
            b"{}",
            b'{"paused":"false"}',
            b'{"paused":0}',
            b'{"paused":false,"paused":true}',
            b'{"paused":false,"basis_points":110}',
            b'{"paused":false,"wallet":"unexpected"}',
            b'{"paused":',
        ]:
            with self.subTest(reply=invalid):
                self.assertEqual(self.poll(response(invalid)), ["unavailable"])

    def test_redirect_is_unavailable_without_following_it(self):
        self.assertEqual(self.poll(response(b'{"paused":false}', status=302)), ["unavailable"])
        self.connection.request.assert_called_once()

    def test_rejects_body_larger_than_policy_limit(self):
        self.assertEqual(self.poll(response(b'{"paused":false}' + b" " * 1024)), ["unavailable"])


if __name__ == "__main__":
    unittest.main()
