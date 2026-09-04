# Money Tracker Telegram Bot

A self-hosted Telegram bot for tracking money lent to people and money owed to other people.

Transactions are stored locally in SQLite and synchronized to a single Blinko note containing only outstanding transactions. The SQLite database remains the source of truth.

## Features

- Add transactions directly from Telegram
- Support full names and transaction purposes
- Track:
  - Money lent to someone
  - Money owed to someone
- Positive amount = money you should receive
- Negative amount = money you owe
- Choose **Today** or select a custom date from an inline calendar
- One persistent Blinko note instead of creating multiple notes
- Blinko note uses the `#Finance` hashtag
- Blinko shows only outstanding transactions
- `/returned` to view outstanding transactions and record repayments
- Partial repayments
- Multiple repayments for a single loan/debt
- `/summary` for total money to receive, total money to pay, and net balance
- `/history` for complete transaction history
- `/history <person>` for a person's transaction history
- `/person <name>` for a person's outstanding/history details
- `/search <term>` to search transactions
- `/edit` to edit transactions
- Edit repayment amounts and dates
- Delete transactions
- Delete individual repayments
- SQLite database persists through the Docker volume
- Dashboard endpoint for a small homepage widget
- User restriction through `ALLOWED_USER_ID`
- Docker Compose deployment
- Container image deployment through GHCR

## Transaction format

Send:

    Full Name Amount Purpose

Examples:

    Rahul Kumar 500 dinner

    John Thomas 1200 shopping

    Anu -250 borrowed cash

### Amount rules

Positive:

    Rahul Kumar 500 dinner

Means:

> Rahul Kumar owes you ₹500.

Negative:

    Rahul Kumar -500 borrowed cash

Means:

> You owe Rahul Kumar ₹500.

Amounts can include commas:

    Rahul Kumar 1,500 shopping

## Dates

When adding a transaction, the bot asks when the transaction occurred.

Choose:

- **Today**
- **Custom date**

The custom-date option opens an inline calendar. You can move between months and select the exact date.

The same date-selection system is used for repayments and supported date edits.

## Repayments

A transaction can have multiple repayments.

For example:

- Lent ₹1,000
- Repaid ₹300
- Repaid ₹200
- Remaining ₹500

The database keeps each repayment separately, preserving the complete history.

For money you owe, repayments represent payments you make toward the debt.

A transaction becomes fully settled when its remaining balance reaches zero.

## Commands

### `/start`

Shows the bot usage and available commands.

### `/returned`

Shows outstanding transactions.

Select a transaction to:

- Add a partial repayment
- Record a full repayment
- Continue tracking the remaining balance

### `/summary`

Shows:

- Total amount to receive
- Total amount to pay
- Net balance
- Outstanding transaction count

### `/history`

Shows complete transaction history, including repayments.

You can also use:

    /history Rahul Kumar

to view history for a specific person.

### `/person <name>`

Shows information for a specific person.

Example:

    /person Rahul Kumar

### `/search <term>`

Searches transaction and repayment information.

Example:

    /search shopping

### `/edit`

Select a transaction and edit supported fields.

Repayment records can also be edited individually.

### `/delete`

Permanently deletes a transaction after confirmation.

Individual repayments can also be deleted.

## Blinko integration

The bot maintains **one Blinko note** rather than creating a new note for every transaction.

The note contains only outstanding transactions.

Example:

    # Money Lent & Owed

    | Person | Type | Remaining | Purpose | Date |
    |---|---|---:|---|---|
    | Rahul Kumar | Lent | ₹500 | Dinner | 04-09-2026 |
    | John Thomas | Owed | ₹300 | Borrowed cash | 02-09-2026 |

    #Finance

When a transaction is fully settled or deleted, it disappears from the Blinko note.

The SQLite database retains the historical information.

## Data storage

The SQLite database is stored at:

    /data/money.db

Docker Compose maps this to:

    ./data:/data

Therefore the database survives container recreation and image updates.

**Back up `data/money.db` regularly.**

The database is the source of truth. If Blinko is temporarily unavailable, transactions can still be recorded in SQLite and synchronized later.

## Dashboard

The application includes a small dashboard API intended for a homepage/service dashboard widget.

The dashboard exposes current information such as:

- Total to receive
- Total to pay
- Net balance
- Outstanding transaction count

The dashboard service uses port:

    8092

When running behind your existing Docker/Traefik setup, expose it according to your homelab routing configuration.

## Environment variables

Create a `.env` file:

    TELEGRAM_BOT_TOKEN=your_telegram_bot_token
    BLINKO_URL=https://blinko.example.com
    BLINKO_API_TOKEN=your_blinko_api_token
    ALLOWED_USER_ID=your_telegram_user_id

Optional:

    DB_PATH=/data/money.db

Do not commit `.env` to Git.

## Docker Compose

The production server uses Docker Compose and pulls the pre-built container image from GitHub Container Registry.

Example:

    services:
      money-tracker:
        image: ghcr.io/albynbabu97/lent-tracker:VERSION
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

Replace `VERSION` with the desired image tag.

## Versioned image deployment

Prefer immutable version tags instead of `latest`.

Example:

    image: ghcr.io/albynbabu97/lent-tracker:v1.3.0

After changing the code:

1. Commit the changes.
2. Push the commit to GitHub.
3. GitHub Actions builds and publishes the image.
4. Update the server's Compose file to the new version.
5. Pull the new image.
6. Recreate the container.

Example:

    docker compose pull
    docker compose up -d

Check the deployment:

    docker compose ps

View logs:

    docker compose logs -f money-tracker

## GitHub Actions

The repository builds the Docker image and publishes it to GHCR.

The server does not need the Python source code or Python packages installed locally. It only needs Docker Compose and the persistent data directory.

This keeps the production server cleaner and makes deployments reproducible.

## Backup

The most important file is:

    ./data/money.db

Recommended backup approach:

- Include `money.db` in your existing homelab backup system.
- Keep multiple historical backup versions.
- Test restoration periodically.

Do not rely on Blinko as the only backup. Blinko is a synchronized view of outstanding transactions; SQLite contains the complete transaction and repayment history.

## Security

- Keep the Telegram bot token secret.
- Keep the Blinko API token secret.
- Keep `.env` out of Git.
- Set `ALLOWED_USER_ID` so only your Telegram account can use the bot.
- Do not expose the SQLite database directly.
- Use your existing Traefik/Cloudflare setup for dashboard access if the dashboard is exposed externally.

## Development

Install dependencies:

    pip install -r requirements.txt

Run locally:

    python bot.py

Build the Docker image:

    docker build -t lent-tracker .

Run with Docker Compose:

    docker compose up -d

## Repository structure

    .
    ├── bot.py
    ├── Dockerfile
    ├── requirements.txt
    ├── compose.yaml
    ├── .env
    └── data/
        └── money.db

`.env` and `data/` should not be committed to Git.

## Requirements

- Docker
- Docker Compose
- Telegram bot
- Blinko instance with API access
- SQLite
- GitHub Actions/GHCR for the production image workflow
