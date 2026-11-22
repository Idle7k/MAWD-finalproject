# Campus Split

Campus Split is a campus expense splitter. Students share an expense, and each
student can pay their own share **directly to the vendor**. When one student pays
for another student's share, that becomes a **peer debt** between the two students.

This version of the project uses only what we have covered in class: FastAPI
routes, `sqlite3`, Jinja2 templates, plain HTML forms, one CSS file, and password
hashing with `hashlib` plus a JWT stored in a cookie.

## Files

```
campus-split-simple/
├── main.py                 all tables, helpers and routes
├── templates/              Jinja2 pages
│   ├── base.html
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   ├── expenses.html
│   ├── expense_new.html
│   ├── expense_detail.html
│   ├── balances.html
│   ├── vendors.html
│   ├── vendor_detail.html
│   ├── admin_vendors.html
│   ├── admin_vendor_form.html
│   └── error.html
├── static/style.css
├── requirements.txt
└── README.md
```

## Run it

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

The database file `campus_split.db` is created automatically the first time the
app starts, and the demo users and vendors are added automatically. Delete the
file and restart to start again from an empty database.

## Demo accounts

| Username | Password | Role |
|---|---|---|
| `admin` | `admin123` | admin (can manage vendors) |
| `aarav` | `student123` | student |
| `diya` | `student123` | student |
| `rohan` | `student123` | student |

## What the app does

1. **Register or log in.** Passwords are hashed with `hashlib.pbkdf2_hmac` and a
   random salt. The login token is a JWT kept in an `httponly` cookie. Every form
   reposts with a plain message under the form when something is wrong.
2. **Dashboard.** Shows three separate balances: people who owe you, people you
   owe, and vendor payments due.
3. **Split an expense.** Pick a vendor, a description, the total, the students,
   and split it equally or give each student a custom amount.
4. **Pay a vendor.** On the expense page, each unpaid share has a UPI link and an
   "I paid" button. Paying your own share creates no peer debt. Paying someone
   else's share creates a peer debt from that student to you.
5. **Balances.** Peer debts are listed separately from vendor dues, and you can
   record a full or partial repayment of a debt you owe.
6. **Admin.** An admin can add vendors, edit them, and activate or deactivate them.

## Money

Amounts are stored as whole paise (1 rupee = 100 paise) so that no money is lost
to rounding. `to_paise()` converts what you type, and one Jinja filter does the
other direction: `{{ paise|rupees }}` prints `70.00`, so the templates never do
money arithmetic themselves. Equal splits put the leftover paise on the first few
students, so the shares always add up to the total.

## Notes for the demo

- Payments are user-reported ("I paid"), not checked with a bank. The UPI link
  opens a UPI app with the vendor's UPI ID and the amount pre-filled.
- Everything runs locally on SQLite.
