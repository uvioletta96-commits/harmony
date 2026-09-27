"""Everything a PythonAnywhere deployment needs, in one place.

Written to be read top to bottom and followed without knowing Linux. The order
matters: the account has to exist before a console exists, the virtualenv before
the requirements, and the web app before anything is reachable.
"""

# =============================================================================
# PythonAnywhere deployment
# =============================================================================
#
# The free tier is enough for this application and needs no card. What it does
# not have, and what that costs:
#
#   * No HTTPS. The URL is plain HTTP, so the session cookie cannot be marked
#     `Secure` - the browser would refuse to send it and sign-in would be
#     impossible. The `preview` profile accepts this and warns on every start.
#   * No WebSockets. Chat and live counters update when a page loads rather than
#     instantly. The app degrades to this on its own when the message queue is
#     unavailable, so nothing breaks.
#   * No background workers. Celery tasks are enqueued and not executed, so
#     verification and notification emails are not sent. Accounts are made by
#     hand instead.
#   * SQLite. Fine until a redeploy, which resets it. `docker-compose.yml` with
#     PostgreSQL is the real answer; this is a way to have the site online today.
#
# Steps, in order. Each is a few clicks; the console steps are copy and paste.

# -----------------------------------------------------------------------------
# 1. Create the account
# -----------------------------------------------------------------------------
#
#   https://www.pythonanywhere.com/signup/
#
# A username becomes your address: <username>.pythonanywhere.com. Free accounts
# do not ask for a card. Confirm the email before going further - the console
# will not activate without it.
#
# Ask an adult to agree to the terms with you. It is an account, and it is in
# your name or theirs - either is fine, as long as whoever signed up can get
# back in to reset the password.

# -----------------------------------------------------------------------------
# 2. Open a console and install the requirements
# -----------------------------------------------------------------------------
#
# Web tab -> "Consoles" -> the blue "Console" button. A terminal opens.
#
# Find the virtualenv Python. The free tier has one at /usr/bin/python3, and the
# create_app command below uses it:
#
#   pip3 install --user boto3
#
# Then upload the project. Either:
#
#   * Console -> "Files" -> drag the project folder in, renamed so that
#     `backend/` and `deploy/pythonanywhere_wsgi.py` land side by side; or
#   * a Bash console:
#
#       cd ~
#       git clone <your-remote> harmony        # if you have put it in git
#       mv harmony/* harmony/                   # or unpack an upload
#
# Check that the layout is right - the WSGI file needs `backend/` beside it:
#
#   ls backend/app/__init__.py deploy/pythonanywhere_wsgi.py
#
# Install the dependencies into the virtualenv:
#
#   pip3 install --user -r ~/harmony/backend/requirements.txt
#
# That list is long and includes a WebSocket server and an event loop library
# that are not needed here. If it fails, install the short list instead - this is
# everything the app actually imports on this host:
#
#   pip3 install --user Flask Werkzeug Jinja2 Flask-SQLAlchemy SQLAlchemy \
#       PyJWT bcrypt itsdangerous email-validator bleach Pillow \
#       python-dotenv prometheus-client redis celery
#
# Create the database and its schema:
#
#   cd ~/harmony
#   flask --app wsgi:application init-db
#   # if that command does not exist, use the CLI directly:
#   python3 -c "import sys; sys.path.insert(0,'backend'); \
#       from app import create_app; \
#       from app.extensions import db; \
#       a=create_app(); \
#       c=a.app_context(); c.push(); \
#       db.create_all(); print('schema created')"
#
# Set the signing key. Generate a fresh one and keep it - changing it signs
# everyone out:
#
#   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
#
# Put that value in the web tab's environment variables as SECRET_KEY.

# -----------------------------------------------------------------------------
# 3. Point a web app at it
# -----------------------------------------------------------------------------
#
# Web tab -> "Web" -> "Add a new web app" -> choose the free manual setup ->
# Python 3.x, then Edit the WSGI configuration file.
#
# Replace the whole contents of that file with:
#
#     import sys
#     sys.path.insert(0, '/home/<username>/harmony/deploy')
#     from pythonanywhere_wsgi import application
#
# Save and reload. The green "Reload" button restarts the app.
#
# Add these under "Environment variables" in the web app's settings:
#
#   APP_ENV            preview
#   SECRET_KEY         the value you generated
#   SERVE_FRONTEND     1
#   SECURE_COOKIE      false
#   MAIL_ENABLED       0
#   REDIS_URL          (leave empty)
#   SOCKETIO_MESSAGE_QUEUE  (leave empty)
#
# Static files: the app serves its own frontend when SERVE_FRONTEND=1, so the
# "Static files" mapping is not needed. If you would rather serve them from
# nginx, map /static/ to ~/harmony/frontend/ and /site.webmanifest to the same
# directory.

# -----------------------------------------------------------------------------
# 4. Make an account, by hand
# -----------------------------------------------------------------------------
#
# With mail disabled nobody can complete a signup, because the link to verify an
# address only ever goes out by email. Create the first account from a console:
#
#   cd ~/harmony
#   python3 -c "import sys; sys.path.insert(0,'backend'); \
#       from app import create_app; \
#       from app.extensions import db; \
#       from app.models import User; \
#       a=create_app(); c=a.app_context(); c.push(); \
#       u=User(username='yourname', email='you@example.com', \
#              password_hash='PLACEHOLDER', email_verified=True, status='active'); \
#       db.session.add(u); db.session.commit(); \
#       from app.services import auth_service; \
#       u.password_hash=auth_service.hash_password('choose-a-password'); \
#       db.session.commit(); print('created', u.username)"
#
# Sign in with that password. Other accounts can be added the same way until a
# real mail provider is configured.

# -----------------------------------------------------------------------------
# 5. If something does not work
# -----------------------------------------------------------------------------
#
# The error is in the web app's "Error log" tab, and it names the file and line.
# A failure to import usually means a dependency is missing; `pip3 install
# --user` fixes it and a reload picks it up. A 500 that mentions "Invalid
# configuration" means an environment variable above is wrong, and the message
# names which one.
#
# Check it is up:
#
#   curl -s https://<username>.pythonanywhere.com/healthz
