# Deployment

## Vercel (serverless)

`vercel.json` and `api/index.py` make the build work. Read the limits below
before treating this as a production target - two of them are not degradations
you can configure away.

### Why the entrypoint had to be named

Vercel finds a Python application by scanning a few default locations for a
module-level variable called `app`. This repository had two matches and could
not choose between them:

* `backend/wsgi.py` - the real entrypoint
* `backend/tests/conftest.py` - a **pytest fixture** named `app`

So the build stopped with "No Flask entrypoint found". `api/index.py` is now the
only thing Vercel has to look at, and it is a shim: it puts `backend/` on the
path and re-exports the object `backend/wsgi.py` already builds, so there is
still exactly one place where the application is constructed.

### Required environment variables

The production profile refuses to boot without these, by design. Generate a
secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

| Variable | Value | Why |
| --- | --- | --- |
| `APP_ENV` | `production` | selects the profile |
| `SECRET_KEY` | 48+ random chars | session and token signing |
| `DATABASE_URL` | `postgresql://...` | SQLite cannot work here; see below |
| `SECURE_COOKIE` | `true` | TLS terminates at Vercel's edge, so this is correct |
| `MAIL_ENABLED` | `true` | verification mail is required for signup |
| `MAIL_BACKEND` | `smtp` | |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD` | provider values | Resend, Mailgun, SES, any SMTP |
| `REDIS_URL` | `rediss://...` | optional but recommended; the app runs without it |
| `CORS_ORIGINS` | your domain | if the API is called from another origin |
| `VERCEL` | set automatically | marks the platform; see `on_serverless()` |

### Media storage

`app/services/storage.py` picks a backend from configuration. The default is a
directory on the machine, which is why a fresh checkout works with no account and
no network. Setting `S3_BUCKET` switches to any S3-compatible bucket.

Keys are identical in both - `<yyyy>/<mm>/<user_id>/<sha256>` - and the key is
what goes in the database, so moving between backends is a configuration change
and not a data migration. Existing uploads keep working if the bucket is
populated by copying `UPLOAD_DIR` into it with the key layout preserved.

| Variable | Meaning |
| --- | --- |
| `S3_BUCKET` | Bucket name. Setting this is what selects the backend. |
| `S3_REGION` | `auto` for R2 and Spaces, `us-east-1` for S3 |
| `S3_ENDPOINT_URL` | Omit for AWS S3; set for R2, Spaces, B2, MinIO |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Credentials |
| `S3_PUBLIC_BASE_URL` | Public base for media URLs, ideally a CDN in front |
| `S3_KEY_PREFIX` | Optional prefix, e.g. a staging bucket under one account |
| `S3_PRESIGN_TTL_SECONDS` | Used only when there is no public base |

`boto3` is not a declared dependency. Install it when you turn the backend on:

    pip install "boto3>=1.35,<2"

Two properties worth knowing. Uploaded objects are sent with a one-year
`immutable` cache header, which is safe because the key is a content hash and
the bytes at it never change. And if `S3_PUBLIC_BASE_URL` is unset the service
hands out presigned URLs instead, which **expire** - fine for a private bucket
while you develop, wrong for a URL stored permanently in a post. Set the public
base.

### What does not work here, and why

**Uploaded files disappear.** `UPLOAD_DIR` is a path on the container's own
filesystem. Every function instance has a separate one, so a photo one person
uploads is invisible to everyone else and gone at the next cold start. This is
not a setting. Serving media needs object storage - an S3-compatible bucket, with
the upload service writing a key instead of a file. The content-addressed
storage key already in `upload_service.py` maps onto that without changing the
schema.

**Realtime does not work.** A serverless function is frozen between
invocations and is killed at a fixed timeout, so a WebSocket cannot outlive one.
`on_serverless()` detects this and leaves the Socket.IO middleware unattached, so
the client's attempt fails immediately instead of hanging. Chat, notifications
and live counters fall back to whatever the page does on load.

**Celery workers never run.** Background jobs - mail delivery, media
processing, moderation - are enqueued to a broker and picked up by a separate
long-lived process. Vercel cannot host one, so anything enqueued is only
executed if a worker is running somewhere else.

**SQLite cannot be used.** The filesystem is read-only apart from `/tmp`, and
ephemeral besides. `ProductionConfig` enforces this: it will not start on
anything but `postgres://`.

### What does work

The API, authentication, posting, the feed, profiles, search, moderation,
comments, settings, GDPR endpoints, and the static frontend. As a preview
environment, with a managed Postgres and a demo account, this is usable.

## A host that fits the whole application

Render, Railway or Fly.io run a long-lived container, which is what the
WebSocket transport, the Celery workers and a persistent disk all assume. The
`wsgi.py` docstring has the gunicorn command. With object storage for media and
a managed Redis, that configuration runs the application as written, with
nothing disabled.
