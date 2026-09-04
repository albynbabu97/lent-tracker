import logging
import os
import re
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            amount INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            lent_at TEXT NOT NULL,
            returned INTEGER NOT NULL DEFAULT 0,
            returned_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def get_setting(key):
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    ).fetchone()
    conn.close()

    return row["value"] if row else None


def set_setting(key, value):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def is_allowed(update: Update):
    if not ALLOWED_USER_ID:
        return True

    user = update.effective_user

    if not user:
        return False

    return str(user.id) == ALLOWED_USER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_amount(amount):
    return f"₹{amount:,}"


def get_outstanding_loans():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM loans
        WHERE returned = 0
        ORDER BY lent_at DESC, id DESC
        """
    ).fetchall()

    conn.close()

    return rows


def get_all_loans():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM loans
        ORDER BY lent_at DESC, id DESC
        """
    ).fetchall()

    conn.close()

    return rows


def get_loan(loan_id):
    conn = get_db()

    row = conn.execute(
        "SELECT * FROM loans WHERE id = ?",
        (loan_id,),
    ).fetchone()

    conn.close()

    return row


# ---------------------------------------------------------------------------
# Blinko
# ---------------------------------------------------------------------------

def build_blinko_content():
    loans = get_outstanding_loans()

    lines = [
        "# Money Lent",
        "",
    ]

    if not loans:
        lines.append("No outstanding loans.")
        return "\n".join(lines)

    lines.extend(
        [
            "| Person | Amount | Purpose | Date |",
            "|---|---:|---|---|",
        ]
    )

    for loan in loans:
        date = loan["lent_at"][:10]

        purpose = loan["purpose"].replace("|", "\\|")
        person = loan["person"].replace("|", "\\|")

        lines.append(
            f"| {person} | {format_amount(loan['amount'])} | "
            f"{purpose} | {date} |"
        )

    return "\n".join(lines)


def sync_blinko():
    content = build_blinko_content()

    note_id = get_setting("blinko_note_id")

    payload = {
        "content": content,
        "type": 1,
        "isShare": False,
    }

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
        logger.info("Created Blinko ledger note: %s", returned_id)

    logger.info("Blinko synchronized")


# ---------------------------------------------------------------------------
# Loan creation
# ---------------------------------------------------------------------------

def parse_loan_message(text):
    """
    Expected:

        Ravi 500 lunch

    The first word is the person.
    The second token is the amount.
    Everything after the amount is the purpose.
    """

    text = text.strip()

    match = re.match(
        r"^(\S+)\s+([0-9]+(?:\.[0-9]{1,2})?)\s+(.+)$",
        text,
    )

    if not match:
        return None

    person = match.group(1)
    amount_text = match.group(2)
    purpose = match.group(3).strip()

    try:
        amount_decimal = Decimal(amount_text)

        if amount_decimal <= 0:
            return None

        if amount_decimal != amount_decimal.quantize(Decimal("1")):
            return None

        amount = int(amount_decimal)

    except InvalidOperation:
        return None

    return person, amount, purpose


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    parsed = parse_loan_message(text)

    if not parsed:
        await update.message.reply_text(
            "I couldn't understand that.\n\n"
            "Use:\n"
            "`Person Amount Purpose`\n\n"
            "Example:\n"
            "`Ravi 500 lunch`",
            parse_mode="Markdown",
        )
        return

    person, amount, purpose = parsed

    context.user_data["pending_loan"] = {
        "person": person,
        "amount": amount,
        "purpose": purpose,
    }

    keyboard = [
        [
            InlineKeyboardButton(
                "Confirm",
                callback_data="confirm_loan",
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data="cancel_loan",
            ),
        ]
    ]

    await update.message.reply_text(
        f"Record this loan?\n\n"
        f"Person: {person}\n"
        f"Amount: {format_amount(amount)}\n"
        f"Purpose: {purpose}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def confirm_loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending_loan")

    if not pending:
        await query.edit_message_text("This transaction has expired.")
        return

    conn = get_db()

    lent_at = datetime.now().astimezone().isoformat()

    cursor = conn.execute(
        """
        INSERT INTO loans (
            person,
            amount,
            purpose,
            lent_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            pending["person"],
            pending["amount"],
            pending["purpose"],
            lent_at,
        ),
    )

    loan_id = cursor.lastrowid

    conn.commit()
    conn.close()

    context.user_data.pop("pending_loan", None)

    try:
        sync_blinko()

        await query.edit_message_text(
            f"✓ Recorded loan #{loan_id}\n\n"
            f"{pending['person']} — "
            f"{format_amount(pending['amount'])}\n"
            f"{pending['purpose']}"
        )

    except Exception:
        logger.exception("Blinko synchronization failed")

        await query.edit_message_text(
            f"✓ Recorded loan #{loan_id}, but Blinko synchronization failed.\n\n"
            "The transaction is safely stored in SQLite."
        )


async def cancel_loan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data.pop("pending_loan", None)

    await query.edit_message_text("Cancelled.")


# ---------------------------------------------------------------------------
# /returned
# ---------------------------------------------------------------------------

async def returned_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    loans = get_outstanding_loans()

    if not loans:
        await update.message.reply_text(
            "There are no outstanding loans."
        )
        return

    buttons = []

    for loan in loans:
        label = (
            f"{loan['person']} — "
            f"{format_amount(loan['amount'])}"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"return_select:{loan['id']}",
                )
            ]
        )

    await update.message.reply_text(
        "Outstanding loans:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def select_return(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    loan_id = int(query.data.split(":")[1])

    loan = get_loan(loan_id)

    if not loan or loan["returned"]:
        await query.edit_message_text(
            "This loan has already been returned."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "Yes, returned",
                callback_data=f"return_confirm:{loan_id}",
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data="return_cancel",
            ),
        ]
    ]

    await query.edit_message_text(
        f"Mark this loan as returned?\n\n"
        f"{loan['person']} — {format_amount(loan['amount'])}\n"
        f"{loan['purpose']}\n"
        f"Lent: {loan['lent_at'][:10]}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def confirm_return(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    loan_id = int(query.data.split(":")[1])

    loan = get_loan(loan_id)

    if not loan:
        await query.edit_message_text("Loan not found.")
        return

    if loan["returned"]:
        await query.edit_message_text(
            "This loan has already been returned."
        )
        return

    returned_at = datetime.now().astimezone().isoformat()

    conn = get_db()

    conn.execute(
        """
        UPDATE loans
        SET returned = 1,
            returned_at = ?
        WHERE id = ?
        """,
        (returned_at, loan_id),
    )

    conn.commit()
    conn.close()

    try:
        sync_blinko()

        await query.edit_message_text(
            f"✓ Marked as returned.\n\n"
            f"{loan['person']} — "
            f"{format_amount(loan['amount'])}"
        )

    except Exception:
        logger.exception("Blinko synchronization failed")

        await query.edit_message_text(
            f"✓ Marked as returned in SQLite.\n\n"
            "Blinko synchronization failed."
        )


async def cancel_return(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Cancelled.")


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    loans = get_outstanding_loans()

    if not loans:
        await update.message.reply_text(
            "Outstanding: ₹0\n\nNo outstanding loans."
        )
        return

    total = sum(loan["amount"] for loan in loans)

    lines = [
        f"Outstanding: {format_amount(total)}",
        "",
    ]

    for loan in loans:
        lines.append(
            f"{loan['person']} — {format_amount(loan['amount'])}"
        )

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    loans = get_all_loans()

    if not loans:
        await update.message.reply_text("No transactions yet.")
        return

    lines = ["Transaction history:", ""]

    for loan in loans:
        status = "Returned" if loan["returned"] else "Outstanding"

        lines.extend(
            [
                f"#{loan['id']} — {loan['person']}",
                f"{format_amount(loan['amount'])} — {loan['purpose']}",
                f"Lent: {loan['lent_at'][:10]}",
                f"Status: {status}",
            ]
        )

        if loan["returned"] and loan["returned_at"]:
            lines.append(
                f"Returned: {loan['returned_at'][:10]}"
            )

        lines.append("")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "Money Tracker\n\n"
        "Add a loan:\n"
        "Ravi 500 lunch\n\n"
        "Commands:\n"
        "/returned — mark a loan as returned\n"
        "/summary — outstanding balance\n"
        "/history — transaction history"
    )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def main():
    init_db()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("returned", returned_command)
    )

    application.add_handler(
        CommandHandler("summary", summary_command)
    )

    application.add_handler(
        CommandHandler("history", history_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            confirm_loan,
            pattern=r"^confirm_loan$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_loan,
            pattern=r"^cancel_loan$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            select_return,
            pattern=r"^return_select:",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            confirm_return,
            pattern=r"^return_confirm:",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_return,
            pattern=r"^return_cancel$",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Money Tracker bot starting")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
