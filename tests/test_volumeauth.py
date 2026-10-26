"""Unit tests for volume provider auth resolution (connection -> DriverOpts)."""

import unittest

from kowan import volumeauth
from kowan.volumeauth import VolumeAuthError


class TestVolumeAuth(unittest.TestCase):
    def test_s3fs_injects_aws_keys(self):
        opts = volumeauth.apply_connection(
            "s3fs", "aws",
            {"accessKeyId": "AKIA", "secretAccessKey": "sk", "region": "eu-west-1"},
            {"bucket": "b"},
        )
        self.assertEqual(opts["AWSACCESSKEYID"], "AKIA")
        self.assertEqual(opts["AWSSECRETACCESSKEY"], "sk")
        self.assertEqual(opts["AWSDEFAULTREGION"], "eu-west-1")
        self.assertEqual(opts["bucket"], "b")  # existing option preserved

    def test_s3fs_does_not_override_explicit_region(self):
        opts = volumeauth.apply_connection(
            "s3fs", "aws",
            {"accessKeyId": "AK", "secretAccessKey": "sk", "region": "eu"},
            {"AWSDEFAULTREGION": "us-east-1"},
        )
        self.assertEqual(opts["AWSDEFAULTREGION"], "us-east-1")  # explicit wins

    def test_cifs_merges_credentials_into_o(self):
        opts = volumeauth.apply_connection(
            "cifs", "basic-auth",
            {"username": "me", "password": "pw"},
            {"o": "vers=3.0", "device": "//srv/share"},
        )
        # username/password appended to the existing comma-joined `o` string.
        self.assertIn("vers=3.0", opts["o"])
        self.assertIn("username=me", opts["o"])
        self.assertIn("password=pw", opts["o"])
        self.assertEqual(opts["device"], "//srv/share")

    def test_cifs_does_not_override_explicit_o_key(self):
        opts = volumeauth.apply_connection(
            "cifs", "basic-auth",
            {"username": "me", "password": "pw"},
            {"o": "username=explicit"},
        )
        self.assertIn("username=explicit", opts["o"])
        self.assertNotIn("username=me", opts["o"])  # explicit key not overridden

    def test_sshfs_injects_password(self):
        opts = volumeauth.apply_connection(
            "sshfs", "basic-auth", {"password": "pw"}, {"sshcmd": "u@h:/p"},
        )
        self.assertEqual(opts["password"], "pw")
        self.assertEqual(opts["sshcmd"], "u@h:/p")

    def test_sshfs_builds_sshcmd_from_host_path_and_username(self):
        # Host/path are typed; the username comes from the connection and is
        # prefixed to form the sshcmd. The host/path options are consumed.
        opts = volumeauth.apply_connection(
            "sshfs", "basic-auth",
            {"username": "me", "password": "pw"},
            {"host": "h.example.com", "path": "/data"},
        )
        self.assertEqual(opts["sshcmd"], "me@h.example.com:/data")
        self.assertEqual(opts["password"], "pw")
        self.assertNotIn("host", opts)
        self.assertNotIn("path", opts)

    def test_sshfs_without_username_omits_user_prefix(self):
        opts = volumeauth.apply_connection(
            "sshfs", "basic-auth", {"password": "pw"}, {"host": "h", "path": ""},
        )
        self.assertEqual(opts["sshcmd"], "h:")

    def test_wrong_connection_type_rejected(self):
        with self.assertRaises(VolumeAuthError):
            volumeauth.apply_connection("s3fs", "basic-auth", {}, {})
        with self.assertRaises(VolumeAuthError):
            volumeauth.apply_connection("cifs", "aws", {}, {})

    def test_provider_without_auth_rejected(self):
        # nfs/tmpfs/local never reference a connection.
        with self.assertRaises(VolumeAuthError):
            volumeauth.apply_connection("nfs", "basic-auth", {}, {})


if __name__ == "__main__":
    unittest.main()
