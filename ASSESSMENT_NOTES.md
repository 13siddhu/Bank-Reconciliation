# Capital Call Reconciliation Portal — Assessment Notes & Architecture

**Digital Alpha Platforms Technical Assessment: Full Stack Developer (Django & React.js)**  
**Application Name:** Capital Call Reconciliation Portal  
**Repository Root:** `~/Desktop/Bank-Reconciliation`  

---

## 1. Project Overview & Implemented Features

The Capital Call Reconciliation Portal is a specialized financial operations platform designed for private equity and venture capital fund operations. It automates matching incoming bank statement transactions against investor capital call line items, calculates funding progress, identifies discrepancies, and provides atomic reconciliation controls with multi-tenant isolation.

### Key Implemented Capabilities:
1. **Multi-Tenant Isolation**: Complete data segregation between funds/firms (`northwind` - Northwind Ventures, `bluebird` - Bluebird Capital) driven by the mandatory `X-Organization` header on all organization-scoped endpoints.
2. **Bank Statement CSV Importer**:
   - Validates CSV headers (`date`, `description`, `amount`, `type`, `reference`) with transparent UTF-8 BOM support.
   - Enforces a 5MB maximum file size limit.
   - Reports physical CSV line numbers for invalid rows (e.g., malformed dates, invalid amounts, missing fields).
   - Skips duplicate transactions within the file and against existing database records without halting the batch.
   - Imports valid transactions even when invalid or duplicate rows are present.
   - Automatically classifies debits as `ignored` (no match proposals generated).
   - Automatically executes matching suggestions for imported credits in real time.
3. **Automated Matching Engine & Precedence**:
   - Reference normalization (uppercase alphanumeric characters only, e.g. `CC-1003` $\rightarrow$ `CC1003`).
   - Investor entity name normalization (collapses punctuation and whitespace, strips trailing legal suffixes `LLC`, `LP`, `LTD`, `INC`, while preserving `TRUST`).
   - Whole-word description matching using regex boundaries (`\b`) to eliminate partial substring false positives.
   - Enforces candidate eligibility: same tenant, positive remaining balance (`amount_called > amount_funded`), and capital call date on or before transaction date (`call_date <= transaction_date`).
   - **Reference Precedence**: Exact reference match takes precedence and bypasses Name-and-Amount evaluation.
   - **Overpayment Safeguard**: If payment amount exceeds remaining line item balance, no proposal is generated; transaction is flagged as `needs_review` with an explanatory diagnostic message.
   - **Ambiguity Detection**: Multiple matching candidates generate proposals for all candidates and flag the transaction as `needs_review`.
   - **Name & Amount Fallback**: Evaluated only when zero reference candidates exist; matches whole-word normalized entity name and exact remaining amount.
4. **Reconciliation Actions (Approve, Reject, Ignore)**:
   - **Approve**: Atomically increments `line_item.amount_funded`, transitions line item to `partially_funded` or `funded`, sets selected match to `approved`, auto-rejects competing proposals, and marks transaction `reconciled`. Uses database row locking (`select_for_update()`) to prevent race conditions and double-funding.
   - **Reject**: Rejects a proposed match. If no proposed matches remain, transitions transaction to `needs_review`. Never alters funding totals.
   - **Ignore**: Marks credit transaction `ignored` and auto-rejects pending proposals. Reconciled transactions cannot be ignored (HTTP 409).
5. **Interactive Operations Dashboard**:
   - Organization switcher with real-time state synchronization.
   - Real-time KPI summary cards displaying database-backed metrics for transactions and capital call funding totals.
   - Interactive CSV file uploader displaying row-level diagnostics and duplicate counts.
   - Filterable transactions table with status badges (`suggested`, `needs_review`, `reconciled`, `ignored`).
   - Ambiguous candidate inspector with dedicated Approve and Reject actions per candidate.
   - Capital call line items panel with visual funding progress indicators.
   - Safety confirmation modals for all state-changing actions.

---

## 2. Fresh-Clone Setup Instructions

Follow these exact commands to set up and run the project from a fresh clone.

### Prerequisites
- **Python**: Version 3.12+
- **Node.js**: Version 18+ (npm version 9+)
- **PostgreSQL**: Local instance or remote managed database (e.g. Supabase)

### Step 1: Clone Repository
```bash
git clone <repository-url>
cd Bank-Reconciliation
```

### Step 2: Backend Setup & Virtual Environment
```bash
cd backend

# Create and activate Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install backend dependencies from requirements.txt
pip install -r requirements.txt
```

### Step 3: Configure Backend Environment
Copy the safe example environment template and configure your PostgreSQL database connection:
```bash
cp .env.example .env
```
Edit `.env` with your PostgreSQL database credentials:
```env
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=your_database_password
DB_HOST=your_database_host
DB_PORT=5432
DB_SSLMODE=require
```

### Step 4: Apply Database Migrations & Seed Demo Data
```bash
# Run Django database migrations
python manage.py migrate

# Seed initial demo organizations, capital calls, and line items
python manage.py seed_demo
```
*(Note: `python manage.py seed_demo` is idempotent and safe to run multiple times; it will not overwrite existing funding progress or transactions).*

### Step 5: Start the Backend Server
```bash
python manage.py runserver 127.0.0.1:8000
```
The Django REST Framework backend will be listening on `http://127.0.0.1:8000/`.

### Step 6: Frontend Setup & Startup (in a New Terminal)
```bash
cd ~/Desktop/Bank-Reconciliation/frontend

# Install frontend dependencies
npm install

# (Optional) Copy frontend environment template if using a custom backend port
cp .env.example .env

# Start Vite development server
npm run dev
```
Open **[http://localhost:5173](http://localhost:5173)** in your browser. All `/api` requests are automatically proxied to the backend at `http://127.0.0.1:8000`.

---

## 3. Automated Test Suite Command

To run the complete test suite across models, normalizations, CSV upload, matching engine, and reconciliation actions:

```bash
cd ~/Desktop/Bank-Reconciliation/backend
source venv/bin/activate
python manage.py test reconciliation --keepdb
```

To run focused tests for read-only endpoints and tenant isolation:
```bash
python manage.py test reconciliation.tests.ReadOnlyEndpointsTests --keepdb
```

---

## 4. Data Models & Database Schema

The database schema is implemented in `backend/reconciliation/models.py`:

```
+--------------------+       1:N       +----------------------+
|    Organization    | <-------------- |    InvestorEntity    |
+--------------------+                 +----------------------+
  |          |                                    |
  | 1:N      | 1:N                                | 1:N
  v          v                                    v
+--------------------+                 +----------------------+
|    CapitalCall     | <-------------- |       LineItem       |
+--------------------+       1:N       +----------------------+
                                                  |
                                                  | 1:N
                                                  v
+--------------------+       1:N       +----------------------+
|  BankTransaction   | <-------------- |        Match         |
+--------------------+                 +----------------------+
```

### Model Summaries:
1. **`Organization`**:
   - `slug` (SlugField, unique): Identifier used in the `X-Organization` header (e.g. `northwind`, `bluebird`).
   - `name` (CharField): Organization display name.
   - `created_at` (DateTimeField).
2. **`InvestorEntity`**:
   - `organization` (ForeignKey $\rightarrow$ `Organization`).
   - `name` (CharField): Legal entity name of the LP / investor.
   - `created_at` (DateTimeField).
   - *Constraint*: `UniqueConstraint(fields=['organization', 'name'])`.
3. **`CapitalCall`**:
   - `organization` (ForeignKey $\rightarrow$ `Organization`).
   - `call_number` (CharField): Call notice identifier (e.g. `#1`, `#2`).
   - `call_date` (DateField): Notice issue date.
   - `due_date` (DateField): Capital contribution due date.
   - *Constraints*: `UniqueConstraint(fields=['organization', 'call_number'])`, `CheckConstraint(due_date >= call_date)`.
4. **`LineItem`**:
   - `organization` (ForeignKey $\rightarrow$ `Organization`).
   - `capital_call` (ForeignKey $\rightarrow$ `CapitalCall`).
   - `investor_entity` (ForeignKey $\rightarrow$ `InvestorEntity`).
   - `expected_wire_reference` (CharField): Reference expected in wire instructions (e.g. `CC-1001`).
   - `amount_called` (DecimalField, 15, 2): Total amount called.
   - `amount_funded` (DecimalField, 15, 2, default 0.00): Cumulative funds received.
   - `status` (CharField): Choices: `open`, `partially_funded`, `funded`.
   - *Property `remaining_amount`*: Calculated as `amount_called - amount_funded`.
   - *Constraints*: `amount_called > 0`, `amount_funded >= 0`, `amount_funded <= amount_called`.
5. **`BankTransaction`**:
   - `organization` (ForeignKey $\rightarrow$ `Organization`).
   - `transaction_date` (DateField).
   - `description` (TextField): Bank wire narrative / description.
   - `signed_amount` (DecimalField, 15, 2): Credit positive, debit negative.
   - `transaction_type` (CharField): `credit` or `debit`.
   - `bank_reference` (CharField): Bank-assigned wire transaction reference.
   - `status` (CharField): Choices: `suggested`, `needs_review`, `reconciled`, `ignored`.
   - `import_metadata` (JSONField): CSV source metadata, line numbers, review reasons.
   - *Constraint*: `UniqueConstraint(fields=['organization', 'bank_reference'])`.
6. **`Match`**:
   - `organization` (ForeignKey $\rightarrow$ `Organization`).
   - `bank_transaction` (ForeignKey $\rightarrow$ `BankTransaction`).
   - `line_item` (ForeignKey $\rightarrow$ `LineItem`).
   - `match_reason` (CharField): Human-readable explanation of why proposal was generated.
   - `status` (CharField): Choices: `proposed`, `approved`, `rejected`.
   - *Constraints*: `UniqueConstraint(fields=['bank_transaction', 'line_item'])`, `UniqueConstraint(fields=['bank_transaction'], condition=Q(status='approved'))`.

---

## 5. API Endpoints Reference

All endpoints are prefixed with `/api/`. Organization-scoped endpoints require the `X-Organization: <slug>` header.

| Method | Endpoint | `X-Organization` | Purpose & Response Codes |
|---|---|---|---|
| `GET` | `/api/organizations/` | Optional | Returns available organizations (`[{"id", "slug", "name"}]`). Returns `200 OK`. |
| `GET` | `/api/dashboard/summary/` | **Required** | Returns aggregated transaction status counts and capital call funding totals (`total_called`, `total_funded`, `total_remaining`). Returns `200 OK`, `400 Bad Request`, `404 Not Found`. |
| `GET` | `/api/transactions/?status=` | **Required** | Returns bank transactions for the tenant with optional status filter (`suggested`, `needs_review`, `reconciled`, `ignored`), including nested match candidates and line item details. Returns `200 OK`. |
| `GET` | `/api/line-items/` | **Required** | Returns capital call line items with investor entity, call details, called amounts, funded amounts, and remaining balances. Returns `200 OK`. |
| `POST` | `/api/transactions/upload/` | **Required** | Uploads bank statement CSV via `multipart/form-data` (`file` key). Returns import counts, duplicate counts, and row-level errors with line numbers. Returns `201 Created`, `200 OK`, `400 Bad Request`. |
| `POST` | `/api/matches/<id>/approve/` | **Required** | Atomically approves proposed match, increments line item funded amount, auto-rejects competing proposals, and marks transaction `reconciled`. Returns `200 OK`, `400 Bad Request`, `404 Not Found`, `409 Conflict`. |
| `POST` | `/api/matches/<id>/reject/` | **Required** | Rejects a proposed match. Updates transaction to `needs_review` if no proposals remain. Returns `200 OK`, `400 Bad Request`, `404 Not Found`, `409 Conflict`. |
| `POST` | `/api/transactions/<id>/ignore/` | **Required** | Marks a credit transaction as `ignored` and auto-rejects pending proposals. Returns `200 OK`, `400 Bad Request`, `404 Not Found`, `409 Conflict`. |

---

## 6. CSV Validation, Matching Precedence & Safety Safeguards

### CSV Validation & Duplicate Handling
- Missing required headers (`date`, `description`, `amount`, `type`, `reference`) reject the whole file with HTTP 400.
- Data rows with format errors (e.g., non-existent dates, non-numeric amounts, negative credits) are skipped and reported with their physical 1-indexed CSV line numbers.
- Rows with references already existing in the database or duplicated within the file are counted under `duplicates_count` and skipped.
- Valid rows are imported in a single atomic transaction.
- Debits are marked `ignored` on import. Credits are evaluated through matching suggestions immediately.

### Matching Engine Precedence
1. **Reference Check (Priority 1)**:
   - Matches normalized expected wire reference (`CC-1001` $\rightarrow$ `CC1001`) in normalized description or bank reference.
   - **Exact Single Match & Amount $\le$ Remaining**: Status `suggested`, creates 1 proposed match (supports partial payments).
   - **Exact Single Match & Amount $>$ Remaining (Overpayment)**: Status `needs_review`, creates **zero** proposed matches, logs diagnostic review reason.
   - **Multiple Reference Matches**: Status `needs_review`, creates proposed matches for **all** candidate line items.
2. **Name & Amount Check (Priority 2 - Fallback)**:
   - Evaluated **only** when 0 reference candidates are found.
   - Matches whole-word normalized investor entity name in description and requires transaction amount $==$ remaining line item balance.
   - **Single Match**: Status `suggested`, creates 1 proposed match.
   - **Multiple Matches**: Status `needs_review`, creates proposed matches for all candidate line items.
   - **Zero Matches**: Status `needs_review`, creates zero proposed matches.

### Double-Funding & Concurrency Safeguards
- In `ApproveMatchView`, `select_for_update()` locks the `Match`, `BankTransaction`, and `LineItem` rows.
- Re-verifies `amount_to_fund <= line_item.remaining_amount` inside the lock.
- Prevents double funding if the transaction is already reconciled or the match was previously processed (returns HTTP 409 Conflict).
- A database-level partial unique constraint (`unique_approved_match_per_transaction`) strictly guarantees at most one approved match per transaction.

---

## 7. Assumptions, Trade-Offs, Limitations & Next Steps

1. **Authentication Scope**:
   - *Assumption*: In accordance with technical assessment specifications, authentication is scoped to the mandatory `X-Organization` header for tenant isolation. User-level session auth/JWT was intentionally omitted to stay strictly within assessment requirements.
2. **Dataset & Pagination**:
   - *Trade-Off*: Capital call transactions typically occur in controlled batches (dozens to hundreds of transactions per bank statement). The transaction review table renders the tenant's current statement batch in full.
   - *Next Step*: For historical audit archives with tens of thousands of past records, server-side cursor pagination should be added.
3. **Manual Match Association**:
   - *Current State*: Operators can approve suggested matches, pick between ambiguous candidates, or ignore transactions.
   - *Next Step*: Implement a manual line item search modal allowing operators to explicitly pair unmatched `needs_review` transactions with any open line item.

---

## 8. Verification Instructions for an Assessor

1. **Run Django System Check**:
   ```bash
   ./backend/venv/bin/python backend/manage.py check
   ```
   *(Expected output: `System check identified no issues (0 silenced).`)*

2. **Run Frontend Linter & Build**:
   ```bash
   cd frontend && npm run lint && npm run build
   ```
   *(Expected output: ESLint passes with 0 errors/warnings; Vite builds `dist/` bundle).*

3. **Run Focused Backend Tests**:
   ```bash
   ./backend/venv/bin/python backend/manage.py test reconciliation.tests.ReadOnlyEndpointsTests reconciliation.tests.NormalizationTests --keepdb
   ```
   *(Expected output: `Ran 7 tests in ... OK`).*

4. **Verify Git Formatting**:
   ```bash
   git diff --check
   ```
   *(Expected output: Exit code 0, no trailing whitespace or blank lines).*

---

## 9. 5-Minute Video Recording Checklist

The current database state contains the seeded demo data and transactions from [sample_bank_statement.csv](file:///home/siddhartha/Desktop/Bank-Reconciliation/sample_bank_statement.csv). Follow this sequence without needing to reset the database:

- [ ] **0:00 - 0:45 | Introduction & Multi-Tenancy**
  - Show portal at `http://localhost:5173`.
  - In top navigation, switch to **Bluebird Capital (`bluebird`)**. Point out isolated $40,000 call and 0 transactions.
  - Switch back to **Northwind Ventures (`northwind`)**.
- [ ] **0:45 - 1:30 | Seed Command & CSV Upload**
  - Show the terminal command `python manage.py seed_demo` and explain that it runs idempotently without resetting funded balances.
  - In the UI, upload `sample_bank_statement.csv`.
  - Point out the import summary: 1 duplicate skipped (line 11) and 2 row errors (line 13 invalid date, line 14 negative credit).
- [ ] **1:30 - 2:30 | Transaction Review & Matching Engine Rules**
  - Filter by **Suggested**: Show **Haller Capital LLC ($27,500.00, CC-1001)** suggested match. Explain reference normalization (`CC-1001` $\rightarrow$ `CC1001`).
  - Filter by **Needs Review**:
    - Show the **Ambiguous Match** where multiple candidates are proposed. Explain why both candidates are shown.
    - Show an **Overpayment** item and explain that overpayments create no proposal to safeguard fund accounting.
- [ ] **2:30 - 3:45 | Approve, Reject & Ignore Actions**
  - Click **Approve** on Haller Capital LLC ($27,500.00). Show confirmation modal and confirm.
  - Show that transaction updates to **Reconciled** and the line item status becomes **Funded**.
  - On an ambiguous item, click **Reject** on one candidate and show rejection behavior.
  - On an unmatched credit, click **Ignore** and show transition to **Ignored**.
- [ ] **3:45 - 4:15 | Capital Call Line Items Panel**
  - Switch to the **Capital Call Line Items** tab.
  - Highlight updated funded amounts, remaining balances, and visual progress bars.
- [ ] **4:15 - 5:00 | Architecture Tour & Code Review**
  - Open code in editor:
    - [services.py](file:///home/siddhartha/Desktop/Bank-Reconciliation/backend/reconciliation/services.py): Show normalization functions, reference precedence, and overpayment checks.
    - [views.py](file:///home/siddhartha/Desktop/Bank-Reconciliation/backend/reconciliation/views.py): Show atomic `select_for_update()` locking in `ApproveMatchView` and row-level CSV parser.
    - [api.js](file:///home/siddhartha/Desktop/Bank-Reconciliation/frontend/src/api.js): Show automatic `X-Organization` header injection.

---

## 10. Academic & Candidate Integrity Notice

> [!IMPORTANT]
> **Assessment Notice for the Candidate**:  
> The official assessment instructions state: *"Please be aware that we use AI-detector tools to verify your responses; therefore, their use is prohibited."*  
> If an explicit, unaltered statement declaring that "this project was completed without AI assistance" is required by the submission form, please be advised that generative AI pair-programming tools were utilized during code generation, debugging, and audit phases. An honest representation of the development workflow should be provided in accordance with your personal and professional integrity standards.
