"""A fake webhook receiver for tests: records each request. No network."""

import json

HOOK_URL = "https://hooks.test/periodicool"


class FakeWebhook:
    def __init__(self, status=200):
        self.status = status
        self.calls = []
        self.fail_with = None

    def __call__(self, method, url, headers, body, timeout):
        if self.fail_with:
            raise self.fail_with
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json.loads(body)}
        )
        return self.status

    def events(self):
        return [c["json"]["eventType"] for c in self.calls]
