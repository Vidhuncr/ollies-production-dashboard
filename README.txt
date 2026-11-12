OLLIES PRODUCTION DASHBOARD — SUPABASE / RENDER VERSION

DATABASE MODES

1. Shared cloud mode
   Set SUPABASE_DATABASE_URL. All users and devices then use the same Supabase
   PostgreSQL database.

2. Local mode
   Leave SUPABASE_DATABASE_URL blank. The application uses plant_production.db
   on this computer for offline use and testing.


LOCAL START

Double-click "Start Dashboard.bat" or run:

  .\.venv\Scripts\python.exe app.py

Open:

  Main dashboard: http://127.0.0.1:8765/
  Team entry:    http://127.0.0.1:8765/entry
  TV dashboard:  http://127.0.0.1:8765/tv


SUPABASE SETUP

1. Create a Supabase project.
2. Open Connect and copy the Session Pooler connection string.
3. Copy .env.example to .env.
4. Put the connection string in SUPABASE_DATABASE_URL.
5. Set APP_ADMIN_EMAIL and a private APP_PASSWORD.
6. Double-click "Setup Supabase.bat". It installs the packages and copies the
   existing local data to the new Supabase database.

For manual package installation, run:

   .\.venv\Scripts\python.exe -m pip install -r requirements.txt

7. Start the app. Tables and indexes are created automatically.

To move the existing local database into a new empty Supabase project, run once:

  .\.venv\Scripts\python.exe migrate_sqlite_to_supabase.py

The migration stops safely if the target already has production records.


RENDER

The repository includes render.yaml. Connect a private GitHub repository as a
Render Blueprint and enter these secret environment variables:

  SUPABASE_DATABASE_URL
  APP_ADMIN_EMAIL
  APP_PASSWORD

Optional email variables:

  EMAIL_PROVIDER
  GMAIL_ADDRESS
  GMAIL_APP_PASSWORD
  GOOGLE_APPS_SCRIPT_URL
  GOOGLE_APPS_SCRIPT_SECRET

See GITHUB_RENDER_EMAIL_SETUP.md for the complete GitHub, Render, Supabase, and
Gmail instructions. DEPLOY_RENDER.md contains the shorter deployment reference.

RENDER FREE EMAIL LIMITATION

Render Free blocks Gmail SMTP ports. For Gmail notifications on the Free
service, use the Google Apps Script HTTPS relay described in
EMAIL_SETUP_APPS_SCRIPT.md. Local Gmail SMTP notifications still work with
EMAIL_PROVIDER=gmail_smtp.


LOGIN ROLES

Admin:
- Add, edit, and delete application data and users.

Manager:
- Add and edit operational records.

Team:
- Uses the simplified entry page to add records.


IMPORTANT

- Never commit .env or database passwords.
- Keep APP_PASSWORD private and unique.
- Use Supabase Session Pooler for Render unless the Supabase project has direct
  IPv4 database access.
- The /health endpoint verifies both the web service and its database connection.
