# Low Level Design — Campus Split (simple version)

Every item below refers to code that exists in `main.py`, `templates/` and
`static/style.css`. Nothing here is planned work; this describes the
implementation as written.

---

## 1. Module layout

`main.py` is one module, read top to bottom in these sections:

| Section | Contents |
|---|---|
| Module docstring and imports | `hashlib`, `secrets`, `sqlite3`, `datetime`, `pathlib`, `urllib.parse.quote`, `jwt`, FastAPI names |
| Constants | `BASE_DIR`, `DB_NAME`, `SECRET_KEY`, `ALGORITHM`, `TOKEN_MINUTES` |
| App setup | `app = FastAPI()`, static mount, `Jinja2Templates`, `rupees` filter registration |
| Database helpers | `get_connection`, `create_tables` |
| Passwords and tokens | `hash_password`, `verify_password`, `create_token` |
| Dependencies | `current_user`, `require_user`, `require_admin` |
| Money helpers | `to_paise`, `equal_split`, `split_shares`, `outstanding_amount`, `with_outstanding`, `sum_outstanding`, `total_receivable`, `total_payable`, `total_vendor_due`, `rupees`, `message_redirect` |
| Shared queries | `active_vendors`, `expenses_for` |
| Page helpers | `login_response`, `register_response`, `expense_form_response`, `vendor_form_response`, `vendor_form_problem` |
| Setup | `seed_demo_data()`, then `create_tables()` and `seed_demo_data()` called at import |
| Error handling | `@app.exception_handler(HTTPException)` — `http_exception_handler` |
| Routes | auth, dashboard, vendors, expenses, balances, admin — in that order |

`templates/` has 13 files: `base.html` plus 12 pages. `static/style.css` is the
only stylesheet.

---

## 2. Database schema

Connection helper:

```python
def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

`sqlite3.Row` lets a route use `row["name"]` and pass the row straight to a
template. Foreign keys are enforced, so an `expense_shares` row cannot point at
a missing expense or user.

### 2.1 `users`

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | user id |
| `username` | TEXT | UNIQUE, NOT NULL | login name |
| `password` | TEXT | NOT NULL | `salt$pbkdf2_hex` |
| `role` | TEXT | NOT NULL, default `'student'` | `student` or `admin` |
| `created_at` | TEXT | default CURRENT_TIMESTAMP | audit |

`UNIQUE(username)` is what makes duplicate registration fail with
`sqlite3.IntegrityError`, which the register route catches.

### 2.2 `vendors`

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | vendor id |
| `name` | TEXT | NOT NULL | shop name |
| `category` | TEXT | NOT NULL | e.g. `Food & Dining` |
| `description` | TEXT | nullable | short line shown on the card |
| `upi_id` | TEXT | NOT NULL | `shop@bank` |
| `is_active` | INTEGER | NOT NULL, default 1 | 0 hides it from students |

Only `is_active = 1` vendors appear in `active_vendors()`. Deactivated vendors
keep their expenses, so history is never broken.

### 2.3 `expenses`

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | expense id |
| `creator_id` | INTEGER | NOT NULL, FK `users(id)` | who recorded it |
| `vendor_id` | INTEGER | NOT NULL, FK `vendors(id)` | who is being paid |
| `description` | TEXT | NOT NULL | what it was for |
| `total` | INTEGER | NOT NULL | total in paise |
| `split_type` | TEXT | NOT NULL, default `'equal'` | `equal` or `custom` |
| `created_at` | TEXT | default CURRENT_TIMESTAMP | ordering |

### 2.4 `expense_shares`

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | share id |
| `expense_id` | INTEGER | NOT NULL, FK `expenses(id)` | parent expense |
| `user_id` | INTEGER | NOT NULL, FK `users(id)` | student who owes it |
| `amount` | INTEGER | NOT NULL | this student's share in paise |
| `paid` | INTEGER | NOT NULL, default 0 | paise already paid to the vendor |

Invariant: for one expense, `sum(amount)` equals `expenses.total`, and
`0 <= paid <= amount`.

### 2.5 `debts`

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | debt id |
| `debtor_id` | INTEGER | NOT NULL, FK `users(id)` | who owes |
| `creditor_id` | INTEGER | NOT NULL, FK `users(id)` | who is owed |
| `expense_id` | INTEGER | nullable, FK `expenses(id)` | expense that caused it |
| `amount` | INTEGER | NOT NULL | original debt in paise |
| `created_at` | TEXT | default CURRENT_TIMESTAMP | audit |

A debt is never edited after creation. Repayments live in `settlements`, so the
original amount and the history are both preserved.

### 2.6 `settlements`

| Column | Type | Constraints | Meaning |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | settlement id |
| `debt_id` | INTEGER | NOT NULL, FK `debts(id)` | debt being repaid |
| `payer_id` | INTEGER | NOT NULL, FK `users(id)` | who paid |
| `amount` | INTEGER | NOT NULL | paise repaid |
| `created_at` | TEXT | default CURRENT_TIMESTAMP | audit |

Invariant: `sum(settlements.amount for a debt) <= debts.amount`.

### 2.7 Reset

`DB_NAME` is `campus_split.db` next to `main.py`. The file is gitignored; delete
it and restart to rebuild an empty database with demo data.

---

## 3. Route table

`Auth` column: `public`, `user` (`require_user`), `admin` (`require_admin`).

| # | Method | Path | Handler | Auth | Success response |
|---|---|---|---|---|---|
| 1 | GET | `/` | `home` | public | 303 → `/dashboard` |
| 2 | GET | `/login` | `login_page` | public | `login.html`, or 303 → `/dashboard` if already logged in |
| 3 | POST | `/login` | `login` | public | 303 → `/dashboard` + cookie, or `login.html` with error |
| 4 | GET | `/register` | `register_page` | public | `register.html`, or 303 → `/dashboard` |
| 5 | POST | `/register` | `register` | public | 303 → `/dashboard` + cookie, or `register.html` with error |
| 6 | GET | `/logout` | `logout` | public | 303 → `/login`, cookie deleted |
| 7 | GET | `/dashboard` | `dashboard` | user | `dashboard.html` |
| 8 | GET | `/vendors` | `vendors_page` | user | `vendors.html` |
| 9 | GET | `/vendors/{vendor_id}` | `vendor_detail` | user | `vendor_detail.html`, 404 if missing |
| 10 | GET | `/expenses` | `expenses_page` | user | `expenses.html` |
| 11 | GET | `/expenses/new` | `new_expense_page` | user | `expense_new.html` |
| 12 | POST | `/expenses/new` | `create_expense` (async) | user | 303 → `/expenses/{id}?msg=...`, or form with error |
| 13 | GET | `/expenses/{expense_id}` | `expense_detail` | user | `expense_detail.html`, 404 if missing |
| 14 | POST | `/shares/{share_id}/pay` | `pay_share` | user | 303 → `/expenses/{id}?msg=...` |
| 15 | GET | `/balances` | `balances` | user | `balances.html` |
| 16 | POST | `/debts/{debt_id}/settle` | `settle_debt` | user | 303 → `/balances?msg=...` |
| 17 | GET | `/admin/vendors` | `admin_vendors` | admin | `admin_vendors.html` |
| 18 | GET | `/admin/vendors/new` | `new_vendor_page` | admin | `admin_vendor_form.html` |
| 19 | POST | `/admin/vendors/new` | `create_vendor` | admin | 303 → `/admin/vendors?msg=...` |
| 20 | GET | `/admin/vendors/{vendor_id}/edit` | `edit_vendor_page` | admin | `admin_vendor_form.html`, 404 if missing |
| 21 | POST | `/admin/vendors/{vendor_id}/edit` | `update_vendor` | admin | 303 → `/admin/vendors?msg=...` |
| 22 | POST | `/admin/vendors/{vendor_id}/toggle` | `toggle_vendor` | admin | 303 → `/admin/vendors?msg=...` |

Path parameters are typed (`vendor_id: int`, `expense_id: int`, `share_id: int`,
`debt_id: int`) so a non-numeric value is rejected by FastAPI before the
handler runs.

Form fields are declared with `Form("")` defaults rather than `Form(...)`, so an
empty field reaches the handler and gets a readable message instead of a raw
422 JSON response. The one exception is `vendor_id: int = Form(...)` on expense
creation, where the vendor is chosen from a list and must be present.

---

## 4. Function reference

### 4.1 Dependencies

```python
def current_user(request: Request) -> dict | None
```
Reads `token` from the cookie, decodes it with `jwt.decode(...,
algorithms=[ALGORITHM])`, loads the user by `id` and returns `dict(row)`.
Returns `None` on a missing cookie, an invalid/expired token, or a deleted user.
`jwt.PyJWTError` is caught; nothing is raised to the page.

```python
def require_user(user = Depends(current_user)) -> dict
```
Returns the user, or raises `HTTPException(status_code=303,
headers={"Location": "/login"})`. `303` is a private convention: the exception
handler turns it into a redirect.

```python
def require_admin(user = Depends(require_user)) -> dict
```
Raises `HTTPException(403, "Only an admin can manage vendors.")` when
`user["role"] != "admin"`.

### 4.2 Passwords and tokens

```python
def hash_password(password: str) -> str
```
16-byte hex salt from `secrets.token_hex(16)`; 100000 rounds of
`pbkdf2_hmac("sha256", ...)`; returns `"salt$hexdigest"`.

```python
def verify_password(password: str, saved: str) -> bool
```
Splits on the first `$`; returns `False` if the format is wrong; otherwise
recomputes the hash and compares with `secrets.compare_digest`.

```python
def create_token(user_id: int) -> str
```
`{"uid": user_id, "exp": now_utc + 1 day}` encoded with HS256.

### 4.3 Money

```python
def to_paise(text) -> int
```
Accepts `"210"`, `"210.5"`, `"210.50"`. Strips whitespace; splits on `.`;
pads/truncates the paise part to two digits; `int(rupees) * 100 + int(paise)`.
Raises `ValueError("Please enter an amount.")` for an empty string and
`ValueError("Amount must be more than zero.")` for zero or a negative value.
Non-numeric input raises the `int()` `ValueError` and is caught by callers.

```python
def equal_split(total_paise: int, number_of_people: int) -> list[int]
```
`base = total // n`, `remainder = total % n`. The first `remainder` students get
`base + 1`, the rest get `base`. Example: 21000 paise over 3 → `[7000, 7000,
7000]`; 10000 over 3 → `[3334, 3333, 3333]`. The sum always equals the total.

```python
def split_shares(form, participant_ids, total_paise, split_type) -> list[int]
```
For `equal`, returns `equal_split(total, len(participant_ids))`.
For `custom`, reads `form.get("custom_<user_id>")` for each participant in the
same order as `participant_ids`, converts each with `to_paise`, and raises
`ValueError("Every chosen student needs a custom amount.")` if any is missing or
invalid, or `ValueError("The custom amounts must add up to the total.")` if the
sum differs from `total_paise`. The returned list is positionally aligned with
`participant_ids`, which is how `create_expense` pairs shares with students.

```python
def outstanding_amount(conn, debt) -> int
```
`SELECT COALESCE(SUM(amount), 0) FROM settlements WHERE debt_id = ?`, subtracted
from `debt["amount"]`. `COALESCE` makes a debt with no settlements return 0
rather than `None`.

```python
def with_outstanding(conn, rows) -> list[dict]
```
Converts each `sqlite3.Row` to a dict and adds `outstanding`. Used by
`balances`, `expense_detail`, `total_receivable` and `total_payable`.

```python
def sum_outstanding(debts) -> int
```
Plain loop over `debt["outstanding"]`, so no `GROUP BY` is needed.

```python
def total_receivable(conn, user_id) -> int   # debts where user is creditor
def total_payable(conn, user_id) -> int      # debts where user is debtor
def total_vendor_due(conn, user_id) -> int   # sum(amount - paid) over own shares
```
These three are the only sources of the three balance figures and are never
added together.

```python
def rupees(paise) -> str
```
`12345 -> "123.45"`. Registered as a Jinja filter:
`templates.env.filters["rupees"] = rupees`, so templates print
`{{ share['amount']|rupees }}` and never divide by 100 themselves.

```python
def message_redirect(url, message) -> RedirectResponse
```
303 to `url + "?msg=" + quote(message)`. Every successful write uses it, so a
page can show one short confirmation line.

### 4.4 Shared queries

```python
def active_vendors(conn)            # WHERE is_active = 1 ORDER BY name
def expenses_for(conn, user_id)     # DISTINCT expenses the user created or takes part in
```
`expenses_for` joins `expenses`, `vendors`, `users` and `expense_shares`, filters
on `creator_id = ? OR s.user_id = ?`, and orders by `e.id DESC`. `DISTINCT` is
needed because the join produces one row per share. It returns `sqlite3.Row`
objects, which the templates read by name (`expense["vendor_name"]`).

### 4.5 Page helpers

```python
def login_response(request, error=None, username="")
def register_response(request, error=None, username="")
```
Re-render the form and echo back the username, so a failed login or register
does not clear the field.

```python
def expense_form_response(request, user, vendor_id=0, error=None)
```
Loads active vendors and all students, and re-renders `expense_new.html` with
the previously chosen vendor still selected.

```python
def vendor_form_response(request, user, vendor=None, error=None)
```
One template serves both "new" and "edit": `vendor=None` means new.

```python
def vendor_form_problem(name, category, upi_id) -> str | None
```
Returns `"Name and category are required."` if either is blank,
`"UPI id must look like shop@bank."` if `"@"` is missing, else `None`.

### 4.6 Setup

```python
def seed_demo_data()
```
Inserts four users (`admin/admin123`, `aarav`, `diya`, `rohan` all
`student123`) and six vendors only when the username or vendor name is not
already present, so it is safe on every start. `create_tables()` uses
`CREATE TABLE IF NOT EXISTS` for the same reason.

### 4.7 Error handler

```python
@app.exception_handler(HTTPException)
def http_exception_handler(request, exc)
```
- `303` → `RedirectResponse(location, 303)`, location from
  `exc.headers["Location"]`, default `/login`.
- Anything else → `error.html` with `status_code`, `message` and
  `current_user(request)` (so the navigation still shows), returned with the
  same status code.

---

## 5. Route logic in detail

### 5.1 `login` (POST `/login`)

1. `SELECT * FROM users WHERE username = ?` with the stripped username.
2. If no row or `verify_password` fails → `login_response(request, "Wrong
   username or password.", username)`.
3. Otherwise `RedirectResponse("/dashboard", 303)` and
   `set_cookie("token", create_token(row["id"]), httponly=True)`.

The message is deliberately identical for an unknown user and a wrong password.

### 5.2 `register` (POST `/register`)

Validation order (first failure wins):

| Check | Message |
|---|---|
| `len(username) < 3` | Username needs at least 3 characters. |
| `len(password) < 6` | Password needs at least 6 characters. |
| `password != confirm_password` | The two passwords are not the same. |
| `sqlite3.IntegrityError` on insert | That username is already taken. |

The new user always gets role `student`. Success logs the user in by setting the
same cookie, using `cursor.lastrowid`.

### 5.3 `create_expense` (POST `/expenses/new`, async)

1. `form = await request.form()` and `form.getlist("participants")` — a repeated
   checkbox field, which is why the route is `async`.
2. Description blank → "Please write what the expense was for."
3. No participants → "Choose at least one student to split with."
4. `to_paise(total)` fails → "Total amount must be a number like 210 or 210.50."
5. Any `split_type` other than `"custom"` is stored as `"equal"`.
6. `split_shares(...)` `ValueError` → its message.
7. Insert the expense, then insert one share per participant using
   `shares[index]` paired with `participant_ids[index]`.
8. `commit()`, `close()`, redirect to `/expenses/{id}?msg=Expense created.`

The creator is not added automatically; the creator ticks themselves in the
participant list like everyone else.

### 5.4 `expense_detail` (GET `/expenses/{expense_id}`)

Three queries: the expense with vendor name, UPI id and creator name; its shares
with usernames; its debts with both usernames, passed through
`with_outstanding`. Then the UPI base is built once:

```python
upi_base = ("upi://pay?pa=" + vendor_upi
            + "&pn=" + quote(vendor_name)
            + "&cu=INR&tn=" + quote(description))
```

The template appends `&am={{ share['amount']|rupees }}`, so the amount is
formatted in exactly one place.

### 5.5 `pay_share` (POST `/shares/{share_id}/pay`)

1. Load the share; 404 if missing.
2. `due = share["amount"] - share["paid"]`.
3. Invalid amount → redirect back with "Invalid amount."
4. `amount_paise > due` → "You can pay at most the unpaid amount for that share."
   Nothing is written.
5. `UPDATE expense_shares SET paid = paid + ? WHERE id = ?`.
6. If `share["user_id"] != user["id"]`, insert one debt
   `(debtor=share owner, creditor=payer, expense_id, amount_paise)`.
7. Redirect back with "Payment recorded."

Consequences that follow from this ordering:

- Paying your own share creates **no** debt.
- Paying someone else's share creates **exactly one** debt for the amount paid,
  so two partial payments of ₹40 and ₹30 produce two debts of ₹40 and ₹30 and
  never a single ₹70 edited row.
- The expense's `paid` total and the debt total stay consistent with each other
  for the same action.

### 5.6 `balances` (GET `/balances`)

Four queries: debts I am creditor of, debts I am debtor of (both joined to
`users` for the other name), and my unpaid shares joined to expenses and
vendors (`WHERE s.user_id = ? AND s.amount > s.paid`). Context gets
`receivable`, `payable`, `vendor_shares`, `total_receivable`, `total_payable`,
`vendor_due`. The page shows the three totals in separate cards and never sums
them.

### 5.7 `settle_debt` (POST `/debts/{debt_id}/settle`)

1. Load the debt; 404 if missing.
2. `debt["debtor_id"] != user["id"]` → 403 "You can only settle your own debts."
3. `remaining = outstanding_amount(conn, debt)`.
4. Invalid amount → "Invalid amount."; `amount_paise > remaining` → "You cannot
   repay more than you owe." In both cases nothing is written.
5. `INSERT INTO settlements (debt_id, payer_id, amount)`, commit, redirect.

A full repayment is just a settlement equal to the outstanding amount; the debt
row stays for history.

### 5.8 Vendor routes

- `create_vendor` / `update_vendor` both call `vendor_form_problem` first and
  re-render the same form with the message on failure.
- `is_active` arrives as the string `"1"` from the checkbox and is stored as
  `1 if is_active == "1" else 0`.
- `toggle_vendor` writes `1 - vendor["is_active"]`, so one button both
  activates and deactivates.
- The shared form template picks its heading and action with
  `"/admin/vendors/{{ vendor['id'] ~ '/edit' if vendor else 'new' }}"`.

---

## 6. Template contracts

Every page extends `base.html`, which renders the navigation from
`current_user` and the confirmation line from `request.query_params.get("msg")`.

| Template | Context keys |
|---|---|
| `login.html` | `current_user` (None), `error`, `username` |
| `register.html` | `current_user` (None), `error`, `username` |
| `dashboard.html` | `current_user`, `expenses`, `vendors`, `vendor_count`, `people_owe_you`, `you_owe_people`, `vendor_due` |
| `vendors.html` | `current_user`, `vendors` |
| `vendor_detail.html` | `current_user`, `vendor` |
| `expenses.html` | `current_user`, `expenses` |
| `expense_new.html` | `current_user`, `vendors`, `students`, `selected_vendor`, `error` |
| `expense_detail.html` | `current_user`, `expense`, `shares`, `debts`, `upi_base` |
| `balances.html` | `current_user`, `receivable`, `payable`, `vendor_shares`, `total_receivable`, `total_payable`, `vendor_due` |
| `admin_vendors.html` | `current_user`, `vendors` (all, active and inactive) |
| `admin_vendor_form.html` | `current_user`, `vendor` (None for new), `error` |
| `error.html` | `current_user`, `status_code`, `message` |

Conventions:

- Money is printed only through `|rupees`; templates contain no `/ 100`.
- Rows are used as `share["amount"]`, `expense["vendor_name"]`, etc.
- Every form posts to a URL that appears in the route table above.
- There is no JavaScript at all in the templates: every interaction is a link
  or a form POST, and there is no client-side validation, so the server-side
  checks listed in section 7 are the only ones.
- The split type is two radio buttons (`equal`, `custom`); the custom amount
  boxes are always rendered as `custom_<user_id>` inputs and only read when the
  `custom` radio is chosen.

---

## 7. Validation rules (complete)

| Input | Rule | Where enforced | Message |
|---|---|---|---|
| username (register) | >= 3 chars after strip | `register` | Username needs at least 3 characters. |
| password (register) | >= 6 chars | `register` | Password needs at least 6 characters. |
| confirm password | equal to password | `register` | The two passwords are not the same. |
| username (register) | unique | `UNIQUE` + `IntegrityError` | That username is already taken. |
| username/password (login) | must match a stored pair | `login` | Wrong username or password. |
| description | not blank | `create_expense` | Please write what the expense was for. |
| participants | at least one | `create_expense` | Choose at least one student to split with. |
| total | parseable, > 0 | `to_paise` | Total amount must be a number like 210 or 210.50. |
| custom shares | all present, > 0 | `split_shares`/`to_paise` | Every chosen student needs a custom amount. |
| custom shares | sum equals total | `split_shares` | The custom amounts must add up to the total. |
| share payment | amount <= unpaid part | `pay_share` | You can pay at most the unpaid amount for that share. |
| debt repayment | amount <= outstanding | `settle_debt` | You cannot repay more than you owe. |
| debt repayment | only the debtor may pay | `settle_debt` | 403 You can only settle your own debts. |
| vendor name/category | not blank | `vendor_form_problem` | Name and category are required. |
| vendor UPI id | contains `@` | `vendor_form_problem` | UPI id must look like shop@bank. |
| admin pages | role must be `admin` | `require_admin` | 403 Only an admin can manage vendors. |

---

## 8. Algorithms

### 8.1 Amount parsing

```
to_paise("210.5"):
  rupees_part = "210", paise_part = "5" -> "500" -> "50"
  return 210 * 100 + 50 = 21050
```

No floats are involved anywhere, so `0.1 + 0.2` style errors cannot occur.

### 8.2 Equal split with remainder

```
equal_split(21000, 3) -> base=7000, rem=0 -> [7000, 7000, 7000]
equal_split(10000, 3) -> base=3333, rem=1 -> [3334, 3333, 3333]
```

The first `rem` participants absorb the extra paise, which makes the result
deterministic and keeps the sum exact.

### 8.3 Outstanding debt

```
outstanding(debt) = debt.amount - sum(settlements.amount where debt_id = debt.id)
```

Computed on read, never stored, so it cannot drift out of sync.

### 8.4 Three balance totals

```
receivable   = sum(outstanding of debts where creditor_id = me)
payable      = sum(outstanding of debts where debtor_id = me)
vendor_due   = sum(share.amount - share.paid for my shares)
```

They are displayed side by side and never combined into one "net" figure.

---

## 9. Request lifecycle example — paying someone else's share

```
Browser  POST /shares/12/pay   amount=70
   |
   v
FastAPI  require_user -> current_user reads cookie -> dict row for aarav
   |
   v
pay_share
   SELECT * FROM expense_shares WHERE id = 12      -> owner diya, amount 7000, paid 0
   due = 7000
   to_paise("70") = 7000, not > due
   UPDATE expense_shares SET paid = 7000 WHERE id = 12
   owner != me -> INSERT INTO debts (diya, aarav, 1, 7000)
   commit, close
   |
   v
303  /expenses/1?msg=Payment%20recorded.
   |
   v
Browser GET /expenses/1 -> expense_detail renders shares (diya paid) and debts (diya owes aarav)
```

---

## 10. Known limitations

- The JWT secret is a constant in `main.py`; a real deployment would read it
  from the environment.
- No CSRF token on forms.
- A payment is recorded when the student presses the button; nothing verifies
  it with a bank.
- One connection per request, opened and closed by hand.
- No automated tests in the repository; the flows in `WHAT_CHANGED.md` were
  walked through manually.
