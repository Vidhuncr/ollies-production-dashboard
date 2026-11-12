# Publish Ollies Production Dashboard with Supabase and Render

## 1. Create the Supabase database

1. Create a project at https://supabase.com/dashboard.
2. Save the database password securely.
3. Open **Connect** at the top of the project.
4. Choose **Session pooler**. This is the IPv4-compatible connection method for a
   persistent Render web service.
5. Copy the complete PostgreSQL connection string and replace `[YOUR-PASSWORD]`
   with the URL-encoded database password.

The connection string is a secret. Never commit it to GitHub or paste it into a
public file.

## 2. Test Supabase locally

On the production computer, double-click `Setup Supabase.bat`. It opens `.env`,
installs the required packages using the application's own Python environment,
and runs the one-time migration.

For manual setup:

1. Copy `.env.example` to `.env`.
2. Set `SUPABASE_DATABASE_URL`, `APP_ADMIN_EMAIL`, and `APP_PASSWORD` in `.env`.
3. Install dependencies with the application's Python environment:

   `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`

4. Start the application:

   `.\.venv\Scripts\python.exe app.py`

The application creates its PostgreSQL tables and indexes automatically.

## 3. Copy the existing local data once

Keep `plant_production.db` in the application folder and run:

`.\.venv\Scripts\python.exe migrate_sqlite_to_supabase.py`

The migration refuses to continue if Supabase already contains production data.
It does not overwrite an active cloud database.

## 4. Push to GitHub

Push the application files to a private GitHub repository. Confirm `.env` and
database backup files are not committed.

## 5. Create the Render web service

1. In Render, choose **New > Blueprint** and connect the GitHub repository.
2. Render reads `render.yaml` and uses:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python app.py`
   - Health check: `/health`
3. Enter the secret environment variables when prompted:
   - `SUPABASE_DATABASE_URL`
   - `APP_ADMIN_EMAIL`
   - `APP_PASSWORD`
4. Deploy the service.

Render supplies `PORT`; the application listens on `0.0.0.0` automatically.

## 6. Open the dashboards

- Main application: `https://YOUR-SERVICE.onrender.com/`
- Team entry: `https://YOUR-SERVICE.onrender.com/entry`
- TV dashboard: `https://YOUR-SERVICE.onrender.com/tv`

All devices and networks will use the same data stored in Supabase.

## Gmail and Render Free

Render Free blocks Gmail SMTP ports. To send cloud notifications on the Free
service, configure the Google Apps Script HTTPS relay using
`EMAIL_SETUP_APPS_SCRIPT.md`. Local Gmail SMTP notifications can still be used.
After upgrading Render, Gmail App Password SMTP can also be enabled.

See `GITHUB_RENDER_EMAIL_SETUP.md` for the complete GitHub and Gmail steps.
