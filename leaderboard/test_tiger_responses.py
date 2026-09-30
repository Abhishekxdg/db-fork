"""Tiger HTTP response regressions; no hosted resources are created."""

import unittest
from unittest import mock

import requests

from dblib.tiger import REQUEST_TIMEOUT, TIGER_API_BASE, TigerToolSuite


class TigerResponses(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = TigerToolSuite.__new__(TigerToolSuite)
        self.suite.project_id = "project"
        self.suite.access_key = self.suite.secret_key = "test"
        self.suite._services = {"probe": ("fork", "password")}

    def test_empty_successful_delete_removes_branch(self) -> None:
        for status in (200, 202, 204):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code, response._content = status, b""
                self.suite._services["probe"] = ("fork", "password")
                with mock.patch("dblib.tiger.requests.request", return_value=response) as request:
                    self.suite._delete_branch_impl("probe", "")
                request.assert_called_once_with(
                    "DELETE", f"{TIGER_API_BASE}/projects/project/services/fork",
                    headers=self.suite._auth_header(), timeout=REQUEST_TIMEOUT,
                )
                self.assertNotIn("probe", self.suite._services)

    def test_json_and_errors_are_not_discarded(self) -> None:
        response = requests.Response()
        with mock.patch("dblib.tiger.requests.request", return_value=response):
            response.status_code, response._content = 202, b'{"service_id": "fork"}'
            self.assertEqual(self.suite._request("POST", "/fork"), {"service_id": "fork"})
            response.status_code, response._content = 500, b""
            with self.assertRaisesRegex(Exception, "Tiger API error 500"):
                self.suite._delete_branch_impl("probe", "")
            self.assertIn("probe", self.suite._services)
            response.status_code, response._content = 200, b"not JSON"
            with self.assertRaises(requests.exceptions.JSONDecodeError):
                self.suite._request("GET", "/fork")


if __name__ == "__main__":
    unittest.main()
