import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = BASE_DIR / "campus_split.db"

SECRET_KEY = "campus-split-class-project-secret"
ALGORITHM = "HS256"
TOKEN_MINUTES = 60 * 24  # 1 day

app = FastAPI()

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")



# Database helpers
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            upi_id TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            vendor_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            total INTEGER NOT NULL,
            split_type TEXT NOT NULL DEFAULT 'equal',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_id) REFERENCES users(id),
            FOREIGN KEY (vendor_id) REFERENCES vendors(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS expense_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (expense_id) REFERENCES expenses(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debtor_id INTEGER NOT NULL,
            creditor_id INTEGER NOT NULL,
            expense_id INTEGER,
            amount INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (debtor_id) REFERENCES users(id),
            FOREIGN KEY (creditor_id) REFERENCES users(id),
            FOREIGN KEY (expense_id) REFERENCES expenses(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debt_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (debt_id) REFERENCES debts(id),
            FOREIGN KEY (payer_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()



# Passwords and login tokens
# ---------------------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    )
    return salt + "$" + hashed.hex()


def verify_password(password, saved):
    try:
        salt, hashed = saved.split("$", 1)
    except ValueError:
        return False

    new_hashed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    )
    return secrets.compare_digest(new_hashed.hex(), hashed)


def create_token(user_id):
    expire = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MINUTES)
    data = {"uid": user_id, "exp": expire}
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)

# Dependencies (who is logged in?)
# ---------------------------------------------------------------------------

def current_user(request: Request):
    token = request.cookies.get("token")
    if not token:
        return None

    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None

    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (data["uid"],)).fetchone()
    conn.close()

    if row is None:
        return None
    return dict(row)


def require_user(user=Depends(current_user)):
    if user is None:
        # Special status code: our error handler turns this into a /login redirect.
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Sirf admin manage karega vendors.")
    return user


# Money helpers
# ---------------------------------------------------------------------------

def to_paise(text):
    # string ko paise mai convert karne ke liye
    text = str(text).strip()
    if text == "":
        raise ValueError("please enter an amount.")

    if "." in text:
        rupees_part, paise_part = text.split(".", 1)
        paise_part = (paise_part + "00")[:2]
    else:
        rupees_part, paise_part = text, "00"

    paise = int(rupees_part) * 100 + int(paise_part)
    if paise <= 0:
        raise ValueError("amount must be more than zero.")
    return paise


def equal_split(total_paise, number_of_people):
    #split paise equally among people
    base = total_paise // number_of_people
    remainder = total_paise % number_of_people

    shares = []
    for index in range(number_of_people):
        if index < remainder:
            shares.append(base + 1)
        else:
            shares.append(base)
    return shares


def split_shares(form, participant_ids, total_paise, split_type):
    if split_type != "custom":
        return equal_split(total_paise, len(participant_ids))

    shares = []
    for user_id in participant_ids:
        value = form.get("custom_" + str(user_id), "")
        try:
            shares.append(to_paise(value))
        except ValueError:
            raise ValueError("Every chosen student needs a custom amount.")

    if sum(shares) != total_paise:
        raise ValueError("The custom amount does not add up to the total.")
    return shares


def outstanding_amount(conn, debt):
    # kitna paisa abhi bhi bacha hai is debt me
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS paid FROM settlements WHERE debt_id = ?",
        (debt["id"],),
    ).fetchone()
    return debt["amount"] - row["paid"]


def with_outstanding(conn, rows):
    # har debt row ko dict me convert karke 'outstanding' amount add karo
    debts = []
    for row in rows:
        debt = dict(row)
        debt["outstanding"] = outstanding_amount(conn, row)
        debts.append(debt)
    return debts


def sum_outstanding(debts):
    total = 0
    for debt in debts:
        total = total + debt["outstanding"]
    return total


def total_receivable(conn, user_id):
    rows = conn.execute("SELECT * FROM debts WHERE creditor_id = ?", (user_id,)).fetchall()
    return sum_outstanding(with_outstanding(conn, rows))


def total_payable(conn, user_id):
    rows = conn.execute("SELECT * FROM debts WHERE debtor_id = ?", (user_id,)).fetchall()
    return sum_outstanding(with_outstanding(conn, rows))


def total_vendor_due(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM expense_shares WHERE user_id = ?", (user_id,)
    ).fetchall()
    total = 0
    for share in rows:
        total = total + (share["amount"] - share["paid"])
    return total


def rupees(paise):
    return f"{paise / 100:.2f}"


templates.env.filters["rupees"] = rupees


def message_redirect(url, message):
    return RedirectResponse(url + "?msg=" + quote(message), status_code=303)

# Queries used by more than one page
# ---------------------------------------------------------------------------

def active_vendors(conn):
    return conn.execute(
        "SELECT * FROM vendors WHERE is_active = 1 ORDER BY name"
    ).fetchall()


def expenses_for(conn, user_id):
   # expenses jo is student ne create kiye ya jinme ye part hai
    return conn.execute(
        """
        SELECT DISTINCT e.*, v.name AS vendor_name, u.username AS creator_name
        FROM expenses e
        JOIN vendors v ON v.id = e.vendor_id
        JOIN users u ON u.id = e.creator_id
        JOIN expense_shares s ON s.expense_id = e.id
        WHERE e.creator_id = ? OR s.user_id = ?
        ORDER BY e.id DESC
        """,
        (user_id, user_id),
    ).fetchall()

# Page helpers
# ---------------------------------------------------------------------------

def login_response(request, error=None, username=""):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"current_user": None, "error": error, "username": username},
    )


def register_response(request, error=None, username=""):
    return templates.TemplateResponse(
        request,
        "register.html",
        {"current_user": None, "error": error, "username": username},
    )


def expense_form_response(request, user, vendor_id=0, error=None):
    conn = get_connection()
    vendors = active_vendors(conn)
    students = conn.execute(
        "SELECT * FROM users WHERE role = 'student' ORDER BY username"
    ).fetchall()
    conn.close()

    return templates.TemplateResponse(
        request,
        "expense_new.html",
        {
            "current_user": user,
            "vendors": vendors,
            "students": students,
            "selected_vendor": vendor_id,
            "error": error,
        },
    )


def vendor_form_response(request, user, vendor=None, error=None):
    return templates.TemplateResponse(
        request,
        "admin_vendor_form.html",
        {"current_user": user, "vendor": vendor, "error": error},
    )


def vendor_form_problem(name, category, upi_id):
    """Returns an error message, or None when the vendor form is fine."""
    if name.strip() == "" or category.strip() == "":
        return "Name and category are required."
    if "@" not in upi_id:
        return "UPI id must look like shop@bank."
    return None

# Small setup: tables + demo data (safe to run every time)
# ---------------------------------------------------------------------------

def seed_demo_data():
    conn = get_connection()

    demo_users = [
        ("admin", "admin123", "admin"),
        ("aarav", "student123", "student"),
        ("diya", "student123", "student"),
        ("rohan", "student123", "student"),
    ]
    for username, password, role in demo_users:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                (username, hash_password(password), role),
            )

    demo_vendors = [
        ("Amul parlour", "Food & Dining",
         "Daily meals, dosas, thalis and quick snacks.", "amullnm.canteen@okhdfcbank", 1),
        ("Canmpus stationary ", "Printing & Supplies",
         "Photocopies, Snacks, stationary .", "campusstationary.falana@oksbi", 1),
        ("Night Mess bh4", "Food & Dining",
         "Late night samosa, maggi and egg rolls.", "nightmess@icici", 1),
        ("Campus Laundry", "Laundry & Services",
         "Weekly wash, fold, dry clean and ironing.", "campuslaundry@paytm", 1),
        ("CCD", "Beverages & Cafe",
         "Cafe , coffe, D'lite ", "frappe@axl", 1),
        ("Kravers kicthen", "Food & Dining",
         "mid meals and expensive snacks.", "kraverskitchen@ybl", 0),
    ]
    for name, category, description, upi_id, is_active in demo_vendors:
        row = conn.execute("SELECT id FROM vendors WHERE name = ?", (name,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO vendors (name, category, description, upi_id, is_active)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, category, description, upi_id, is_active),
            )

    conn.commit()
    conn.close()


create_tables()
seed_demo_data()


# Error handling
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        location = (exc.headers or {}).get("Location", "/login")
        return RedirectResponse(url=location, status_code=303)

    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "current_user": current_user(request),
            "status_code": exc.status_code,
            "message": exc.detail,
        },
        status_code=exc.status_code,
    )


# Login / register / logout
# ---------------------------------------------------------------------------

@app.get("/")
def home():
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/login")
def login_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return login_response(request)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    conn.close()

    if row is None or not verify_password(password, row["password"]):
        return login_response(request, "Wrong Username or Password.", username)

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("token", create_token(row["id"]), httponly=True)
    return response


@app.get("/register")
def register_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return register_response(request)


@app.post("/register")
def register(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    username = username.strip()
    error = None

    if len(username) < 3:
        error = "Username needs at least 3 characters."
    elif len(password) < 6:
        error = "Password needs at least 6 characters."
    elif password != confirm_password:
        error = "The two passwords are not the same."

    if error:
        return register_response(request, error, username)

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, 'student')",
            (username, hash_password(password)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return register_response(request, "That username is already taken.", username)

    new_user_id = cursor.lastrowid
    conn.close()

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("token", create_token(new_user_id), httponly=True)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("token")
    return response

# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard")
def dashboard(request: Request, user=Depends(require_user)):
    conn = get_connection()

    expenses = expenses_for(conn, user["id"])[:5]
    vendors = active_vendors(conn)

    people_owe_you = total_receivable(conn, user["id"])
    you_owe_people = total_payable(conn, user["id"])
    vendor_due = total_vendor_due(conn, user["id"])
    conn.close()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_user": user,
            "expenses": expenses,
            "vendors": vendors[:4],
            "vendor_count": len(vendors),
            "people_owe_you": people_owe_you,
            "you_owe_people": you_owe_people,
            "vendor_due": vendor_due,
        },
    )


# Vendors (students)
# ---------------------------------------------------------------------------

@app.get("/vendors")
def vendors_page(request: Request, user=Depends(require_user)):
    conn = get_connection()
    rows = active_vendors(conn)
    conn.close()

    return templates.TemplateResponse(
        request, "vendors.html", {"current_user": user, "vendors": rows}
    )


@app.get("/vendors/{vendor_id}")
def vendor_detail(vendor_id: int, request: Request, user=Depends(require_user)):
    conn = get_connection()
    vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if vendor is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Vendor not found")

    conn.close()

    return templates.TemplateResponse(
        request, "vendor_detail.html", {"current_user": user, "vendor": vendor}
    )


# Expenses
# ---------------------------------------------------------------------------

@app.get("/expenses")
def expenses_page(request: Request, user=Depends(require_user)):
    conn = get_connection()
    rows = expenses_for(conn, user["id"])
    conn.close()

    return templates.TemplateResponse(
        request, "expenses.html", {"current_user": user, "expenses": rows}
    )


@app.get("/expenses/new")
def new_expense_page(request: Request, vendor_id: int = 0, user=Depends(require_user)):
    return expense_form_response(request, user, vendor_id)

@app.post("/expenses/new")
async def create_expense(
    request: Request,
    vendor_id: int = Form(...),
    description: str = Form(""),
    total: str = Form(""),
    split_type: str = Form("equal"),
    user=Depends(require_user),
):
    form = await request.form()
    participant_ids = form.getlist("participants")

    description = description.strip()
    if description == "":
        return expense_form_response(
            request, user, vendor_id, "Please write what the expense was for."
        )

    if len(participant_ids) == 0:
        return expense_form_response(
            request, user, vendor_id, "Choose at least one student to split with."
        )

    try:
        total_paise = to_paise(total)
    except ValueError:
        return expense_form_response(
            request, user, vendor_id, "Total amount must be a number like 210 or 210.50."
        )

    if split_type != "custom":
        split_type = "equal"

    try:
        shares = split_shares(form, participant_ids, total_paise, split_type)
    except ValueError as error:
        return expense_form_response(request, user, vendor_id, str(error))

    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO expenses (creator_id, vendor_id, description, total, split_type)
           VALUES (?, ?, ?, ?, ?)""",
        (user["id"], vendor_id, description, total_paise, split_type),
    )
    expense_id = cursor.lastrowid

    index = 0
    for user_id in participant_ids:
        conn.execute(
            "INSERT INTO expense_shares (expense_id, user_id, amount, paid) VALUES (?, ?, ?, 0)",
            (expense_id, int(user_id), shares[index]),
        )
        index = index + 1

    conn.commit()
    conn.close()

    return message_redirect("/expenses/" + str(expense_id), "Expense created.")


@app.get("/expenses/{expense_id}")
def expense_detail(expense_id: int, request: Request, user=Depends(require_user)):
    conn = get_connection()

    expense = conn.execute(
        """
        SELECT e.*, v.name AS vendor_name, v.upi_id AS vendor_upi,
               u.username AS creator_name
        FROM expenses e
        JOIN vendors v ON v.id = e.vendor_id
        JOIN users u ON u.id = e.creator_id
        WHERE e.id = ?
        """,
        (expense_id,),
    ).fetchone()

    if expense is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Expense not found")

    shares = conn.execute(
        """
        SELECT s.*, u.username
        FROM expense_shares s
        JOIN users u ON u.id = s.user_id
        WHERE s.expense_id = ?
        ORDER BY s.id
        """,
        (expense_id,),
    ).fetchall()

    debts = conn.execute(
        """
        SELECT d.*, u.username AS debtor_name, c.username AS creditor_name
        FROM debts d
        JOIN users u ON u.id = d.debtor_id
        JOIN users c ON c.id = d.creditor_id
        WHERE d.expense_id = ?
        ORDER BY d.id
        """,
        (expense_id,),
    ).fetchall()

    debts = with_outstanding(conn, debts)
    conn.close()

    # The UPI app takes the vendor, the note and (per share) the amount.
    upi_base = (
        "upi://pay?pa=" + expense["vendor_upi"]
        + "&pn=" + quote(expense["vendor_name"])
        + "&cu=INR&tn=" + quote(expense["description"])
    )

    return templates.TemplateResponse(
        request,
        "expense_detail.html",
        {
            "current_user": user,
            "expense": expense,
            "shares": shares,
            "debts": debts,
            "upi_base": upi_base,
        },
    )


@app.post("/shares/{share_id}/pay")
def pay_share(share_id: int, amount: str = Form(""), user=Depends(require_user)):
    """Record a payment to the vendor for one share.

    - If I pay my own share, no debt is created.
    - If I pay someone else's share, that person now owes me that money.
    """
    conn = get_connection()
    share = conn.execute(
        "SELECT * FROM expense_shares WHERE id = ?", (share_id,)
    ).fetchone()

    if share is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Share not found")

    due = share["amount"] - share["paid"]
    back = "/expenses/" + str(share["expense_id"])

    try:
        amount_paise = to_paise(amount)
    except ValueError:
        conn.close()
        return message_redirect(back, "Invalid amount.")

    if amount_paise > due:
        conn.close()
        return message_redirect(back, "You can pay at most the unpaid amount for that share.")

    conn.execute(
        "UPDATE expense_shares SET paid = paid + ? WHERE id = ?",
        (amount_paise, share_id),
    )

    if share["user_id"] != user["id"]:
        conn.execute(
            """INSERT INTO debts (debtor_id, creditor_id, expense_id, amount)
               VALUES (?, ?, ?, ?)""",
            (share["user_id"], user["id"], share["expense_id"], amount_paise),
        )

    conn.commit()
    conn.close()

    return message_redirect(back, "Payment recorded.")


# Balances and peer settlements
# ---------------------------------------------------------------------------

@app.get("/balances")
def balances(request: Request, user=Depends(require_user)):
    conn = get_connection()

    others_owe_me = conn.execute(
        """
        SELECT d.*, u.username AS other_name
        FROM debts d
        JOIN users u ON u.id = d.debtor_id
        WHERE d.creditor_id = ?
        ORDER BY d.id DESC
        """,
        (user["id"],),
    ).fetchall()

    i_owe_others = conn.execute(
        """
        SELECT d.*, u.username AS other_name
        FROM debts d
        JOIN users u ON u.id = d.creditor_id
        WHERE d.debtor_id = ?
        ORDER BY d.id DESC
        """,
        (user["id"],),
    ).fetchall()

    vendor_shares = conn.execute(
        """
        SELECT s.*, e.description, e.id AS expense_id, v.name AS vendor_name
        FROM expense_shares s
        JOIN expenses e ON e.id = s.expense_id
        JOIN vendors v ON v.id = e.vendor_id
        WHERE s.user_id = ? AND s.amount > s.paid
        ORDER BY s.id DESC
        """,
        (user["id"],),
    ).fetchall()

    receivable_rows = with_outstanding(conn, others_owe_me)
    payable_rows = with_outstanding(conn, i_owe_others)

    vendor_due = total_vendor_due(conn, user["id"])
    conn.close()

    return templates.TemplateResponse(
        request,
        "balances.html",
        {
            "current_user": user,
            "receivable": receivable_rows,
            "payable": payable_rows,
            "vendor_shares": vendor_shares,
            "total_receivable": sum_outstanding(receivable_rows),
            "total_payable": sum_outstanding(payable_rows),
            "vendor_due": vendor_due,
        },
    )


@app.post("/debts/{debt_id}/settle")
def settle_debt(debt_id: int, amount: str = Form(""), user=Depends(require_user)):
    conn = get_connection()
    debt = conn.execute("SELECT * FROM debts WHERE id = ?", (debt_id,)).fetchone()

    if debt is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Debt not found")

    if debt["debtor_id"] != user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="You can only settle your own debts.")

    remaining = outstanding_amount(conn, debt)

    try:
        amount_paise = to_paise(amount)
    except ValueError:
        conn.close()
        return message_redirect("/balances", "Invalid amount.")

    if amount_paise > remaining:
        conn.close()
        return message_redirect("/balances", "You cannot repay more than you owe.")

    conn.execute(
        "INSERT INTO settlements (debt_id, payer_id, amount) VALUES (?, ?, ?)",
        (debt_id, user["id"], amount_paise),
    )
    conn.commit()
    conn.close()

    return message_redirect("/balances", "Repayment recorded.")

# Admin: manage vendors
# ---------------------------------------------------------------------------

@app.get("/admin/vendors")
def admin_vendors(request: Request, user=Depends(require_admin)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        request, "admin_vendors.html", {"current_user": user, "vendors": rows}
    )


@app.get("/admin/vendors/new")
def new_vendor_page(request: Request, user=Depends(require_admin)):
    return vendor_form_response(request, user)


@app.post("/admin/vendors/new")
def create_vendor(
    request: Request,
    name: str = Form(""),
    category: str = Form(""),
    upi_id: str = Form(""),
    description: str = Form(""),
    is_active: str = Form(""),
    user=Depends(require_admin),
):
    problem = vendor_form_problem(name, category, upi_id)
    if problem:
        return vendor_form_response(request, user, None, problem)

    conn = get_connection()
    conn.execute(
        """INSERT INTO vendors (name, category, description, upi_id, is_active)
           VALUES (?, ?, ?, ?, ?)""",
        (
            name.strip(),
            category.strip(),
            description.strip(),
            upi_id.strip(),
            1 if is_active == "1" else 0,
        ),
    )
    conn.commit()
    conn.close()

    return message_redirect("/admin/vendors", "Vendor added.")


@app.get("/admin/vendors/{vendor_id}/edit")
def edit_vendor_page(vendor_id: int, request: Request, user=Depends(require_admin)):
    conn = get_connection()
    vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    conn.close()

    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")

    return vendor_form_response(request, user, vendor)


@app.post("/admin/vendors/{vendor_id}/edit")
def update_vendor(
    vendor_id: int,
    request: Request,
    name: str = Form(""),
    category: str = Form(""),
    upi_id: str = Form(""),
    description: str = Form(""),
    is_active: str = Form(""),
    user=Depends(require_admin),
):
    conn = get_connection()
    vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()

    if vendor is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Vendor not found")

    problem = vendor_form_problem(name, category, upi_id)
    if problem:
        conn.close()
        return vendor_form_response(request, user, vendor, problem)

    conn.execute(
        """UPDATE vendors
           SET name = ?, category = ?, description = ?, upi_id = ?, is_active = ?
           WHERE id = ?""",
        (
            name.strip(),
            category.strip(),
            description.strip(),
            upi_id.strip(),
            1 if is_active == "1" else 0,
            vendor_id,
        ),
    )
    conn.commit()
    conn.close()

    return message_redirect("/admin/vendors", "Vendor updated.")


@app.post("/admin/vendors/{vendor_id}/toggle")
def toggle_vendor(vendor_id: int, user=Depends(require_admin)):
    conn = get_connection()
    vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()

    if vendor is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Vendor not found")

    new_value = 1 - vendor["is_active"]
    conn.execute("UPDATE vendors SET is_active = ? WHERE id = ?", (new_value, vendor_id))
    conn.commit()
    conn.close()

    return message_redirect("/admin/vendors", "Vendor status changed.")
