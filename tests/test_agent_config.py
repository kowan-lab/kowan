"""An agent never starts without a strong token: the token is the only thing
between the network and the Docker socket on that host."""

import unittest

from kowan.agent import config_problem
from kowan.config import AgentConfig


class TestAgentConfig(unittest.TestCase):
    def test_direct_mode_needs_a_strong_token(self):
        self.assertTrue(config_problem(AgentConfig(mode="agent", token="")))
        self.assertTrue(config_problem(AgentConfig(mode="agent", token="changeme")))
        self.assertEqual(config_problem(AgentConfig(mode="agent", token="a" * 64)), "")

    def test_edge_mode_needs_its_token(self):
        self.assertTrue(config_problem(AgentConfig(mode="edge", edge_token="")))
        self.assertEqual(config_problem(AgentConfig(mode="edge", edge_token="a" * 64)), "")


if __name__ == "__main__":
    unittest.main()
