import unittest

from kowan.web.accesslog import redact


class RedactTests(unittest.TestCase):
    def test_token_is_masked(self):
        self.assertEqual(redact("/api/v1/hosts/local/events/ws?token=eyJabc.def&type=container"),
                         "/api/v1/hosts/local/events/ws?token=***&type=container")

    def test_other_params_untouched(self):
        self.assertEqual(redact("/api/v1/hosts?limit=20&tokenized=1"), "/api/v1/hosts?limit=20&tokenized=1")

    def test_oauth_code_masked(self):
        self.assertEqual(redact("/cb?state=x&code=abc"), "/cb?state=x&code=***")


if __name__ == "__main__":
    unittest.main()
