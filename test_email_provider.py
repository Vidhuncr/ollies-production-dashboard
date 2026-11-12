import json

import app_sqlite_backup as app


class FakeResponse:
    status = 200

    def read(self):
        return json.dumps({"ok": True, "remainingDailyQuota": 99}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


captured = {}


def fake_urlopen(request, timeout=0):
    captured["url"] = request.full_url
    captured["headers"] = {key.lower(): value for key, value in request.header_items()}
    captured["payload"] = json.loads(request.data.decode("utf-8"))
    captured["timeout"] = timeout
    return FakeResponse()


original = {
    "EMAIL_ENABLED": app.EMAIL_ENABLED,
    "EMAIL_PROVIDER": app.EMAIL_PROVIDER,
    "GOOGLE_APPS_SCRIPT_URL": app.GOOGLE_APPS_SCRIPT_URL,
    "GOOGLE_APPS_SCRIPT_SECRET": app.GOOGLE_APPS_SCRIPT_SECRET,
    "urlopen": app.urlopen,
}

try:
    app.EMAIL_ENABLED = True
    app.EMAIL_PROVIDER = "google_apps_script"
    app.GOOGLE_APPS_SCRIPT_URL = "https://script.google.com/macros/s/test-deployment/exec"
    app.GOOGLE_APPS_SCRIPT_SECRET = "test-shared-secret"
    app.urlopen = fake_urlopen
    result = app.deliver_notification_email(
        ["recipient@example.com"], "S12345 - Partial production update", "Need to produce: 5 gallons"
    )
    assert result == "Email sent to 1 recipient(s)."
    assert captured["url"] == app.GOOGLE_APPS_SCRIPT_URL
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["payload"] == {
        "secret": "test-shared-secret",
        "to": ["recipient@example.com"],
        "subject": "S12345 - Partial production update",
        "body": "Need to produce: 5 gallons",
    }
    assert captured["timeout"] == 20

    app.EMAIL_ENABLED = False
    assert "disabled" in app.deliver_notification_email(["recipient@example.com"], "Test", "Test").lower()
finally:
    for key, value in original.items():
        setattr(app, key, value)

print("PASS: Google Apps Script HTTPS and disabled email notification modes")
