import io
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import neb_setup as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)

    def test_attachment_only_selects_detected_pci_and_the_matching_pack(self):
        store = setup.Setup(self.path, discover=lambda: [
            {"device": "0000:03:00.0", "vendor": "amd", "edge": 44, "hot": 55},
            {"device": "0000:84:00.0", "vendor": "nvidia", "edge": 45}])
        with patch.object(store, "paths", return_value=(PurePosixPath("/safe/binary"), PurePosixPath("/safe/dataset"))):
            got = store.worker_configs(["0000:03:00.0"], [])
            self.assertEqual([(w["device"],w["adapter"]) for w in got], [("0000:03:00.0", "suprminer-opencl")])
            with self.assertRaises(ValueError):
                store.worker_configs(["0000:45:00.0"], [])
            with self.assertRaises(ValueError):
                store.worker_configs(["0000:03:00.0"], [{"device":"0000:03:00.0"}])

    def test_unverified_download_has_no_accepted_path(self):
        a = setup.Asset("https://github.com/official/example", sha256(b"official").hexdigest(), 8)
        dest = self.path / "download"
        with patch.object(setup, "opener", return_value=io.BytesIO(b"modified")):
            with self.assertRaisesRegex(ValueError,"контрольная сумма"):
                setup.download(a, dest)
        self.assertFalse(dest.exists())
        with patch.object(setup, "opener", return_value=io.BytesIO(b"official")):
            setup.download(a, dest)
        self.assertEqual(dest.read_bytes(), b"official")
