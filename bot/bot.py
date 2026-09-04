import calendar
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BLINKO_URL = os.environ["BLINKO_URL"].rstrip("/")
BLINKO_API_TOKEN = os.environ["BLINKO_API_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "/data/money.db")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID")
BLINKO_UPSERT_URL = f"{BLINKO_URL}/api/v1/note/upsert"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # The previous version used loans.returned/returned_at. Since the current
    # database contains only fake data, reset that old schema cleanly.
    loan_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(loans)").fetchall()
    }
    if loan_columns and "returned" in loan_columns:
        logger.warning("Old loan schema detected; resetting fake transaction data.")
        conn.execute("DROP TABLE IF EXISTS repayments")
        conn.execute("DROP TABLE IF EXISTS loans")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            purpose TEXT NOT NULL,
            lent_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repayments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            paid_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loans_person ON loans(person)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_loans_lent_at ON loans(lent_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repayments_loan_id ON repayments(loan_id)")
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now().astimezone().isoformat()


def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key, value):
    conn = get_db()
    conn.execute("""
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


def get_loan(loan_id):
    conn = get_db()
    row = conn.execute("""
        SELECT
            l.*,
            COALESCE(SUM(r.amount), 0) AS repaid,
            l.amount - COALESCE(SUM(r.amount), 0) AS remaining
        FROM loans l
        LEFT JOIN repayments r ON r.loan_id = l.id
        WHERE l.id = ?
        GROUP BY l.id
    """, (loan_id,)).fetchone()
    conn.close()
    return row


def get_outstanding_loans():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            l.*,
            COALESCE(SUM(r.amount), 0) AS repaid,
            l.amount - COALESCE(SUM(r.amount), 0) AS remaining
        FROM loans l
        LEFT JOIN repayments r ON r.loan_id = l.id
        GROUP BY l.id
        HAVING l.amount - COALESCE(SUM(r.amount), 0) > 0
        ORDER BY l.lent_at DESC, l.id DESC
    """).fetchall()
    conn.close()
    return rows


def get_all_loans():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            l.*,
            COALESCE(SUM(r.amount), 0) AS repaid,
            l.amount - COALESCE(SUM(r.amount), 0) AS remaining
        FROM loans l
        LEFT JOIN repayments r ON r.loan_id = l.id
        GROUP BY l.id
        ORDER BY l.lent_at DESC, l.id DESC
    """).fetchall()
    conn.close()
    return rows


def get_repayments(loan_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM repayments
        WHERE loan_id = ?
        ORDER BY paid_at ASC, id ASC
    """, (loan_id,)).fetchall()
    conn.close()
    return rows


def get_repayment(repayment_id):
    conn = get_db()
    row = conn.execute("""
        SELECT r.*, l.person, l.amount AS loan_amount
        FROM repayments r
        JOIN loans l ON l.id = r.loan_id
        WHERE r.id = ?
    """, (repayment_id,)).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_allowed(update):
    if not ALLOWED_USER_ID:
        return True
    user = update.effective_user
    return user is not None and str(user.id) == ALLOWED_USER_ID


def format_amount(amount):
    return f"₹{int(amount):,}"


def display_date(value):
    try:
        return datetime.fromisoformat(value).strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return str(value)[:10]


def date_to_iso(selected_date):
    return datetime.combine(
        selected_date, datetime.min.time()
    ).astimezone().isoformat()


def parse_integer_amount(text):
    cleaned = text.strip().replace(",", "").replace("₹", "")
    if not re.fullmatch(r"\d+", cleaned):
        return None
    try:
        value = int(Decimal(cleaned))
    except (ValueError, InvalidOperation):
        return None
    return value if value > 0 else None


def clear_flow(context):
    for key in (
        "pending_loan",
        "pending_repayment",
        "pending_edit",
        "pending_delete",
        "calendar_action",
        "calendar_year",
        "calendar_month",
    ):
        context.user_data.pop(key, None)


def loan_label(loan):
    status = (
        f"remaining {format_amount(loan['remaining'])}"
        if loan["remaining"] > 0
        else "returned"
    )
    return f"#{loan['id']} {loan['person']} — {format_amount(loan['amount'])} ({status})"


# ---------------------------------------------------------------------------
# Blinko
# ---------------------------------------------------------------------------

def build_blinko_content():
    loans = get_outstanding_loans()
    lines = ["#Finance", "# Money Lent", ""]
    if not loans:
        lines.append("No outstanding loans.")
        return "\n".join(lines)

    lines.extend([
        "| Person | Original | Repaid | Remaining | Purpose | Date |",
        "|---|---:|---:|---:|---|---|",
    ])
    for loan in loans:
        person = loan["person"].replace("|", "\\|")
        purpose = loan["purpose"].replace("|", "\\|")
        lines.append(
            f"| {person} | {format_amount(loan['amount'])} | "
            f"{format_amount(loan['repaid'])} | {format_amount(loan['remaining'])} | "
            f"{purpose} | {display_date(loan['lent_at'])} |"
        )
    return "\n".join(lines)


def sync_blinko():
    content = build_blinko_content()
    note_id = get_setting("blinko_note_id")
    payload = {"content": content, "type": 1, "isShare": False}
    if note_id:
        payload["id"] = int(note_id)

    response = requests.post(
        BLINKO_UPSERT_URL,
        headers={
            "Authorization": f"Bearer {BLINKO_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    returned_id = data.get("id")
    if returned_id and not note_id:
        set_setting("blinko_note_id", str(returned_id))

    logger.info("Blinko synchronized")


async def sync_and_report(query, success_text):
    try:
        sync_blinko()
        await query.edit_message_text(success_text)
    except Exception:
        logger.exception("Blinko synchronization failed")
        await query.edit_message_text(
            success_text + "\n\nBlinko synchronization failed. "
            "The database change was saved safely."
        )


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def build_calendar(year, month, prefix="calendar"):
    keyboard = [[
        InlineKeyboardButton("◀", callback_data=f"{prefix}_prev"),
        InlineKeyboardButton(f"{MONTH_NAMES[month - 1]} {year}", callback_data=f"{prefix}_noop"),
        InlineKeyboardButton("▶", callback_data=f"{prefix}_next"),
    ]]
    keyboard.append([
        InlineKeyboardButton(day, callback_data=f"{prefix}_noop")
        for day in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    ])

    for week in calendar.monthcalendar(year, month):
        row = []
        for day_number in week:
            if day_number == 0:
                row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}_noop"))
            else:
                row.append(InlineKeyboardButton(
                    str(day_number),
                    callback_data=f"{prefix}_day:{year}:{month}:{day_number}",
                ))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("Today", callback_data=f"{prefix}_today"),
        InlineKeyboardButton("Cancel", callback_data=f"{prefix}_cancel"),
    ])
    return InlineKeyboardMarkup(keyboard)


async def show_calendar(query, context, prefix, title):
    year = context.user_data.get("calendar_year", date.today().year)
    month = context.user_data.get("calendar_month", date.today().month)
    context.user_data["calendar_prefix"] = prefix
    await query.edit_message_text(
        title,
        reply_markup=build_calendar(year, month, prefix),
    )


def set_calendar_context(context, prefix):
    today = date.today()
    context.user_data["calendar_year"] = today.year
    context.user_data["calendar_month"] = today.month
    context.user_data["calendar_prefix"] = prefix


async def calendar_navigation(update, context):
    query = update.callback_query
    await query.answer()
    prefix = context.user_data.get("calendar_prefix", "calendar")
    year = context.user_data.get("calendar_year", date.today().year)
    month = context.user_data.get("calendar_month", date.today().month)

    if query.data.endswith("_prev"):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    elif query.data.endswith("_next"):
        month += 1
        if month == 13:
            month, year = 1, year + 1

    context.user_data["calendar_year"] = year
    context.user_data["calendar_month"] = month

    title = (
        "Select the lending date:"
        if prefix == "loan_calendar"
        else "Select the repayment date:"
    )
    await show_calendar(query, context, prefix, title)


async def calendar_noop(update, context):
    await update.callback_query.answer()


async def calendar_cancel(update, context):
    query = update.callback_query
    await query.answer()
    clear_flow(context)
    await query.edit_message_text("Cancelled.")


async def calendar_today(update, context):
    query = update.callback_query
    await query.answer()
    prefix = context.user_data.get("calendar_prefix")
    if prefix == "loan_calendar":
        pending = context.user_data.get("pending_loan")
        if not pending:
            await query.edit_message_text("This transaction has expired.")
            return
        pending["lent_at"] = date_to_iso(date.today())
        await show_loan_confirmation(query, context)
    elif prefix == "repayment_calendar":
        pending = context.user_data.get("pending_repayment")
        if not pending:
            await query.edit_message_text("This repayment has expired.")
            return
        pending["paid_at"] = date_to_iso(date.today())
        await show_repayment_confirmation(query, context)


async def calendar_day(update, context):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    prefix = parts[0].removesuffix("_day")

    selected_date = date(
        int(parts[1]),
        int(parts[2]),
        int(parts[3]),
    )

    if prefix == "loan_calendar":
        pending = context.user_data.get("pending_loan")
        if not pending:
            await query.edit_message_text("This transaction has expired.")
            return

        pending["lent_at"] = date_to_iso(selected_date)
        await show_loan_confirmation(query, context)

    elif prefix == "repayment_calendar":
        pending = context.user_data.get("pending_repayment")
        if not pending:
            await query.edit_message_text("This repayment has expired.")
            return

        pending["paid_at"] = date_to_iso(selected_date)
        await show_repayment_confirmation(query, context)


# ---------------------------------------------------------------------------
# Add loan
# ---------------------------------------------------------------------------

def parse_loan_message(text):
    tokens = text.strip().split()
    if len(tokens) < 3:
        return None

    amount_index = None
    amount = None
    for index, token in enumerate(tokens):
        cleaned = token.replace(",", "").replace("₹", "")
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", cleaned):
            continue
        try:
            decimal_amount = Decimal(cleaned)
            if decimal_amount <= 0 or decimal_amount != decimal_amount.quantize(Decimal("1")):
                continue
            amount = int(decimal_amount)
            amount_index = index
            break
        except InvalidOperation:
            continue

    if amount_index is None or amount_index == 0:
        return None

    person = " ".join(tokens[:amount_index])
    purpose = " ".join(tokens[amount_index + 1:])
    if not person or not purpose:
        return None
    return {"person": person, "amount": amount, "purpose": purpose}


async def handle_message(update, context):
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    # State-specific text input.
    if context.user_data.get("pending_repayment"):
        await handle_repayment_amount_input(update, context)
        return

    if context.user_data.get("pending_edit"):
        if context.user_data["pending_edit"].get("field") == "repayment_amount":
            await handle_edit_repayment_amount(update, context)
        else:
            await handle_edit_input(update, context)
        return

    parsed = parse_loan_message(text)
    if not parsed:
        await update.message.reply_text(
            "I couldn't understand that.\n\n"
            "Use:\nFull Name Amount Purpose\n\n"
            "Example:\nRahul Kumar 500 dinner"
        )
        return

    context.user_data["pending_loan"] = parsed
    await update.message.reply_text(
        f"When was this money lent?\n\n"
        f"Person: {parsed['person']}\n"
        f"Amount: {format_amount(parsed['amount'])}\n"
        f"Purpose: {parsed['purpose']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Today", callback_data="loan_date_today")],
            [InlineKeyboardButton("Custom date", callback_data="loan_date_custom")],
            [InlineKeyboardButton("Cancel", callback_data="loan_date_cancel")],
        ]),
    )


async def loan_date_today(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_loan")
    if not pending:
        await query.edit_message_text("This transaction has expired.")
        return
    pending["lent_at"] = date_to_iso(date.today())
    await show_loan_confirmation(query, context)


async def loan_date_custom(update, context):
    query = update.callback_query
    await query.answer()
    set_calendar_context(context, "loan_calendar")
    await show_calendar(query, context, "loan_calendar", "Select the lending date:")


async def loan_date_cancel(update, context):
    await calendar_cancel(update, context)


async def show_loan_confirmation(query, context):
    pending = context.user_data.get("pending_loan")
    if not pending:
        await query.edit_message_text("This transaction has expired.")
        return
    await query.edit_message_text(
        f"Record this loan?\n\n"
        f"Person: {pending['person']}\n"
        f"Amount: {format_amount(pending['amount'])}\n"
        f"Purpose: {pending['purpose']}\n"
        f"Date: {display_date(pending['lent_at'])}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm", callback_data="loan_confirm"),
            InlineKeyboardButton("Cancel", callback_data="loan_cancel"),
        ]]),
    )


async def confirm_loan(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_loan")
    if not pending:
        await query.edit_message_text("This transaction has expired.")
        return

    timestamp = now_iso()
    conn = get_db()
    try:
        cursor = conn.execute("""
            INSERT INTO loans(person, amount, purpose, lent_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            pending["person"], pending["amount"], pending["purpose"],
            pending["lent_at"], timestamp, timestamp,
        ))
        loan_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    person, amount, purpose, lent_at = (
        pending["person"], pending["amount"], pending["purpose"], pending["lent_at"]
    )
    clear_flow(context)
    await sync_and_report(
        query,
        f"✓ Recorded loan #{loan_id}\n\n"
        f"{person} — {format_amount(amount)}\n"
        f"{purpose}\n"
        f"Date: {display_date(lent_at)}",
    )


async def cancel_loan(update, context):
    query = update.callback_query
    await query.answer()
    clear_flow(context)
    await query.edit_message_text("Cancelled.")


# ---------------------------------------------------------------------------
# Repayments
# ---------------------------------------------------------------------------

async def returned_command(update, context):
    if not is_allowed(update):
        return
    loans = get_outstanding_loans()
    if not loans:
        await update.message.reply_text("There are no outstanding loans.")
        return

    buttons = [[InlineKeyboardButton(
        f"{loan['person']} — {format_amount(loan['remaining'])} remaining",
        callback_data=f"repay_select:{loan['id']}",
    )] for loan in loans]
    await update.message.reply_text(
        "Outstanding loans:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def select_repayment_loan(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan or loan["remaining"] <= 0:
        await query.edit_message_text("This loan is already fully returned.")
        return

    await query.edit_message_text(
        f"{loan['person']}\n\n"
        f"Original: {format_amount(loan['amount'])}\n"
        f"Repaid: {format_amount(loan['repaid'])}\n"
        f"Remaining: {format_amount(loan['remaining'])}\n"
        f"Purpose: {loan['purpose']}\n"
        f"Lent: {display_date(loan['lent_at'])}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Add repayment", callback_data=f"repay_add:{loan_id}")],
            [InlineKeyboardButton(
                f"Mark remaining {format_amount(loan['remaining'])} as returned",
                callback_data=f"repay_full:{loan_id}",
            )],
            [InlineKeyboardButton("Cancel", callback_data="repay_cancel")],
        ]),
    )


async def start_repayment(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan or loan["remaining"] <= 0:
        await query.edit_message_text("This loan is already fully returned.")
        return

    context.user_data["pending_repayment"] = {
        "loan_id": loan_id,
        "max_amount": loan["remaining"],
    }
    await query.edit_message_text(
        f"How much was returned?\n\n"
        f"{loan['person']} — remaining {format_amount(loan['remaining'])}\n\n"
        "Enter the amount as a whole number."
    )


async def handle_repayment_amount_input(update, context):
    pending = context.user_data.get("pending_repayment")
    amount = parse_integer_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("Enter a valid positive whole-number amount.")
        return

    loan = get_loan(pending["loan_id"])
    if not loan or loan["remaining"] <= 0:
        context.user_data.pop("pending_repayment", None)
        await update.message.reply_text("This loan is already fully returned.")
        return

    if amount > loan["remaining"]:
        await update.message.reply_text(
            f"That amount is too high. The remaining balance is "
            f"{format_amount(loan['remaining'])}."
        )
        return

    pending["amount"] = amount
    set_calendar_context(context, "repayment_calendar")
    await update.message.reply_text(
        f"Repayment: {format_amount(amount)}\n"
        f"Remaining after repayment: {format_amount(loan['remaining'] - amount)}\n\n"
        "When was this repayment made?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Today", callback_data="repay_date_today")],
            [InlineKeyboardButton("Custom date", callback_data="repay_date_custom")],
            [InlineKeyboardButton("Cancel", callback_data="repay_date_cancel")],
        ]),
    )


async def repayment_date_today(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_repayment")
    if not pending:
        await query.edit_message_text("This repayment has expired.")
        return
    pending["paid_at"] = date_to_iso(date.today())
    await show_repayment_confirmation(query, context)


async def repayment_date_custom(update, context):
    query = update.callback_query
    await query.answer()
    set_calendar_context(context, "repayment_calendar")
    await show_calendar(query, context, "repayment_calendar", "Select the repayment date:")


async def repayment_date_cancel(update, context):
    await calendar_cancel(update, context)


async def show_repayment_confirmation(query, context):
    pending = context.user_data.get("pending_repayment")
    if not pending:
        await query.edit_message_text("This repayment has expired.")
        return
    loan = get_loan(pending["loan_id"])
    if not loan:
        clear_flow(context)
        await query.edit_message_text("Loan not found.")
        return
    remaining_after = loan["remaining"] - pending["amount"]
    await query.edit_message_text(
        f"Record repayment?\n\n"
        f"Person: {loan['person']}\n"
        f"Repayment: {format_amount(pending['amount'])}\n"
        f"Date: {display_date(pending['paid_at'])}\n"
        f"Remaining after repayment: {format_amount(remaining_after)}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm", callback_data="repayment_confirm"),
            InlineKeyboardButton("Cancel", callback_data="repayment_cancel"),
        ]]),
    )


async def confirm_repayment(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_repayment")
    if not pending:
        await query.edit_message_text("This repayment has expired.")
        return

    conn = get_db()
    try:
        conn.execute("BEGIN")
        loan = conn.execute("""
            SELECT l.id, l.amount, COALESCE(SUM(r.amount), 0) AS repaid
            FROM loans l
            LEFT JOIN repayments r ON r.loan_id = l.id
            WHERE l.id = ?
            GROUP BY l.id
        """, (pending["loan_id"],)).fetchone()
        if not loan:
            conn.rollback()
            await query.edit_message_text("Loan not found.")
            return

        remaining = loan["amount"] - loan["repaid"]
        if pending["amount"] > remaining:
            conn.rollback()
            await query.edit_message_text(
                f"Repayment exceeds the remaining balance of {format_amount(remaining)}."
            )
            return

        timestamp = now_iso()
        cursor = conn.execute("""
            INSERT INTO repayments(loan_id, amount, paid_at, created_at)
            VALUES (?, ?, ?, ?)
        """, (pending["loan_id"], pending["amount"], pending["paid_at"], timestamp))
        repayment_id = cursor.lastrowid
        conn.execute(
            "UPDATE loans SET updated_at = ? WHERE id = ?",
            (timestamp, pending["loan_id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    loan = get_loan(pending["loan_id"])
    amount = pending["amount"]
    clear_flow(context)
    status = (
        "Loan is now fully returned."
        if loan and loan["remaining"] == 0
        else f"Remaining: {format_amount(loan['remaining'])}"
    )
    await sync_and_report(
        query,
        f"✓ Repayment #{repayment_id} recorded.\n\n"
        f"{loan['person'] if loan else 'Loan'} — {format_amount(amount)}\n"
        f"{status}",
    )


async def full_repayment(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan or loan["remaining"] <= 0:
        await query.edit_message_text("This loan is already fully returned.")
        return

    context.user_data["pending_repayment"] = {
        "loan_id": loan_id,
        "amount": loan["remaining"],
    }
    set_calendar_context(context, "repayment_calendar")
    await query.edit_message_text(
        f"Mark remaining {format_amount(loan['remaining'])} as returned.\n\n"
        "When was the repayment made?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Today", callback_data="repay_date_today")],
            [InlineKeyboardButton("Custom date", callback_data="repay_date_custom")],
            [InlineKeyboardButton("Cancel", callback_data="repay_date_cancel")],
        ]),
    )


async def cancel_repayment(update, context):
    query = update.callback_query
    await query.answer()
    clear_flow(context)
    await query.edit_message_text("Repayment cancelled.")


# ---------------------------------------------------------------------------
# Summary / history / person / search
# ---------------------------------------------------------------------------

async def summary_command(update, context):
    if not is_allowed(update):
        return
    loans = get_outstanding_loans()
    total = sum(loan["remaining"] for loan in loans)
    if not loans:
        await update.message.reply_text("Total amount to be returned: ₹0\n\nNo outstanding loans.")
        return
    lines = [
        f"Total amount to be returned: {format_amount(total)}",
        "",
        f"Outstanding loans: {len(loans)}",
        "",
    ]
    lines.extend(
        f"{loan['person']} — {format_amount(loan['remaining'])}"
        for loan in loans
    )
    await update.message.reply_text("\n".join(lines))


def loan_history_text(loan, include_repayments=True):
    status = "Outstanding" if loan["remaining"] > 0 else "Returned"
    lines = [
        f"#{loan['id']} — {loan['person']}",
        f"Original: {format_amount(loan['amount'])}",
        f"Repaid: {format_amount(loan['repaid'])}",
        f"Remaining: {format_amount(loan['remaining'])}",
        f"Purpose: {loan['purpose']}",
        f"Lent: {display_date(loan['lent_at'])}",
        f"Status: {status}",
    ]
    if include_repayments:
        repayments = get_repayments(loan["id"])
        if repayments:
            lines.append("Repayments:")
            for repayment in repayments:
                lines.append(
                    f"  #{repayment['id']} — {format_amount(repayment['amount'])} "
                    f"on {display_date(repayment['paid_at'])}"
                )
    return "\n".join(lines)


async def history_command(update, context):
    if not is_allowed(update):
        return
    person = " ".join(context.args).strip()
    loans = get_all_loans()
    if person:
        loans = [loan for loan in loans if person.casefold() in loan["person"].casefold()]

    if not loans:
        await update.message.reply_text(
            f"No transactions found for {person}." if person else "No transactions yet."
        )
        return

    lines = ["Transaction history:" if not person else f"History for {person}:", ""]
    for loan in loans:
        lines.append(loan_history_text(loan))
        lines.append("")
    await send_long_text(update.message.reply_text, "\n".join(lines))


async def send_long_text(sender, text):
    # Telegram messages are limited to 4096 characters.
    chunks = []
    while len(text) > 4000:
        split_at = text.rfind("\n", 0, 4000)
        if split_at <= 0:
            split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)
    for chunk in chunks:
        await sender(chunk)


async def person_command(update, context):
    if not is_allowed(update):
        return
    person = " ".join(context.args).strip()
    if not person:
        await update.message.reply_text("Use: /person Full Name")
        return

    loans = [
        loan for loan in get_all_loans()
        if person.casefold() in loan["person"].casefold()
    ]
    if not loans:
        await update.message.reply_text(f"No transactions found for {person}.")
        return

    total_lent = sum(loan["amount"] for loan in loans)
    total_repaid = sum(loan["repaid"] for loan in loans)
    currently_owed = sum(loan["remaining"] for loan in loans)
    outstanding_count = sum(loan["remaining"] > 0 for loan in loans)

    await update.message.reply_text(
        f"{person}\n\n"
        f"Total lent: {format_amount(total_lent)}\n"
        f"Total repaid: {format_amount(total_repaid)}\n"
        f"Currently owed: {format_amount(currently_owed)}\n\n"
        f"Loans: {len(loans)}\n"
        f"Outstanding loans: {outstanding_count}\n"
        f"Returned loans: {len(loans) - outstanding_count}"
    )


async def search_command(update, context):
    if not is_allowed(update):
        return
    query_text = " ".join(context.args).strip()
    if not query_text:
        await update.message.reply_text("Use: /search <name, purpose, amount, or loan ID>")
        return

    loans = get_all_loans()
    q = query_text.casefold()
    results = []
    for loan in loans:
        repayment_rows = get_repayments(loan["id"])
        repayment_text = " ".join(
            f"{r['id']} {r['amount']} {r['paid_at']}" for r in repayment_rows
        )
        haystack = " ".join([
            str(loan["id"]),
            loan["person"],
            str(loan["amount"]),
            str(loan["repaid"]),
            str(loan["remaining"]),
            loan["purpose"],
            loan["lent_at"],
            repayment_text,
        ]).casefold()
        if q in haystack:
            results.append(loan)

    if not results:
        await update.message.reply_text(f'No transactions found for "{query_text}".')
        return

    buttons = [[InlineKeyboardButton(
        loan_label(loan),
        callback_data=f"view_loan:{loan['id']}",
    )] for loan in results]
    await update.message.reply_text(
        f'Search results for "{query_text}":',
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def view_loan(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return

    buttons = []
    if loan["remaining"] > 0:
        buttons.append([
            InlineKeyboardButton("Add repayment", callback_data=f"repay_add:{loan_id}"),
        ])
    buttons.extend([
        [InlineKeyboardButton("Edit", callback_data=f"edit_select:{loan_id}")],
        [InlineKeyboardButton("Delete", callback_data=f"delete_select:{loan_id}")],
    ])
    await query.edit_message_text(
        loan_history_text(loan),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------------------------------------------------------------------------
# Edit loans
# ---------------------------------------------------------------------------

async def edit_command(update, context):
    if not is_allowed(update):
        return
    loans = get_all_loans()
    if not loans:
        await update.message.reply_text("There are no transactions to edit.")
        return
    buttons = [[InlineKeyboardButton(
        loan_label(loan),
        callback_data=f"edit_select:{loan['id']}",
    )] for loan in loans]
    await update.message.reply_text(
        "Select the transaction to edit:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def select_edit(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return

    await query.edit_message_text(
        loan_history_text(loan),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Person", callback_data=f"edit_field:person:{loan_id}")],
            [InlineKeyboardButton("Amount", callback_data=f"edit_field:amount:{loan_id}")],
            [InlineKeyboardButton("Purpose", callback_data=f"edit_field:purpose:{loan_id}")],
            [InlineKeyboardButton("Lending date", callback_data=f"edit_date:{loan_id}")],
            [InlineKeyboardButton("Edit repayment", callback_data=f"edit_repayment_list:{loan_id}")],
            [InlineKeyboardButton("Cancel", callback_data="edit_cancel")],
        ]),
    )


async def edit_field(update, context):
    query = update.callback_query
    await query.answer()
    _, field, loan_id = query.data.split(":")
    loan_id = int(loan_id)
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return

    context.user_data["pending_edit"] = {"loan_id": loan_id, "field": field}
    current = loan[field]
    prompt = {
        "person": f"Current person: {current}\n\nEnter the new full name.",
        "amount": f"Current amount: {format_amount(current)}\n\nEnter the new amount.",
        "purpose": f"Current purpose: {current}\n\nEnter the new purpose.",
    }[field]
    await query.edit_message_text(prompt)


async def handle_edit_input(update, context):
    pending = context.user_data.get("pending_edit")
    loan = get_loan(pending["loan_id"])
    if not loan:
        clear_flow(context)
        await update.message.reply_text("Transaction not found.")
        return

    field = pending["field"]
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("The value cannot be empty.")
        return

    if field == "amount":
        value = parse_integer_amount(value)
        if value is None:
            await update.message.reply_text("Enter a valid positive whole-number amount.")
            return
        if value < loan["repaid"]:
            await update.message.reply_text(
                f"The new amount cannot be less than the already repaid "
                f"{format_amount(loan['repaid'])}."
            )
            return

    conn = get_db()
    conn.execute(
        f"UPDATE loans SET {field} = ?, updated_at = ? WHERE id = ?",
        (value, now_iso(), loan["id"]),
    )
    conn.commit()
    conn.close()

    clear_flow(context)
    await update.message.reply_text(
        f"✓ Updated loan #{loan['id']}."
    )
    try:
        sync_blinko()
    except Exception:
        logger.exception("Blinko synchronization failed after edit.")
        await update.message.reply_text("Blinko synchronization failed; the database change was saved.")


async def edit_date(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return

    context.user_data["pending_edit"] = {"loan_id": loan_id, "field": "lent_at"}
    set_calendar_context(context, "edit_loan_calendar")
    await show_calendar(query, context, "edit_loan_calendar", "Select the new lending date:")


# ---------------------------------------------------------------------------
# Edit repayments
# ---------------------------------------------------------------------------

async def edit_repayment_list(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return
    repayments = get_repayments(loan_id)
    if not repayments:
        await query.edit_message_text(
            "This loan has no repayments.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back", callback_data=f"edit_select:{loan_id}")
            ]]),
        )
        return

    buttons = [[InlineKeyboardButton(
        f"#{r['id']} — {format_amount(r['amount'])} — {display_date(r['paid_at'])}",
        callback_data=f"edit_repay:{r['id']}",
    )] for r in repayments]
    buttons.append([InlineKeyboardButton("Back", callback_data=f"edit_select:{loan_id}")])
    await query.edit_message_text(
        "Select a repayment to edit:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def edit_repayment(update, context):
    query = update.callback_query
    await query.answer()
    repayment_id = int(query.data.split(":")[1])
    repayment = get_repayment(repayment_id)
    if not repayment:
        await query.edit_message_text("Repayment not found.")
        return

    await query.edit_message_text(
        f"Repayment #{repayment['id']}\n"
        f"Person: {repayment['person']}\n"
        f"Amount: {format_amount(repayment['amount'])}\n"
        f"Date: {display_date(repayment['paid_at'])}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Amount", callback_data=f"edit_repay_amount:{repayment_id}")],
            [InlineKeyboardButton("Date", callback_data=f"edit_repay_date:{repayment_id}")],
            [InlineKeyboardButton("Delete repayment", callback_data=f"delete_repay:{repayment_id}")],
            [InlineKeyboardButton("Cancel", callback_data="edit_cancel")],
        ]),
    )


async def edit_repay_amount(update, context):
    query = update.callback_query
    await query.answer()
    repayment_id = int(query.data.split(":")[1])
    repayment = get_repayment(repayment_id)
    if not repayment:
        await query.edit_message_text("Repayment not found.")
        return

    context.user_data["pending_edit"] = {
        "repayment_id": repayment_id,
        "field": "repayment_amount",
    }
    await query.edit_message_text(
        f"Current repayment: {format_amount(repayment['amount'])}\n\n"
        "Enter the new amount."
    )


async def edit_repay_date(update, context):
    query = update.callback_query
    await query.answer()
    repayment_id = int(query.data.split(":")[1])
    repayment = get_repayment(repayment_id)
    if not repayment:
        await query.edit_message_text("Repayment not found.")
        return

    context.user_data["pending_edit"] = {
        "repayment_id": repayment_id,
        "field": "paid_at",
    }
    set_calendar_context(context, "edit_repayment_calendar")
    await show_calendar(query, context, "edit_repayment_calendar", "Select the new repayment date:")


async def handle_edit_repayment_amount(update, context):
    pending = context.user_data.get("pending_edit")
    repayment = get_repayment(pending["repayment_id"])
    if not repayment:
        clear_flow(context)
        await update.message.reply_text("Repayment not found.")
        return

    amount = parse_integer_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("Enter a valid positive whole-number amount.")
        return

    loan = get_loan(repayment["loan_id"])
    max_allowed = loan["remaining"] + repayment["amount"]
    if amount > max_allowed:
        await update.message.reply_text(
            f"The maximum allowed amount is {format_amount(max_allowed)}."
        )
        return

    conn = get_db()
    conn.execute(
        "UPDATE repayments SET amount = ? WHERE id = ?",
        (amount, repayment["id"]),
    )
    conn.execute(
        "UPDATE loans SET updated_at = ? WHERE id = ?",
        (now_iso(), repayment["loan_id"]),
    )
    conn.commit()
    conn.close()
    clear_flow(context)
    await update.message.reply_text(f"✓ Repayment #{repayment['id']} updated.")
    try:
        sync_blinko()
    except Exception:
        logger.exception("Blinko synchronization failed after repayment edit.")
        await update.message.reply_text("Blinko synchronization failed; the database change was saved.")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

async def delete_command(update, context):
    if not is_allowed(update):
        return
    loans = get_all_loans()
    if not loans:
        await update.message.reply_text("There are no transactions to delete.")
        return
    buttons = [[InlineKeyboardButton(
        loan_label(loan),
        callback_data=f"delete_select:{loan['id']}",
    )] for loan in loans]
    await update.message.reply_text(
        "Select the transaction to permanently delete:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def select_delete(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return

    await query.edit_message_text(
        f"PERMANENTLY delete this transaction?\n\n"
        f"{loan_history_text(loan)}\n\n"
        "All repayments for this loan will also be deleted.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, delete permanently", callback_data=f"delete_confirm:{loan_id}"),
            InlineKeyboardButton("Cancel", callback_data="delete_cancel"),
        ]]),
    )


async def confirm_delete(update, context):
    query = update.callback_query
    await query.answer()
    loan_id = int(query.data.split(":")[1])
    loan = get_loan(loan_id)
    if not loan:
        await query.edit_message_text("Transaction not found.")
        return

    conn = get_db()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    await sync_and_report(
        query,
        f"✓ Transaction #{loan_id} permanently deleted.",
    )


async def cancel_delete(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Delete cancelled.")


async def delete_repayment(update, context):
    query = update.callback_query
    await query.answer()
    repayment_id = int(query.data.split(":")[1])
    repayment = get_repayment(repayment_id)
    if not repayment:
        await query.edit_message_text("Repayment not found.")
        return

    await query.edit_message_text(
        f"Delete repayment #{repayment['id']}?\n\n"
        f"{repayment['person']} — {format_amount(repayment['amount'])}\n"
        f"Date: {display_date(repayment['paid_at'])}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, delete", callback_data=f"delete_repay_confirm:{repayment_id}"),
            InlineKeyboardButton("Cancel", callback_data="edit_cancel"),
        ]]),
    )


async def confirm_delete_repayment(update, context):
    query = update.callback_query
    await query.answer()
    repayment_id = int(query.data.split(":")[1])
    repayment = get_repayment(repayment_id)
    if not repayment:
        await query.edit_message_text("Repayment not found.")
        return

    conn = get_db()
    conn.execute("DELETE FROM repayments WHERE id = ?", (repayment_id,))
    conn.execute("UPDATE loans SET updated_at = ? WHERE id = ?", (now_iso(), repayment["loan_id"]))
    conn.commit()
    conn.close()

    await sync_and_report(query, f"✓ Repayment #{repayment_id} deleted.")


# ---------------------------------------------------------------------------
# Calendar handlers for edit dates
# ---------------------------------------------------------------------------

async def edit_calendar_today(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_edit")
    if not pending:
        await query.edit_message_text("This edit has expired.")
        return

    if pending["field"] == "lent_at":
        value = date_to_iso(date.today())
        conn = get_db()
        conn.execute("UPDATE loans SET lent_at = ?, updated_at = ? WHERE id = ?",
                     (value, now_iso(), pending["loan_id"]))
        conn.commit()
        conn.close()
        clear_flow(context)
        await query.edit_message_text("✓ Lending date updated.")
    elif pending["field"] == "paid_at":
        value = date_to_iso(date.today())
        repayment = get_repayment(pending["repayment_id"])
        if not repayment:
            clear_flow(context)
            await query.edit_message_text("Repayment not found.")
            return
        conn = get_db()
        conn.execute("UPDATE repayments SET paid_at = ? WHERE id = ?",
                     (value, pending["repayment_id"]))
        conn.execute("UPDATE loans SET updated_at = ? WHERE id = ?",
                     (now_iso(), repayment["loan_id"]))
        conn.commit()
        conn.close()
        clear_flow(context)
        await query.edit_message_text("✓ Repayment date updated.")
    try:
        sync_blinko()
    except Exception:
        logger.exception("Blinko synchronization failed after date edit.")
        await query.edit_message_text("✓ Date updated, but Blinko synchronization failed.")


async def edit_calendar_day(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_edit")
    if not pending:
        await query.edit_message_text("This edit has expired.")
        return

    parts = query.data.split(":")
    selected = date(int(parts[1]), int(parts[2]), int(parts[3]))
    value = date_to_iso(selected)

    if pending["field"] == "lent_at":
        conn = get_db()
        conn.execute("UPDATE loans SET lent_at = ?, updated_at = ? WHERE id = ?",
                     (value, now_iso(), pending["loan_id"]))
        conn.commit()
        conn.close()
        message = "✓ Lending date updated."
    else:
        repayment = get_repayment(pending["repayment_id"])
        if not repayment:
            clear_flow(context)
            await query.edit_message_text("Repayment not found.")
            return
        conn = get_db()
        conn.execute("UPDATE repayments SET paid_at = ? WHERE id = ?",
                     (value, pending["repayment_id"]))
        conn.execute("UPDATE loans SET updated_at = ? WHERE id = ?",
                     (now_iso(), repayment["loan_id"]))
        conn.commit()
        conn.close()
        message = "✓ Repayment date updated."

    clear_flow(context)
    try:
        sync_blinko()
        await query.edit_message_text(message)
    except Exception:
        logger.exception("Blinko synchronization failed after date edit.")
        await query.edit_message_text(message + "\n\nBlinko synchronization failed.")


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start_command(update, context):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Money Tracker\n\n"
        "Add a loan:\n"
        "Full Name Amount Purpose\n\n"
        "Example:\n"
        "Rahul Kumar 500 dinner\n\n"
        "Commands:\n"
        "/returned — outstanding loans and repayments\n"
        "/summary — total amount currently owed\n"
        "/history — complete transaction history\n"
        "/history <person> — history for one person\n"
        "/person <name> — summary for one person\n"
        "/search <query> — search transactions\n"
        "/edit — edit a loan or repayment\n"
        "/delete — permanently delete a loan"
    )

# ---------------------------------------------------------------------------
# Homepage dashboard API
# ---------------------------------------------------------------------------

DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))


def get_dashboard_data():
    outstanding = get_outstanding_loans()

    outstanding_amount = sum(
        int(loan["remaining"])
        for loan in outstanding
    )

    people_owing = len({
        loan["person"].strip().lower()
        for loan in outstanding
    })

    current_month = date.today().strftime("%Y-%m")

    conn = get_db()

    lent_row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM loans
        WHERE substr(lent_at, 1, 7) = ?
        """,
        (current_month,),
    ).fetchone()

    repaid_row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM repayments
        WHERE substr(paid_at, 1, 7) = ?
        """,
        (current_month,),
    ).fetchone()

    conn.close()

    return {
        "outstanding_amount": outstanding_amount,
        "outstanding_loans": len(outstanding),
        "people_owing": people_owing,
        "lent_this_month": int(lent_row["total"]),
        "repaid_this_month": int(repaid_row["total"]),
    }


class DashboardHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path != "/api/dashboard":
            self.send_response(404)
            self.end_headers()
            return

        try:
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        except Exception:
            logger.exception("Dashboard API failed")

            body = b'{"error":"internal server error"}'

            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_dashboard_server():
    server = ThreadingHTTPServer(
        (DASHBOARD_HOST, DASHBOARD_PORT),
        DashboardHandler,
    )

    logger.info(
        "Dashboard API listening on %s:%s",
        DASHBOARD_HOST,
        DASHBOARD_PORT,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    init_db()
    start_dashboard_server()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("returned", returned_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("person", person_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("edit", edit_command))
    application.add_handler(CommandHandler("delete", delete_command))

    # Loan dates.
    application.add_handler(CallbackQueryHandler(loan_date_today, pattern=r"^loan_date_today$"))
    application.add_handler(CallbackQueryHandler(loan_date_custom, pattern=r"^loan_date_custom$"))
    application.add_handler(CallbackQueryHandler(loan_date_cancel, pattern=r"^loan_date_cancel$"))
    application.add_handler(CallbackQueryHandler(confirm_loan, pattern=r"^loan_confirm$"))
    application.add_handler(CallbackQueryHandler(cancel_loan, pattern=r"^loan_cancel$"))

    # Generic calendar navigation.
    application.add_handler(CallbackQueryHandler(calendar_navigation, pattern=r"^(loan_calendar|repayment_calendar|edit_loan_calendar|edit_repayment_calendar)_(prev|next)$"))
    application.add_handler(CallbackQueryHandler(calendar_noop, pattern=r"^(loan_calendar|repayment_calendar|edit_loan_calendar|edit_repayment_calendar)_noop$"))
    application.add_handler(CallbackQueryHandler(calendar_cancel, pattern=r"^(loan_calendar|repayment_calendar|edit_loan_calendar|edit_repayment_calendar)_cancel$"))

    # Loan/repayment calendar days and today.
    application.add_handler(CallbackQueryHandler(calendar_day, pattern=r"^(loan_calendar|repayment_calendar)_day:"))
    application.add_handler(CallbackQueryHandler(calendar_today, pattern=r"^(loan_calendar|repayment_calendar)_today$"))

    # Repayments.
    application.add_handler(CallbackQueryHandler(select_repayment_loan, pattern=r"^repay_select:"))
    application.add_handler(CallbackQueryHandler(start_repayment, pattern=r"^repay_add:"))
    application.add_handler(CallbackQueryHandler(full_repayment, pattern=r"^repay_full:"))
    application.add_handler(CallbackQueryHandler(repayment_date_today, pattern=r"^repay_date_today$"))
    application.add_handler(CallbackQueryHandler(repayment_date_custom, pattern=r"^repay_date_custom$"))
    application.add_handler(CallbackQueryHandler(repayment_date_cancel, pattern=r"^repay_date_cancel$"))
    application.add_handler(CallbackQueryHandler(confirm_repayment, pattern=r"^repayment_confirm$"))
    application.add_handler(CallbackQueryHandler(cancel_repayment, pattern=r"^repayment_cancel$"))
    application.add_handler(CallbackQueryHandler(cancel_repayment, pattern=r"^repay_cancel$"))

    # Search/view.
    application.add_handler(CallbackQueryHandler(view_loan, pattern=r"^view_loan:"))

    # Edit loans.
    application.add_handler(CallbackQueryHandler(select_edit, pattern=r"^edit_select:"))
    application.add_handler(CallbackQueryHandler(edit_field, pattern=r"^edit_field:"))
    application.add_handler(CallbackQueryHandler(edit_date, pattern=r"^edit_date:"))
    application.add_handler(CallbackQueryHandler(edit_repayment_list, pattern=r"^edit_repayment_list:"))
    application.add_handler(CallbackQueryHandler(edit_repayment, pattern=r"^edit_repay:"))
    application.add_handler(CallbackQueryHandler(edit_repay_amount, pattern=r"^edit_repay_amount:"))
    application.add_handler(CallbackQueryHandler(edit_repay_date, pattern=r"^edit_repay_date:"))
    application.add_handler(CallbackQueryHandler(delete_repayment, pattern=r"^delete_repay:"))
    application.add_handler(CallbackQueryHandler(confirm_delete_repayment, pattern=r"^delete_repay_confirm:"))
    application.add_handler(CallbackQueryHandler(edit_calendar_today, pattern=r"^edit_(loan|repayment)_calendar_today$"))
    application.add_handler(CallbackQueryHandler(edit_calendar_day, pattern=r"^edit_(loan|repayment)_calendar_day:"))
    application.add_handler(CallbackQueryHandler(lambda update, context: edit_cancel(update, context), pattern=r"^edit_cancel$"))

    # Delete loans.
    application.add_handler(CallbackQueryHandler(select_delete, pattern=r"^delete_select:"))
    application.add_handler(CallbackQueryHandler(confirm_delete, pattern=r"^delete_confirm:"))
    application.add_handler(CallbackQueryHandler(cancel_delete, pattern=r"^delete_cancel$"))

    # Free text is last so state-specific input can be handled.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Money Tracker bot starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


async def edit_cancel(update, context):
    query = update.callback_query
    await query.answer()
    clear_flow(context)
    await query.edit_message_text("Edit cancelled.")


if __name__ == "__main__":
    main()
