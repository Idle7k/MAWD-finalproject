# High Level Design — Campus Split (simple version)

## 1. Purpose

Campus Split is a small web application for sharing campus expenses between
students. A group of students buys something from a campus vendor (canteen,
xerox shop, laundry). The total is split between the students who took part.
Each student is expected to pay their own share directly to the vendor's UPI
id. When one student pays another student's share, that becomes a debt between
the two students, which can later be repaid in part or in full.

SO no need to go  scan the qr the open up aplitwise to check dues or wait for your
friends to pay you back later.

## 2. Scope

In scope:

- Register and log in as a student; log in as an admin.
- Browse active vendors and their UPI details.
- Create an expense for a vendor, split equally or by custom amounts.
- Record payment of an expense share to the vendor (full or partial).
- See three separate balances: people who owe you, people you owe, vendor
  payments still due.
- Repay a peer debt (full or partial).
- Admin: add, edit, activate and deactivate vendors.

Out of scope (deliberately removed to stay within the syllabus):

- Groups, percentage splits, payment confirmation workflows.
- Real payment verification. A payment is user-reported.
- Notifications, file uploads, reporting exports, multi-campus tenancy.

## 3. Actors

| Actor | Description | Main actions |
|---|---|---|
| Student | A registered user with role `student` | Create expenses, pay shares, view balances, repay debts |
| Admin | A user with role `admin` | Everything a student can do, plus vendor management |
| Vendor | External shop, not a user of the system | Receives money through its UPI id; stored as a row in `vendors` |
| UPI app | External app opened by a `upi://pay?...` link | Pre-fills the payee, note and amount |

## 4. System context

```
        +-------------+          HTTP (forms, cookies)          +----------------+
        |  Browser    | <-------------------------------------> |  FastAPI app   |
        | HTML + CSS  |                                         |  (main.py)     |
        +-------------+                                         +-------+--------+
              |                                                         |
              | upi://pay?... link (opens a UPI app)                    | sqlite3
              v                                                         v
        +-------------+                                         +----------------+
        |  UPI app    |                                         | campus_split.db|
        +-------------+                                         |  (6 tables)    |
                                                                +----------------+
```

The system has one process and one database file. The browser renders
server-side HTML; there is no separate front-end application and no API client.

## 5. Layered architecture

```
Presentation      templates/*.html, static/style.css
                  Jinja2 renders every page; forms post to the same app
---------------------------------------------------------------------------
Routing           @app.get / @app.post handlers in main.py
                  each handler validates input, calls helpers, renders a page
                  or redirects (303) after a successful write
---------------------------------------------------------------------------
Authentication    current_user()  reads the JWT from the cookie
                  require_user()  redirects to /login when not logged in
                  require_admin() returns 403 when role != 'admin'
---------------------------------------------------------------------------
Business logic    to_paise, equal_split, split_shares, outstanding_amount,
                  total_receivable/payable/vendor_due, vendor_form_problem
---------------------------------------------------------------------------
Data access       get_connection() -> sqlite3.Connection with row_factory
                  raw SQL: SELECT / INSERT / UPDATE / JOIN / SUM / COALESCE
---------------------------------------------------------------------------
Storage           campus_split.db (SQLite file, created on first start)
```

Why this shape:

- One module (`main.py`) keeps the whole request path visible in one file,
  which matches the level of the course.
- Helpers are plain functions, not a service layer, so a route reads top to
  bottom: validate, query, write, redirect.
- SQL stays in the route or in a named helper; there is no query builder.

## 6. Data model (overview)

Six tables. Full column detail is in `lld.md`.

```
users ──< expenses >── vendors
  │          │
  │          └──< expense_shares >── users
  │
  └──< debts >── users
         │
         └──< settlements >── users
```

- `users` — login identity and role.
- `vendors` — shops that can be paid, with a UPI id and an active flag.
- `expenses` — one purchase: creator, vendor, description, total in paise,
  split type.
- `expense_shares` — one row per student per expense: the amount owed and how
  much of it has already been paid to the vendor.
- `debts` — a peer debt created when one student pays another student's share.
- `settlements` — repayments against a debt; a debt's outstanding amount is
  its `amount` minus the sum of its settlements.

Two independent kinds of money exist and are never merged:

1. **Vendor due** — `sum(expense_shares.amount - expense_shares.paid)` for a
   student. Money the student still owes a shop.
2. **Peer debt** — `debts.amount - sum(settlements.amount)`. Money one
   student owes another student.

## 7. Key flows

### 7.1 Login

1. `GET /login` renders the form (or redirects to `/dashboard` if a valid
   cookie is already present).
2. `POST /login` looks the user up by username and verifies the password with
   `pbkdf2_hmac` and `compare_digest`.
3. On success a JWT `{uid, exp}` is written to an `httponly` cookie and the
   browser is redirected to `/dashboard` (303).
4. On failure the same page is rendered with "Wrong username or password."

### 7.2 Create an expense

1. `GET /expenses/new` shows the active vendors and all students.
2. `POST /expenses/new` validates, in order: description present, at least one
   participant, total is a valid positive amount, split adds up.
3. The expense row is inserted, then one `expense_shares` row per participant.
   For an equal split the leftover paise go to the first students so the shares
   always add up to the total exactly.
4. Redirect to the expense page with a message.

### 7.3 Pay a share to the vendor

1. On the expense page each unpaid share shows the UPI link and a form with an
   amount.
2. `POST /shares/{id}/pay` checks the amount is valid and not more than the
   unpaid part of the share.
3. `expense_shares.paid` is increased.
4. If the share belongs to the logged-in student, nothing else happens. If it
   belongs to someone else, one `debts` row is inserted: that student owes the
   payer. This is the only place peer debts are created.

### 7.4 Settle a peer debt

1. `/balances` lists debts where the student is creditor and where the student
   is debtor, each with its outstanding amount.
2. `POST /debts/{id}/settle` accepts only the debtor (403 otherwise) and only
   an amount up to the outstanding amount.
3. A `settlements` row is inserted; the debt is not edited, so its history stays
   intact.

### 7.5 Admin vendor management

`/admin/vendors` lists every vendor. `require_admin` protects all four admin
routes on the server, not only in the navigation. Creating and editing validate
name, category and the shape of the UPI id.

## 8. Security design

- Passwords: random 16-byte salt, `pbkdf2_hmac("sha256", ..., 100000)`, stored as
  `salt$hash`; comparison with `secrets.compare_digest`.
- Session: JWT signed with HS256, 1-day expiry, stored in an `httponly` cookie,
  cleared by `/logout`.
- Authorisation: every page except `/login` and `/register` depends on
  `require_user`; admin routes depend on `require_admin` and return 403.
- SQL: every query uses bound parameters (`?`), never string formatting.
- Money: integers only (paise), so no floating point rounding errors.
- Known limitation: the signing secret is a constant in `main.py`, and there is
  no CSRF token, which is acceptable for a local class project.

## 9. Error handling

- `HTTPException` is handled in one place.
- Status `303` is used internally by `require_user` to mean "not logged in";
  the handler turns it into a redirect to `/login`.
- Other statuses (403, 404) render `error.html` with the navigation still
  visible.
- Form problems never raise; the page is re-rendered with a message under the
  form, so the student keeps what they typed.

## 10. Deployment and configuration

- Run with `uvicorn main:app --reload` from the project folder.
- Tables and demo data are created at import time by `create_tables()` and
  `seed_demo_data()`; both are idempotent.
- No environment variables, no external services, no build step.
- To reset: stop the app, delete `campus_split.db`, start again.

## 11. Non-functional notes

| Concern | Approach |
|---|---|
| Correctness of money | integer paise end to end; leftover paise assigned deterministically |
| Consistency | a write is followed by `commit()` before the redirect |
| Readability | one file, sections marked with comment banners, helpers named after the rule they implement |
| Accessibility | semantic HTML, labels on every input, navigation present on error pages |
| Portability | only Python, SQLite and the packages in `requirements.txt` |
| Scalability | not a goal; single process, per-request connection |
