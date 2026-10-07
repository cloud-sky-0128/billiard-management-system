# Billiard Hall Operations Management System

[English](README.md) | [繁體中文](README.zh-TW.md)

[![Tests](https://github.com/cloud-sky-0128/billiard-management-system/actions/workflows/tests.yml/badge.svg)](https://github.com/cloud-sky-0128/billiard-management-system/actions/workflows/tests.yml)

## Project Overview

This is a local-first operations management system for a single billiard hall, built with Python, Flask, SQLite, Jinja2, HTML/CSS, and vanilla JavaScript. It covers timed and package-based table sessions, table transfers and extensions, food and beverage orders, partial or multiple payments, reservations, employee shifts, revenue and expense records, operational statistics, and backups.

The project was motivated by real operational requirements discussed with a friend involved in a planned billiard hall. Many existing systems are difficult to adapt to a specific venue, so I used this project to explore how table operations, billing rules, staff scheduling, reporting, and eventual hardware control could be represented in one maintainable application.

Rather than treating the project as a simple CRUD exercise, I focused on turning real operational rules into a maintainable data model, testable business logic, and a deployable local application.

## Project Status

| Area | Status |
| --- | --- |
| Core operational workflows | Implemented and tested with non-production data |
| Windows browser version | Packaged and released |
| Standalone window version | Preview build available |
| Physical table-light control | Not integrated yet; hardware integration is planned as a future extension |
| Production use in a billiard hall | Not deployed yet |

The application is currently intended for a single-site, local, low-concurrency environment. It has not yet been validated with live billiard hall data or daily production operations.

## Key Features

- Timed and package-based table billing
- Session extension and table transfer
- Food and beverage ordering, including waiting tabs and food-only checkout
- Partial and multiple payment records
- Reservation and calendar management
- Employee and shift scheduling with individual schedule export
- Daily revenue, expense, and operational reporting
- Automatic database backup, validation, and recovery support

## Screenshots

The screenshots below use demonstration data only.

### Table Dashboard

![Table dashboard with demonstration data](docs/screenshots/dashboard.png)

### Session and Billing Details

![Session and billing details with demonstration data](docs/screenshots/table-detail.png)

### Reservation Calendar

![Reservation calendar with demonstration data](docs/screenshots/calendar.png)

### Revenue Statistics

![Revenue statistics with demonstration data](docs/screenshots/stats.png)

## Engineering Highlights

| Area | Implementation |
| --- | --- |
| Application structure | Flask application factory, feature-based Blueprints, and service modules for billing, business-day, and scheduling rules |
| Data integrity | SQLite foreign keys, constraints, transactions, indexes, and partial unique indexes |
| Billing accuracy | Monetary values stored as integer cents; `Decimal` is used when converting and calculating user input |
| Historical consistency | Sessions snapshot applied rates; orders snapshot item/category names, unit prices, and subtotals; payments preserve actual payment events |
| Atomic operations | Database transactions, including `BEGIN IMMEDIATE` where appropriate, protect table occupation, reservation creation, and shift scheduling from conflicting concurrent writes |
| Retry protection | Operation identifiers and receipts prevent duplicate processing of selected state-changing requests |
| Security hardening | CSRF validation, input limits, session invalidation after password changes, and audit records for sensitive operations |
| Reliability | Automated SQLite backups, backup verification, restore checks, and packaged-data-path handling |
| Delivery | PyInstaller Windows builds; GitHub Actions run checks and tests, build Windows packages, generate SHA-256 checksums, and publish tag-triggered releases |

Historical snapshots are important because menu items and rates can change. Existing transactions must still reproduce the names, prices, discounts, and payments that were valid when the transaction occurred.

The repository contains more than 100 automated regression and hardening tests. They cover billing boundaries, cross-midnight rules, duplicate submissions, reservation and shift conflicts, concurrent operations, malformed input, migrations, backup validation, and security edge cases.

## Architecture

```text
Browser or desktop window
          |
          v
   Flask application
          |
          v
  Feature Blueprints
          |
          v
     Service layer
          |
          v
        SQLite
```

A typical request travels from a Flask route to shared business logic, then to the database, before a Jinja template or file response is returned. Business rules that need reuse or focused tests are kept outside the route handlers.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the detailed architecture, data flow, schema overview, and design decisions.

## Data Model and Design Decisions

The operational billing lifecycle is centered on four related concepts:

```text
customer_tabs
   |-- sessions
   |-- orders
   `-- payments
```

- `customer_tabs` represents the overall customer visit and bill.
- `sessions` represents actual table usage and the rate applied to that usage.
- `orders` stores food and drink purchases with transaction-time snapshots.
- `payments` records actual payment events, allowing a bill to be paid in more than one step.

Other tables model reservations, employees, shift types, shifts, daily cash records, expenses, settings, duplicate-operation receipts, and audit events. SQLite was selected deliberately for a single-machine deployment: it keeps installation and backup simple while still providing transactions, constraints, foreign keys, and indexes. A multi-machine or multi-site product would require a centralized database and a different authentication and deployment model.

## Tech Stack

### Backend

- Python 3
- Flask
- Waitress for packaged local serving

### Database

- SQLite
- SQL migrations and integrity constraints

### Frontend

- Jinja2 templates
- HTML and CSS
- Vanilla JavaScript

### Testing

- Python `unittest`
- Flask test client
- Temporary SQLite databases

### Packaging and Delivery

- PyInstaller
- pywebview / WebView2 for the standalone-window preview
- GitHub Actions

## Why These Technologies?

### Flask and Jinja2

This is an internal operational tool rather than a public content platform or highly interactive SPA. Server-rendered pages keep the request flow, validation, and deployment model straightforward without adding a separate frontend build system.

### SQLite

SQLite matches the current offline-first, single-site, low-concurrency scope. It reduces setup and operational overhead while retaining the transactions and integrity features needed by the domain. This is a scope-based engineering choice, not a claim that SQLite is appropriate for every deployment size.

### Service Layer

Shared billing, business-day, and scheduling logic is separated from HTTP route handling. This reduces duplicated rules and makes boundary cases easier to test directly.

## Testing

Run the full test suite from the repository root:

```bash
python -m unittest discover -s tests -v
```

The suite currently contains more than 100 automated tests, including:

- Minute-boundary billing and integer-money rounding
- Package extension and table transfer
- Cross-midnight discounts, reservations, and shifts
- Concurrent reservation and table-session conflicts
- Duplicate active-session and request-retry prevention
- Partial payment and payment duplication behavior
- Corrupted or stale backup detection
- Schema migration compatibility and malformed input handling

GitHub Actions runs Python syntax checks and the automated tests on pushes and pull requests to `main`.

## Running Locally

```bash
git clone https://github.com/cloud-sky-0128/billiard-management-system.git
cd billiard-management-system
python -m venv .venv
```

Activate the virtual environment, then run:

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000` on the same computer. Local development does not require an internet connection after the dependencies have been installed.

## Windows Releases

Packaged Windows builds are available from [GitHub Releases](https://github.com/cloud-sky-0128/billiard-management-system/releases). The browser-based package is the recommended build. The standalone-window build is a preview and uses the Microsoft Edge WebView2 Runtime.

Detailed installation, data-location, backup, checksum, and troubleshooting instructions are kept in:

- [Windows portable/browser version](docs/WINDOWS_PORTABLE.md)
- [Standalone-window preview](docs/WINDOWS_WINDOW_PREVIEW.md)

## Current Limitations

- The system has been tested with non-production data and has not been deployed for real billiard hall operations.
- Physical table-light control has not yet been integrated or validated with real hardware.
- The current design targets one local machine and one venue, not multi-store or cloud deployment.
- SQLite is appropriate for the current low-concurrency scope; concurrent multi-machine use is outside the design target.
- Shared administrator access cannot identify the individual employee behind every action.
- The standalone desktop window remains a preview build and requires further clean-computer acceptance testing.

## Documentation

- [Architecture and design decisions](docs/ARCHITECTURE.md)
- [Windows portable/browser guide](docs/WINDOWS_PORTABLE.md)
- [Standalone-window preview guide](docs/WINDOWS_WINDOW_PREVIEW.md)
- [Windows download acceptance report](docs/DOWNLOAD_ACCEPTANCE_2026-10-07.md)
- [Windows release checklist](docs/DOWNLOAD_RELEASE_CHECKLIST.md)
