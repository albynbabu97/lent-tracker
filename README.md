# Money Tracker Bot

A self-hosted Telegram bot for tracking money lent to other people.

The bot provides a mobile-first interface for recording loans, selecting dates from a calendar, tracking partial and multiple repayments, searching transactions, editing records, and viewing outstanding balances.

SQLite is the **source of truth** for all financial data.

Blinko contains a **single live note showing only currently outstanding loans**.

---

# Architecture

```text
                         ┌──────────────────┐
                         │     Telegram     │
                         │       Bot        │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │  Money Tracker   │
                         │      Bot         │
                         └────────┬─────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
                    ▼                           ▼
             ┌──────────────┐            ┌──────────────┐
             │    SQLite    │            │    Blinko    │
             │ Source of    │            │ Live view of │
             │    truth     │            │ outstanding  │
             └──────────────┘            │    loans     │
                                         └──────────────┘
```

The application is deployed using Docker Compose.

The Docker image is built by GitHub Actions and published to GitHub Container Registry (GHCR).

```text
GitHub
   │
   ▼
GitHub Actions
   │
   ▼
GHCR Docker Image
   │
   ▼
Homelab Docker Compose
   │
   ▼
Money Tracker Container
```

---

# Features

## Loan management

* Add loans through Telegram
* Full-name support
* Amount tracking
* Purpose tracking
* Custom lending date
* Interactive calendar date picker
* Confirmation before saving
* Edit existing transactions
* Permanently delete transactions

## Repayment management

* Mark a loan completely returned
* Record partial repayments
* Record multiple repayments against the same loan
* Automatically calculate outstanding balance
* Track the date of every repayment
* Automatically mark a loan as fully returned when the balance reaches ₹0

## Search and history

* `/history`
* `/history <person>`
* `/person <name>`
* `/search <query>`
* Search by:

  * Person
  * Purpose
  * Amount
  * Transaction ID
* View complete transaction history
* View outstanding transactions
* View repayment history

## Blinko integration

* Maintain exactly **one Blinko note**
* Show only outstanding loans
* Show remaining balance rather than original amount
* Automatically update Blinko after:

  * New loan
  * Repayment
  * Full return
  * Edit
  * Delete

## Reliability

* SQLite persistence
* Docker persistent storage
* Restic-compatible database backup
* SQLite is independent of Blinko
* Blinko can be rebuilt from SQLite
* No public HTTP endpoint required for Telegram bot operation

---

# Adding a Loan

Send:

```text
Full Name Amount Purpose
```

Example:

```text
Rahul Kumar 500 dinner
```

The bot asks:

```text
When was this money lent?

[ Today ]
[ Custom date ]
[ Cancel ]
```

---

# Date Selection

## Today

Selecting `Today` uses the current date.

## Custom date

Selecting `Custom date` opens an interactive calendar.

Example:

```text
       September 2026

[Mo] [Tu] [We] [Th] [Fr] [Sa] [Su]
     [1]  [2]  [3]  [4]  [5]  [6]
 [7] [8]  [9] [10] [11] [12] [13]
[14] [15] [16] [17] [18] [19] [20]
[21] [22] [23] [24] [25] [26] [27]
[28] [29] [30]

[◀] [September 2026] [▶]

[Today] [Cancel]
```

The selected date is then used for the transaction.

---

# Loan Confirmation

After selecting the date:

```text
Record this loan?

Person: Rahul Kumar
Amount: ₹500
Purpose: dinner
Date: 02-09-2026

[ Confirm ] [ Cancel ]
```

Selecting `Confirm` stores the transaction in SQLite and synchronizes Blinko.

---

# Database Model

Because a loan can have multiple repayments, loans and repayments are stored separately.

```text
loans
├── id
├── person
├── amount
├── purpose
├── lent_at
├── created_at
└── updated_at

repayments
├── id
├── loan_id
├── amount
├── paid_at
└── created_at
```

Relationship:

```text
Loan #12
₹5,000
│
├── Repayment #1
│   ₹1,000
│
├── Repayment #2
│   ₹1,500
│
└── Repayment #3
    ₹500
```

Total repaid:

```text
₹3,000
```

Outstanding:

```text
₹5,000 - ₹3,000 = ₹2,000
```

The outstanding balance is calculated from the repayment records rather than stored as an independent value.

This prevents the balance from becoming inconsistent.

---

# Partial Repayments

A borrower does not have to return the entire amount at once.

For example:

```text
Original loan: ₹5,000
```

The person returns:

```text
₹1,000
```

The bot records:

```text
Repayment: ₹1,000
Remaining: ₹4,000
```

The loan remains outstanding.

Blinko displays:

```text
| Person | Original | Repaid | Remaining | Purpose | Date |
|---|---:|---:|---:|---|---|
| Rahul Kumar | ₹5,000 | ₹1,000 | ₹4,000 | dinner | 02-09-2026 |
```

---

# Multiple Repayments

The same loan can have any number of repayments.

Example:

```text
Original loan: ₹10,000

Repayment 1: ₹2,000
Repayment 2: ₹3,000
Repayment 3: ₹1,000
```

The database contains three separate repayment records.

```text
Total repaid: ₹6,000
Remaining: ₹4,000
```

The original loan remains one transaction.

This is important because it preserves the complete repayment history.

---

# Full Repayment

When the total repayments equal the original loan amount:

```text
Original: ₹5,000
Repaid:   ₹5,000
Remaining: ₹0
```

The loan is automatically considered fully returned.

It disappears from the Blinko outstanding-loans note.

The loan and all repayment records remain in SQLite.

---

# `/returned`

Displays outstanding loans.

Example:

```text
Outstanding loans:

[Rahul Kumar — ₹4,000]
[Anu Thomas — ₹1,200]
[John — ₹300]
```

Selecting a loan displays repayment options.

```text
Rahul Kumar

Original: ₹5,000
Repaid: ₹1,000
Remaining: ₹4,000

[Add repayment]
[Mark remaining ₹4,000 as returned]
[Cancel]
```

## Add repayment

Selecting `Add repayment` asks for the repayment amount.

Example:

```text
How much was returned?

Enter amount:
```

The user enters:

```text
1500
```

The bot then asks for the repayment date:

```text
When was this repayment made?

[ Today ]
[ Custom date ]
[ Cancel ]
```

After selecting the date:

```text
Record repayment?

Person: Rahul Kumar
Repayment: ₹1,500
Date: 04-09-2026
Remaining after repayment: ₹2,500

[ Confirm ] [ Cancel ]
```

The repayment is then added to the `repayments` table.

---

# Preventing Invalid Repayments

A repayment cannot exceed the current outstanding balance.

For example:

```text
Original: ₹5,000
Already repaid: ₹3,000
Remaining: ₹2,000
```

Trying to enter:

```text
₹2,500
```

will be rejected.

The bot will explain:

```text
The repayment cannot exceed the remaining balance of ₹2,000.
```

This prevents negative balances.

---

# `/summary`

Displays the total amount currently owed to you.

Example:

```text
Total amount to be returned: ₹7,500

Outstanding loans: 3

Rahul Kumar — ₹2,500
Anu Thomas — ₹4,000
John — ₹1,000
```

The total is calculated from current outstanding balances.

Returned loans are excluded.

---

# `/history`

Displays complete transaction history.

Example:

```text
Transaction history:

#12 — Rahul Kumar
Original: ₹5,000
Repaid: ₹2,500
Remaining: ₹2,500
Purpose: dinner
Lent: 02-09-2026
Status: Outstanding

#11 — Anu Thomas
Original: ₹1,200
Repaid: ₹1,200
Remaining: ₹0
Purpose: shopping
Lent: 01-09-2026
Status: Returned
```

Returned transactions remain available.

---

# `/history <person>`

Shows the transaction history for a specific person.

Example:

```text
/history Rahul Kumar
```

The bot returns all loans associated with Rahul Kumar.

Example:

```text
Rahul Kumar

#12
Original: ₹5,000
Repaid: ₹2,500
Remaining: ₹2,500
Purpose: dinner
Lent: 02-09-2026
Status: Outstanding

#8
Original: ₹2,000
Repaid: ₹2,000
Remaining: ₹0
Purpose: fuel
Lent: 20-08-2026
Status: Returned
```

The command should support names containing spaces.

---

# `/person <name>`

Provides a summary for one person.

Example:

```text
/person Rahul Kumar
```

Result:

```text
Rahul Kumar

Total lent: ₹7,000
Total repaid: ₹4,500
Currently owed: ₹2,500

Loans: 2
Outstanding loans: 1
Returned loans: 1
```

This is different from `/history`.

`/history Rahul Kumar` shows the transactions.

`/person Rahul Kumar` shows the person's overall financial summary.

---

# Search

The bot provides:

```text
/search <query>
```

Examples:

```text
/search Rahul
```

```text
/search dinner
```

```text
/search 500
```

Search should operate across:

* Person name
* Purpose
* Loan ID
* Original amount
* Repayment information where appropriate

Example:

```text
/search dinner
```

Result:

```text
Search results for "dinner":

#12 — Rahul Kumar
₹5,000
dinner
Remaining: ₹2,500
02-09-2026

#7 — John
₹1,500
dinner
Returned
20-08-2026
```

Search results should use Telegram inline buttons so a transaction can be selected for further actions.

---

# Editing Transactions

The bot provides:

```text
/edit
```

The bot displays transactions that can be edited.

Example:

```text
Select transaction:

[#12 Rahul Kumar — ₹5,000]
[#11 Anu Thomas — ₹1,200]
[#10 John — ₹300]
```

Selecting a transaction shows:

```text
Edit transaction #12

[Person]
[Amount]
[Purpose]
[Date]
[Cancel]
```

The user can modify individual fields.

Example:

```text
Person:
Rahul Kumar
```

can be changed to:

```text
Rahul Kumar Nair
```

The same applies to:

* Person
* Original amount
* Purpose
* Lending date

After modification:

```text
Save changes?

Person: Rahul Kumar Nair
Amount: ₹5,000
Purpose: dinner
Date: 02-09-2026

[ Save ] [ Cancel ]
```

Blinko is synchronized after the change.

---

# Editing Amounts With Repayments

Editing an original loan amount requires validation.

Example:

```text
Original loan: ₹5,000
Repayments: ₹3,000
```

The user cannot change the original loan to:

```text
₹2,000
```

because repayments already exceed the new loan amount.

The bot must reject the change.

The new original amount must always be greater than or equal to the total repayments already recorded.

Example:

```text
Original: ₹5,000
Repaid: ₹3,000

Valid:
₹3,000
₹4,000
₹5,000
₹6,000

Invalid:
₹2,999
₹2,000
```

This prevents inconsistent financial records.

---

# Editing Repayments

Repayments should also be individually identifiable.

For example:

```text
Loan #12

Repayments:

[#21 — ₹1,000 — 03-09-2026]
[#24 — ₹1,500 — 04-09-2026]
```

Selecting a repayment allows:

```text
[Edit amount]
[Edit date]
[Delete repayment]
[Cancel]
```

Changing a repayment amount must never allow the total repayments to exceed the original loan amount.

---

# `/delete`

Displays transactions for permanent deletion.

Example:

```text
Select transaction:

[#12 Rahul Kumar — ₹5,000 — ₹2,500 remaining]
[#11 Anu Thomas — ₹1,200 — Returned]
```

Selecting a transaction shows a confirmation.

```text
Permanently delete this transaction?

#12
Rahul Kumar
Original: ₹5,000
Repaid: ₹2,500
Remaining: ₹2,500

This will also delete all repayment records.

[Delete permanently]
[Cancel]
```

Deleting a loan also deletes its associated repayments.

The deletion should use a database transaction so the loan and its repayments cannot be left in an inconsistent state.

---

# Blinko Integration

Blinko contains exactly **one note** for outstanding loans.

Example:

```text
# Money Lent

| Person | Original | Repaid | Remaining | Purpose | Date |
|---|---:|---:|---:|---|---|
| Rahul Kumar | ₹5,000 | ₹2,500 | ₹2,500 | dinner | 02-09-2026 |
| Anu Thomas | ₹1,200 | ₹0 | ₹1,200 | shopping | 01-09-2026 |
```

Only loans with a remaining balance greater than ₹0 appear.

---

# Blinko Is Not the Source of Truth

SQLite is authoritative.

Blinko is a generated view.

```text
                    SQLite
                      │
          ┌───────────┴───────────┐
          │                       │
          ▼                       ▼
     All transactions       Outstanding
          │                  balances
          │                       │
          │                       ▼
          │                    Blinko
          │                 Single note
          │
          ├── Outstanding
          └── Returned
```

This means:

* Blinko can be deleted without losing financial data.
* Returned transactions remain in SQLite.
* Repayment history remains in SQLite.
* Blinko can be regenerated from SQLite.

---

# Blinko Synchronization

Blinko should be synchronized after every operation that affects the outstanding balance or displayed transaction information.

These operations include:

```text
Add loan
   ↓
Blinko sync

Edit loan
   ↓
Blinko sync

Add repayment
   ↓
Blinko sync

Edit repayment
   ↓
Blinko sync

Delete repayment
   ↓
Blinko sync

Fully repay loan
   ↓
Blinko sync

Delete loan
   ↓
Blinko sync
```

If Blinko is temporarily unavailable, the database operation should still succeed.

The bot should report the synchronization failure rather than losing the transaction.

A subsequent synchronization can rebuild the Blinko note from SQLite.

---

# Data Integrity

All financial calculations should be derived from the database.

For every loan:

```text
total_repaid = SUM(repayments.amount)

outstanding =
    loan.amount - total_repaid
```

A loan is considered returned when:

```text
outstanding == 0
```

A loan is outstanding when:

```text
outstanding > 0
```

Negative outstanding balances must never be permitted.

---

# Database Transactions

Operations affecting multiple records must use SQLite transactions.

For example, deleting a loan:

```text
BEGIN TRANSACTION

Delete repayments
Delete loan

COMMIT
```

If anything fails:

```text
ROLLBACK
```

This prevents orphaned repayment records.

---

# Database Schema

Recommended schema:

```sql
CREATE TABLE loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    purpose TEXT NOT NULL,
    lent_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

```sql
CREATE TABLE repayments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    paid_at TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (loan_id)
        REFERENCES loans(id)
        ON DELETE CASCADE
);
```

Useful indexes:

```sql
CREATE INDEX idx_loans_person
ON loans(person);
```

```sql
CREATE INDEX idx_loans_lent_at
ON loans(lent_at);
```

```sql
CREATE INDEX idx_repayments_loan_id
ON repayments(loan_id);
```

---

# Currency

Amounts are stored as integer currency units.

For Indian Rupees:

```text
₹500
```

is stored as:

```text
500
```

This avoids floating-point rounding problems.

Decimal currency values should not be used unless there is a specific requirement for paise.

---

# Telegram Commands

| Command             | Purpose                                 |
| ------------------- | --------------------------------------- |
| `/start`            | Show help                               |
| `/summary`          | Total currently owed                    |
| `/returned`         | Manage outstanding loans and repayments |
| `/history`          | Complete transaction history            |
| `/history <person>` | History for a person                    |
| `/person <name>`    | Person-level financial summary          |
| `/search <query>`   | Search transactions                     |
| `/edit`             | Edit a transaction                      |
| `/delete`           | Permanently delete a transaction        |

---

# Example Complete Workflow

## 1. Create loan

```text
Rahul Kumar 5000 laptop
```

Select:

```text
[Today]
```

Confirm.

Database:

```text
Loan #1
Original: ₹5,000
Repaid: ₹0
Remaining: ₹5,000
```

Blinko:

```text
Rahul Kumar | ₹5,000 | ₹0 | ₹5,000
```

---

## 2. First repayment

Use:

```text
/returned
```

Select Rahul.

Select:

```text
[Add repayment]
```

Enter:

```text
1000
```

Select the date and confirm.

Database:

```text
Original: ₹5,000
Repaid: ₹1,000
Remaining: ₹4,000
```

Blinko automatically changes to:

```text
Rahul Kumar | ₹5,000 | ₹1,000 | ₹4,000
```

---

## 3. Second repayment

Add:

```text
2000
```

Now:

```text
Original: ₹5,000
Repaid: ₹3,000
Remaining: ₹2,000
```

The loan remains outstanding.

---

## 4. Final repayment

Add:

```text
2000
```

Now:

```text
Original: ₹5,000
Repaid: ₹5,000
Remaining: ₹0
```

The loan is automatically considered returned.

It disappears from Blinko.

It remains in:

```text
/history
```

---

# Storage

The SQLite database is stored outside the Docker container.

Example:

```text
./data/money.db
```

On the homelab server:

```text
/opt/homelab/stacks/money-tracker/data/money.db
```

The database must be included in the existing Restic backup configuration.

The database must **never** be committed to Git.

---

# Backup

The most important application data is:

```text
data/money.db
```

SQLite contains:

* Loans
* Repayments
* Dates
* Return history
* Transaction history
* Blinko note configuration

Restic should back up this directory.

```text
SQLite
   │
   ▼
Restic
   │
   ▼
Backup Storage
```

---

# Recovery

If the Docker container is lost:

1. Restore `money.db`.
2. Restore `.env`.
3. Pull the Docker image.
4. Start the container.
5. Verify the database.
6. Synchronize Blinko.

Because Blinko is only a derived view, losing the Blinko note does not mean losing the financial history.

---

# Environment Variables

Create `.env` on the server.

```dotenv
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

BLINKO_URL=http://blinko-website:1111

BLINKO_API_TOKEN=your_blinko_api_token

ALLOWED_USER_ID=your_telegram_user_id

TZ=Asia/Kolkata
```

Never commit `.env`.

The repository should contain:

```text
.env.example
```

instead.

---

# Docker Deployment

The production server uses the published GHCR image.

Example:

```yaml
services:
  money-tracker:
    image: ghcr.io/YOUR_USERNAME/money-tracker:main

    container_name: money-tracker

    restart: unless-stopped

    env_file:
      - .env

    environment:
      DB_PATH: /data/money.db

    volumes:
      - ./data:/data

    networks:
      - homelab

    logging:
      options:
        max-size: 10m
        max-file: "3"

networks:
  homelab:
    external: true
```

Blinko is accessed through the internal Docker network:

```text
http://blinko-website:1111
```

Traefik and Cloudflare are not required for bot-to-Blinko communication.

---

# CI/CD

Application code is stored in Git.

GitHub Actions builds the Docker image.

```text
git push
    │
    ▼
GitHub Actions
    │
    ├── Checkout
    ├── Build Docker image
    ├── Authenticate with GHCR
    └── Push image
            │
            ▼
           GHCR
```

The production server pulls the new image:

```bash
docker compose pull
docker compose up -d
```

Check status:

```bash
docker compose ps
```

Check logs:

```bash
docker compose logs -f
```

---

# Repository Structure

```text
money-tracker/
│
├── .github/
│   └── workflows/
│       └── docker.yml
│
├── bot/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── bot.py
│
├── data/
│   └── money.db
│
├── compose.yaml
├── .env.example
├── .gitignore
└── README.md
```

`data/money.db` is runtime data and must be excluded from Git.

---

# Security

* Telegram access is restricted using `ALLOWED_USER_ID`.
* Telegram credentials are stored in `.env`.
* Blinko API credentials are stored in `.env`.
* Secrets must never be committed to Git.
* SQLite is stored outside the container.
* The bot does not require a public HTTP endpoint.
* Blinko communication uses the internal Docker network.
* Traefik and Cloudflare are not required for internal bot communication.

---

# Design Principles

## SQLite is the source of truth

All financial history lives in SQLite.

## Blinko is a view

Blinko only shows what is currently outstanding.

## One Blinko note

The application should never create a new Blinko note for every loan.

There should be exactly one managed note.

## Repayments are separate records

A loan and its repayments are different entities.

This makes it possible to accurately track:

```text
₹10,000 loan

₹2,000 repayment
₹3,000 repayment
₹1,000 repayment

Total repaid: ₹6,000
Remaining: ₹4,000
```

## Calculations are derived

The outstanding amount should always be calculated from:

```text
Original amount - Total repayments
```

rather than manually maintaining a balance field.

## Every destructive action requires confirmation

This applies to:

* Deleting loans
* Deleting repayments
* Editing amounts
* Marking remaining balances as returned

---

# Future Improvements

Potential future features:

* Automatic Telegram reminders for outstanding loans
* Scheduled `/summary`
* Monthly financial reports
* CSV export
* Excel export
* Per-person repayment reminders
* Backup verification
* Automatic Blinko recovery/synchronization
* Automatic deployment after a successful GHCR build
* Transaction pagination for very large histories
* Audit log for edits and deletions
