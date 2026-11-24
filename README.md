# Daybook

Daybook is a job book for a small business that takes on all kinds of work: a software project, hanging a light, a light HVAC call, a stretch of yard work, or an errand. Staff sign in, schedule the job, price time and materials, and record cash, check, or card payments. An admin sets up people, the shop profile, and backups.

The app is a Python web service (FastAPI, server-rendered HTML, Postgres) packaged with Docker Compose. Card charges go through Stripe Checkout. The card number never reaches this app.

## Run with Docker

Docker Compose starts Postgres and the web app. The site listens on `http://localhost:8055` from this computer only. The same pages are laid out for iPhone Safari out on a job and for Chrome on a Mac back at the desk.

Copy these commands to get a local copy running:

```bash
git clone https://github.com/nibrocsolutions/handyman-app.git
cd handyman-app
cp .env.example .env
docker compose up --build
```

Then open `http://localhost:8055`. Renaming the GitHub repository does not change anything inside the app. After a rename, clone the new address and `cd` into that folder. GitHub keeps the old address working as a redirect.

The first launch creates an admin and, outside production, a fictional sample shop so the screens are not empty.

| | |
|---|---|
| Admin | `owner@daybook.local` / `change-this-password` |
| Sample staff | `sam@daybook.local` / `sample-tech-password` |
| Sample shop | Northline |

Change both passwords from **Account** (and the admin password in `.env` before any shared deploy) before you rely on the data. Sample rows are marked and can be removed from **Backup**.

`SEED_DEMO` defaults to `true` in Compose so a new shop can click around. Set `SEED_DEMO=false` when you do not want sample customers.

## Run without Docker

Python 3.12. SQLite is used when `DATABASE_URL` is unset, which is enough for a local look.

```bash
python3 -m pip install -r requirements.txt
ENV=development SEED_DEMO=true python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8055
```

Tests:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

## Who can do what

There is no public sign-up. An admin creates every account.

Each job is tagged house, yard, HVAC, software, errand, or other, and the job list can be filtered by that tag. Time, materials, and other charges are priced on the job. Time can be taxed or left untaxed.

**Admin** can do everything staff can, plus users, shop settings, JSON and CSV backup, refunds, and deleting customers or jobs.

**Staff** can run assigned work: status, line items, internal notes, and cash, check, or other payments. Staff cannot open the admin desk, refund a payment, or delete a job.

The last active admin cannot be demoted or deactivated.

## Payments

Money is stored in cents. Each job keeps its own tax rate. Time can be taxed or left untaxed, copied from the shop when the job is opened.

Cash, check, and other payments work with no Stripe account. To take cards, set these in the environment (never in the database or the page):

```bash
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
APP_BASE_URL=https://shop.example
```

Use a secret key (`sk_` or restricted `rk_`, test or live) and a webhook signing secret. A publishable key is not required because Checkout is hosted by Stripe.

Point a Stripe webhook at `https://your-host/webhooks/stripe` for:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.expired`
- `checkout.session.async_payment_failed`

Local forwarding:

```bash
stripe listen --forward-to localhost:8055/webhooks/stripe
```

**Collect card payment** sends the customer to `https://checkout.stripe.com`. The balance is marked paid when Stripe reports the session paid, either on the return URL or through the webhook. A pending Checkout link is reused for about a day so a double-click does not open two charges. Test card: `4242 4242 4242 4242`.

Refunds of card payments call Stripe. Cash and check refunds update the ledger only. The smallest card charge is $0.50 and the largest single Checkout in this app is $100,000.

## Backup

**Backup** exports JSON (customers, jobs, lines, notes, payments, and users without password hashes) or CSV for jobs, customers, and payments. Import merges by id. It does not replace passwords and it does not wipe the shop. CSV cells that start with `=`, `+`, `-`, or `@` are prefixed so a spreadsheet does not treat them as formulas.

## Production

Put TLS in front of the app and set:

- `ENV=production` (refuses `SEED_DEMO`, a short `SESSION_SECRET`, and a weak first admin password)
- `SESSION_SECRET` to a unique value of at least 32 characters
- `ADMIN_EMAIL` and a strong `ADMIN_PASSWORD` before the first boot
- `DATABASE_URL=postgresql+psycopg://...`
- `POSTGRES_PASSWORD` to something other than the Compose default
- `APP_BASE_URL` to the public `https://` origin
- `SEED_DEMO=false`
- Stripe live keys only after a test-mode payment has succeeded

The session cookie is `Secure` when `ENV=production`. Compose publishes port 8055 on `127.0.0.1` so a reverse proxy on the same machine is the intended public entry.

## Layout

```
app/            FastAPI app, templates, and static files
app/services/   totals, job ledger, Stripe, backup
tests/          pytest against a temporary SQLite file
Dockerfile      non-root image, uvicorn on port 8055
docker-compose.yml
```
