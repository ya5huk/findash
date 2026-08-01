"""Unit tests for the CA-bundle guard used by the HTTPS-fetching scripts.
Run: python3 scripts/lib/test_ssl_bootstrap.py"""
import os
import tempfile
import types
import unittest

import ssl_bootstrap as sb

BROKEN = types.SimpleNamespace(cafile=None, capath=None)
HEALTHY_FILE = types.SimpleNamespace(cafile="/somewhere/cert.pem", capath=None)
HEALTHY_DIR = types.SimpleNamespace(cafile=None, capath="/somewhere/certs")


class EnsureCaBundleTests(unittest.TestCase):
    def _bundle(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.remove, path)
        return path

    def test_respects_existing_ssl_cert_file(self):
        env = {"SSL_CERT_FILE": "/user/choice.pem"}
        self.assertIsNone(sb.ensure_ca_bundle(env, BROKEN, (self._bundle(),)))
        self.assertEqual(env["SSL_CERT_FILE"], "/user/choice.pem")

    def test_respects_existing_ssl_cert_dir(self):
        env = {"SSL_CERT_DIR": "/user/certs"}
        self.assertIsNone(sb.ensure_ca_bundle(env, BROKEN, (self._bundle(),)))
        self.assertNotIn("SSL_CERT_FILE", env)

    def test_noop_when_interpreter_store_is_usable(self):
        for paths in (HEALTHY_FILE, HEALTHY_DIR):
            env = {}
            self.assertIsNone(sb.ensure_ca_bundle(env, paths, (self._bundle(),)))
            self.assertEqual(env, {})

    def test_broken_store_picks_first_existing_bundle(self):
        env = {}
        real = self._bundle()
        picked = sb.ensure_ca_bundle(env, BROKEN, ("/no/such.pem", real))
        self.assertEqual(picked, real)
        self.assertEqual(env["SSL_CERT_FILE"], real)

    def test_empty_env_value_counts_as_unset(self):
        env = {"SSL_CERT_FILE": ""}
        real = self._bundle()
        self.assertEqual(sb.ensure_ca_bundle(env, BROKEN, (real,)), real)
        self.assertEqual(env["SSL_CERT_FILE"], real)

    def test_no_bundle_found_leaves_env_alone(self):
        env = {}
        self.assertIsNone(sb.ensure_ca_bundle(env, BROKEN, ("/no/such.pem",)))
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
