"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/cli_common.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/api_client.py
"""

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pte_plugin_auto_annotation import api_client
from pte_plugin_auto_annotation.api_client import ApiSession
from pte_plugin_auto_annotation.cli_common import Connection, add_connection_args, connection_from_args


def connection(argv: list[str], env_file: str = "") -> Connection:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(env_file)
        parser = argparse.ArgumentParser()
        add_connection_args(parser)
        return connection_from_args(parser.parse_args(["--env", str(path), *argv]), parser)


CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in ("API_TOKEN", "API_USER", "API_PASSWORD", "API_BASE_URL")}


@mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
class ConnectionFromArgsTest(unittest.TestCase):
    def test_token_from_env_file_is_preferred_over_password(self):
        result = connection([], "API_TOKEN=pte_x\nAPI_USER=alice\nAPI_PASSWORD=pw\n")
        self.assertEqual(result, Connection("http://localhost:8000", token="pte_x"))

    def test_token_option_overrides_env_file(self):
        self.assertEqual(connection(["--token", "pte_cli"], "API_TOKEN=pte_x\n").token, "pte_cli")

    def test_password_without_token(self):
        result = connection([], "API_USER=alice\nAPI_PASSWORD=pw\n")
        self.assertEqual(result, Connection("http://localhost:8000", user="alice", password="pw"))

    def test_no_credentials_exits(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            connection([])


class ConnectTest(unittest.TestCase):
    def test_token_is_exchanged_for_a_session(self):
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {"sessionId": "sid"}
        with mock.patch.object(api_client.requests, "post", return_value=response) as post:
            session = ApiSession.connect(Connection("http://host/", token="pte_x"))
        self.assertEqual(session.session_id, "sid")
        self.assertEqual(post.call_args.args[0], "http://host/api/v1/auth/token-login")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer pte_x"})

    def test_password_connection_uses_login(self):
        sentinel = ApiSession("http://host", "sid")
        with mock.patch.object(ApiSession, "login", return_value=sentinel) as login:
            self.assertIs(ApiSession.connect(Connection("http://host", user="alice", password="pw")), sentinel)
        self.assertEqual(login.call_args.args[:3], ("http://host", "alice", "pw"))


if __name__ == "__main__":
    unittest.main()
