# GitHub, Render, Supabase, and Gmail Setup

This guide keeps database files, backups, passwords, and `.env` files off GitHub.

## 1. Confirm Supabase first

1. Create a Supabase project.
2. In Supabase, open **Connect > Session pooler**.
3. Double-click `Setup Supabase.bat` in the application folder.
4. Paste the connection string into `.env` after `SUPABASE_DATABASE_URL=`.
5. Set `APP_ADMIN_EMAIL` and `APP_PASSWORD`.
6. Save and close Notepad. The setup utility copies the existing local data to
   the empty Supabase database.

Do not paste `.env`, database passwords, Gmail passwords, or connection strings
into GitHub.

## 2. Create a private GitHub repository

1. Sign in at https://github.com.
2. Click **New repository**.
3. Name it `ollies-production-dashboard`.
4. Select **Private**.
5. Do not add a README, `.gitignore`, or license on GitHub because the local
   project already contains these files.
6. Click **Create repository**.
7. Copy the HTTPS repository URL. It looks like:

   `https://github.com/YOUR-USERNAME/ollies-production-dashboard.git`

## 3. Push the prepared local repository

Open PowerShell in `D:\GDB\Plant Production` and run:

```powershell
git remote add origin https://github.com/YOUR-USERNAME/ollies-production-dashboard.git
git push -u origin main
```

GitHub might open a browser sign-in window. Sign in to authorize Git on this
computer. Never type a GitHub password directly into the PowerShell window.

Before every future push:

```powershell
git status
git add .
git commit -m "Describe the change"
git push
```

The included `.gitignore` excludes `.env`, virtual environments, SQLite files,
database backups, dumps, logs, and editor settings.

## 4. Deploy the Free Render web service

1. Sign in at https://dashboard.render.com.
2. Click **New > Blueprint**.
3. Connect GitHub and select the private `ollies-production-dashboard` repository.
4. Render detects `render.yaml` in the repository root.
5. Enter the requested secret values:
   - `SUPABASE_DATABASE_URL`: the complete Supabase Session Pooler URL.
   - `APP_ADMIN_EMAIL`: the administrator login email.
   - `APP_PASSWORD`: a new long, private production password.
6. Deploy the Blueprint.
7. Wait until the service shows **Live** and `/health` passes.
8. Open the assigned `https://...onrender.com` address.

Supabase stores the permanent data, so Render restarts and redeployments do not
erase production records. To enable email on the Free plan, follow
`EMAIL_SETUP_APPS_SCRIPT.md` after the first successful deployment.

## 5. Gmail notifications locally

The application already emails selected order recipients whenever production or
tinting is recorded. The email says whether the order is partial or complete and
includes the batch amount and the quantity or gallons still needed.

1. Turn on 2-Step Verification for the Gmail account.
2. Create a 16-character Google App Password.
3. In the local `.env`, set:

```text
EMAIL_ENABLED=true
EMAIL_PROVIDER=gmail_smtp
GMAIL_ADDRESS=your-account@gmail.com
GMAIL_APP_PASSWORD=your-16-character-app-password
```

4. Restart `Start Dashboard.bat`.
5. In **Backend**, add the people who may receive notifications.
6. When entering an order, select the required email recipients.

Never use the normal Gmail account password in this application.

## 6. Enable Gmail after upgrading Render to Pro

Render Free blocks outbound Gmail SMTP ports. After upgrading the web service to
a paid instance:

1. Open the Render service and select **Environment**.
2. Change `EMAIL_ENABLED` to `true`.
3. Add `GMAIL_ADDRESS` with the Gmail address.
4. Add `GMAIL_APP_PASSWORD` with the 16-character App Password.
5. Choose **Save, rebuild, and deploy**.
6. Record a small test production batch for an order with your email selected.
7. Confirm the partial/complete production email is received.

If the Google account password is changed, Google revokes existing App Passwords;
create a new one and update the Render secret.

While remaining on Render Free, use the Google Apps Script HTTPS relay described
in `EMAIL_SETUP_APPS_SCRIPT.md`. It uses a private shared webhook secret instead
of an App Password.

## 7. Free-plan expectations

- The service sleeps after 15 minutes without traffic and can take about one
  minute to wake up.
- Gmail SMTP cannot send from the Free web service.
- Google Apps Script notifications can send from the Free service over HTTPS.
- Data remains safe because it is stored in Supabase, not Render's temporary disk.
- After moving to a paid Render instance, the service no longer has the Free
  SMTP restriction and Gmail notifications can be enabled as described above.
