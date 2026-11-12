# Gmail Notifications with Google Apps Script

Render Free calls a private Google Apps Script web app over HTTPS. The web app
sends production notifications from the Google account that owns and deploys
the script. No local database, backup, or production file is sent to Apps Script.

## 1. Create a separate Apps Script project

1. Open https://script.google.com.
2. Select **New project**.
3. Rename it `Ollies Production Mail`.
4. Replace the contents of `Code.gs` with `google_apps_script_email.gs` from
   this repository and save it.

Keep this project separate from the existing Task Desk and EHS mail projects.

## 2. Create and store the shared secret

In a PowerShell terminal, generate a random secret:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
[Convert]::ToBase64String($bytes)
```

Copy the result into a password manager. Do not place it in GitHub or source code.

In Apps Script:

1. Open **Project Settings**.
2. Under **Script properties**, select **Add script property**.
3. Property: `WEBHOOK_SECRET`
4. Value: the generated secret.
5. Save the property.

## 3. Deploy the web app

1. Select **Deploy > New deployment**.
2. Choose **Web app**.
3. Description: `Ollies Production Email`.
4. Execute as: **Me**.
5. Who has access: **Anyone**.
6. Select **Deploy** and approve the requested send-email permission.
7. Copy the `/exec` Web app URL. Do not use the `/dev` test URL.

The endpoint is protected by the `WEBHOOK_SECRET`; requests with a missing or
incorrect secret are rejected.

## 4. Configure Render

In the Render web service, open **Environment** and add:

```text
EMAIL_ENABLED=true
EMAIL_PROVIDER=google_apps_script
GOOGLE_APPS_SCRIPT_URL=the-private-exec-web-app-url
GOOGLE_APPS_SCRIPT_SECRET=the-same-private-secret
```

Choose **Save, rebuild, and deploy**. Store the URL and secret only in Render's
secret environment settings.

## 5. Test the notification

1. In the dashboard Backend page, add your email as an active contact.
2. Create a small test order and select that contact.
3. Record a partial production or tint entry.
4. Confirm the email contains the S#, category, color, batch number, produced
   amount, and amount still needed.
5. In Apps Script, check **Executions** if the email is not received.

Google currently documents daily email-recipient quotas of 100 for consumer
accounts and 1,500 for Google Workspace accounts. Quotas are per user and may
change. MailApp also reports its remaining daily recipient quota.

## Updating the Apps Script later

After changing `Code.gs`, select **Deploy > Manage deployments**, edit the active
deployment, select **New version**, and deploy it. Keep the existing `/exec` URL.
