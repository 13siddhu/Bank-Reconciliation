# Digital Alpha Platforms — Bank Reconciliation for Capital Calls

A bank reconciliation portal designed for private equity and venture capital operations. The system automates matching incoming bank transactions against investor capital call line items, handles partial funding, flags overpayments and ambiguous matches for operator review, and enables atomic approval, rejection, and ignore actions with strict tenant isolation.

---

## 1. Architecture & Tech Stack

- **Backend**: Python 3.12, Django 6.1, Django REST Framework (DRF)
- **Database**: PostgreSQL (Supabase) with row locking (`select_for_update()`) and atomic transactions
- **Frontend**: React 19, Vite, Vanilla CSS design system
- **Multi-Tenancy**: Organization isolation via mandatory `X-Organization` header (`northwind`, `bluebird`)

---

## 2. Prerequisites & Setup

### A. Backend Setup
1. Navigate to the `backend/` directory:
   ```bash
   cd backend
   ```
2. Activate the virtual environment:
   ```bash
   source venv/bin/activate
   ```
3. Configure environment variables by creating `.env` from `.env.example`:
   ```bash
   cp .env.example .env
   ```
   Provide your PostgreSQL credentials:
   ```env
   DB_NAME=postgres
   DB_USER=postgres
   DB_PASSWORD=your_database_password
   DB_HOST=your_database_host
   DB_PORT=5432
   DB_SSLMODE=require
   ```
4. Run migrations:
   ```bash
   python manage.py migrate
   ```
5. Seed demo organizations and initial capital calls:
   ```bash
   python manage.py seed_demo
   ```
   *(Safe to run repeatedly; uses idempotent ORM queries without resetting existing funded amounts).*

6. Start the backend development server:
   ```bash
   python manage.py runserver 127.0.0.1:8000
   ```

---

### B. Frontend Setup
1. Navigate to the `frontend/` directory:
   ```bash
   cd frontend
   ```
2. Install dependencies:
   ```bash
   npm install
   ```
3. Optional configuration:
   By default, Vite proxies `/api` requests directly to `http://127.0.0.1:8000`. If running on custom ports, create `.env` from `.env.example`:
   ```bash
   cp .env.example .env
   # VITE_API_BASE_URL=http://127.0.0.1:8000
   ```
4. Start the frontend development server:
   ```bash
   npm run dev
   ```
5. Open [http://localhost:5173](http://localhost:5173) in your browser.

---

## 3. API Endpoints

All organization-scoped endpoints require the `X-Organization: <slug>` header. Missing headers return HTTP 400; unknown organizations return HTTP 404.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/organizations/` | List available organizations (`northwind`, `bluebird`). |
| `GET` | `/api/dashboard/summary/` | Aggregate metrics: transaction counts by status and funding totals. |
| `GET` | `/api/transactions/?status=` | List transactions for tenant (optional status filter: `suggested`, `needs_review`, `reconciled`, `ignored`). |
| `GET` | `/api/line-items/` | List capital call line items, called amounts, funded amounts, and remaining balances. |
| `POST` | `/api/transactions/upload/` | Upload bank statement CSV (`multipart/form-data`, file key `file`). |
| `POST` | `/api/matches/<id>/approve/` | Atomically approve a proposed match, fund the line item, and reconcile transaction. |
| `POST` | `/api/matches/<id>/reject/` | Reject a proposed match without altering funding. |
| `POST` | `/api/transactions/<id>/ignore/` | Mark credit transaction as ignored and reject outstanding proposals. |

---

## 4. Reconciliation & Matching Engine Rules

1. **Normalization**:
   - References are normalized to uppercase alphanumeric characters (`CC-1003` -> `CC1003`).
   - Investor names normalize punctuation and whitespace and strip trailing legal suffixes (`LLC`, `LP`, `LTD`, `INC`) while preserving `TRUST`.
2. **Matching Precedence**:
   - **Reference Match (Highest Priority)**:
     - Exact normalized reference match against description or bank reference.
     - Single candidate & amount $\le$ remaining: **Suggested Match** (handles partial funding).
     - Single candidate & amount $>$ remaining: **Overpayment** $\rightarrow$ `needs_review` (no match proposal created).
     - Multiple reference candidates: **Ambiguous** $\rightarrow$ proposes all candidates, leaves transaction in `needs_review`.
   - **Name & Amount Match (Fallback)**:
     - Only evaluated when zero reference candidates are found.
     - Exact remaining amount match + whole-word investor entity name match in description.
     - Single candidate: **Suggested Match**.
     - Multiple candidates: **Ambiguous** $\rightarrow$ proposes all candidates, leaves transaction in `needs_review`.
     - Zero candidates: `needs_review`.
3. **Reconciliation Actions**:
   - **Approve**: Atomically increments `line_item.amount_funded`, updates status to `funded` or `partially_funded`, marks match `approved`, rejects competing proposed matches for that transaction, and marks transaction `reconciled`. Prevents double-funding with row locks and 409 responses.
   - **Reject**: Rejects proposed match. If no proposals remain, updates transaction status to `needs_review`. Never touches line item funding.
   - **Ignore**: Marks credit transaction `ignored` and rejects any pending proposals. Reconciled transactions cannot be ignored. Debits are automatically marked `ignored` on import.

---

## 5. Verification Commands

Run focused checks and tests:

```bash
# 1. Django System Check
./backend/venv/bin/python backend/manage.py check

# 2. Frontend Linter and Production Build
cd frontend && npm run lint && npm run build

# 3. Focused Backend Tests (Read-Only Endpoints & Tenant Isolation)
./backend/venv/bin/python backend/manage.py test reconciliation.tests.ReadOnlyEndpointsTests --keepdb

# 4. Check for clean Git diff
git diff --check
```