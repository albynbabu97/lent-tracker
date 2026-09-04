# Money Tracker Bot

A self-hosted Telegram bot for tracking money lent to other people.

The bot provides a simple mobile-first interface for recording loans and marking them as returned, while maintaining a complete transaction history in SQLite and a live view of outstanding loans in Blinko.

## Architecture

```text
Telegram
    │
    ▼
Money Tracker Bot
    │
    ├── SQLite
    │     └── Complete transaction history
    │
    └── Blinko
          └── Single note containing outstanding loans
```

The application is deployed as a Docker container and published as an image to GitHub Container Registry (GHCR).

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
```

## Features

* Add a loan directly from Telegram
* Confirmation before recording a transaction
* Track:

  * Person
  * Amount
  * Purpose
  * Date lent
  * Return status
  * Date returned
* `/returned` command for marking outstanding loans as returned
* Telegram inline buttons for confirmation
* `/summary` command for current outstanding balance
* `/history` command for complete transaction history
* SQLite database for permanent transaction history
* Single Blinko note containing only outstanding loans
* Automatic synchronization of outstanding loans to Blinko
* Persistent Docker storage
* Designed for Restic backups
* Telegram user access restriction

## Example

Send:

```text
Ravi 500 lunch
```

The bot responds:

```text
Record this loan?

Person: Ravi
Amount: ₹500
Purpose: lunch

[Confirm] [Cancel]
```

After confirmation, SQLite stores the transaction and the Blinko ledger is updated.

### Blinko

The single Blinko note contains only currently outstanding loans:

| Person | Amount | Purpose  | Date       |
| ------ | -----: | -------- | ---------- |
| Ravi   |   ₹500 | lunch    | 2026-09-04 |
| Anu    | ₹1,200 | shopping | 2026-09-03 |

When a loan is returned, it is removed from the Blinko note but remains permanently stored in SQLite.

## Telegram Commands

### `/start`

Displays available commands and usage instructions.

### `/returned`

Displays all currently outstanding loans.

Select a loan:

```text
Ravi — ₹500
```

The bot asks for confirmation:

```text
Mark this loan as returned?

[Yes, returned] [Cancel]
```

After confirmation, the transaction is marked as returned in SQLite and removed from the Blinko outstanding-loans view.

### `/summary`

Displays the current outstanding balance and individual loans.

Example:

```text
Outstanding: ₹2,000

Ravi — ₹500
Anu — ₹1,200
John — ₹300
```

### `/history`

Displays all transactions, including returned loans.

## Data Model

SQLite is the source of truth.

```text
loans
├── id
├── person
├── amount
├── purpose
├── lent_at
├── returned
└── returned_at
```

Returned transactions are never deleted.

Example:

```text
ID | Person | Amount | Purpose | Returned
---|--------|--------|---------|---------
1  | Ravi   | 500    | Lunch   | No
2  | Anu    | 1200   | Shopping| Yes
3  | John   | 300    | Fuel    | No
```

## Storage

The SQLite database is stored outside the container:

```text
./data/money.db
```

On the homelab server:

```text
/opt/homelab/stacks/money-tracker/data/money.db
```

The database should be included in the existing Restic backup strategy.

The database must **not** be committed to Git.

## Environment Variables

Create a `.env` file on the server.

```dotenv
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
BLINKO_URL=http://blinko-website:1111
BLINKO_API_TOKEN=your_blinko_api_token
ALLOWED_USER_ID=your_telegram_user_id
TZ=Asia/Kolkata
```

Never commit `.env` to Git.

## Docker Compose

The production server uses the published GHCR image rather than building the application locally.

Example:

```yaml
services:
  money-tracker:
    image: ghcr.io/YOUR_USERNAME/money-tracker:VERSION
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

The container connects directly to the existing `homelab` Docker network.

Blinko is accessed internally using:

```text
http://blinko-website:1111
```

Traefik and Cloudflare are not required for communication between the bot and Blinko.

## Deployment

The Docker image is built automatically by GitHub Actions and published to GHCR.

On the homelab server:

```bash
docker compose pull
docker compose up -d
```

Check the container:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f
```

## Development

The repository contains the application source and Docker build configuration.

Expected structure:

```text
money-tracker/
├── bot/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── bot.py
├── compose.yaml
├── .env.example
├── .gitignore
└── README.md
```

The production server does not need the application source code to run the bot. It only needs the Compose configuration, environment variables, persistent data directory, and the published Docker image.

## Security

* Telegram access is restricted using `ALLOWED_USER_ID`
* Telegram bot token is stored in `.env`
* Blinko API token is stored in `.env`
* Secrets must never be committed to Git
* SQLite data is kept outside the container
* Blinko communication uses the internal Docker network
* The bot does not need a public HTTP endpoint
* Traefik and Cloudflare are not required for the Telegram bot

## Backup

The following data is important:

```text
data/money.db
```

This database contains the complete transaction history.

It should be backed up using the existing Restic infrastructure.

The Blinko note is considered a derived view and can be reconstructed from SQLite.

## Recovery

If the container is lost:

1. Restore `money.db` from backup.
2. Deploy the required Docker image.
3. Restore the `.env` configuration.
4. Start the container.
5. The bot can reconstruct the Blinko outstanding-loans view from SQLite.

The database remains the authoritative record.

## Roadmap

Potential future features:

* `/balance`
* `/history <person>`
* `/person <name>`
* Multiple repayments for a single loan
* Partial repayments
* Natural-language transaction parsing
* Monthly summaries
* Export to CSV
* Automatic backup verification
* Blinko synchronization recovery
* Telegram reminders for outstanding loans
* Versioned Docker releases
* Automated deployment from GHCR
