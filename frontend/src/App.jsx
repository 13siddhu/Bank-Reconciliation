import { useState, useEffect, useCallback } from 'react';
import { api } from './api';
import './App.css';

const formatMoney = (val) => {
  if (val === null || val === undefined) return '$0.00';
  const num = typeof val === 'string' ? parseFloat(val) : val;
  if (isNaN(num)) return '$0.00';
  const sign = num < 0 ? '-' : '';
  const abs = Math.abs(num);
  return `${sign}$${abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function App() {
  const [organizations, setOrganizations] = useState([]);
  const [selectedOrg, setSelectedOrg] = useState('');
  const [loadingOrgs, setLoadingOrgs] = useState(true);

  const [summary, setSummary] = useState(null);
  const [transactions, setTransactions] = useState([]);
  const [lineItems, setLineItems] = useState([]);
  const [loadingData, setLoadingData] = useState(false);
  const [dataError, setDataError] = useState(null);

  // Filters & Tabs
  const [activeTab, setActiveTab] = useState('transactions'); // 'transactions' | 'lineItems'
  const [statusFilter, setStatusFilter] = useState(''); // '' for all, or 'suggested', 'needs_review', 'reconciled', 'ignored'

  // Upload state
  const [selectedFile, setSelectedFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState(null);
  const [uploadError, setUploadError] = useState(null);

  // Action state
  const [actionInProgress, setActionInProgress] = useState({});
  const [actionError, setActionError] = useState(null);
  const [confirmDialog, setConfirmDialog] = useState(null); // { title, message, onConfirm }

  // 1. Load organizations on mount
  useEffect(() => {
    let active = true;
    async function loadOrgs() {
      try {
        const orgs = await api.getOrganizations();
        if (active) {
          setOrganizations(orgs);
          if (orgs.length > 0) {
            setSelectedOrg(orgs[0].slug);
          }
          setLoadingOrgs(false);
        }
      } catch (err) {
        if (active) {
          setDataError(err.message || 'Failed to load organizations.');
          setLoadingOrgs(false);
        }
      }
    }
    loadOrgs();
    return () => {
      active = false;
    };
  }, []);

  // 2. Fetch organization data
  const refreshData = useCallback(async () => {
    if (!selectedOrg) return;
    try {
      setLoadingData(true);
      setDataError(null);
      const [sumRes, txsRes, liRes] = await Promise.all([
        api.getSummary(selectedOrg),
        api.getTransactions(selectedOrg, statusFilter),
        api.getLineItems(selectedOrg),
      ]);
      setSummary(sumRes);
      setTransactions(txsRes);
      setLineItems(liRes);
    } catch (err) {
      setDataError(err.message || 'Failed to load organization data.');
    } finally {
      setLoadingData(false);
    }
  }, [selectedOrg, statusFilter]);

  useEffect(() => {
    let active = true;
    if (!selectedOrg) return;

    api.getSummary(selectedOrg)
      .then((sumRes) => {
        if (active) setSummary(sumRes);
      })
      .catch((err) => {
        if (active) setDataError(err.message || 'Failed to load summary.');
      });

    api.getTransactions(selectedOrg, statusFilter)
      .then((txsRes) => {
        if (active) setTransactions(txsRes);
      })
      .catch((err) => {
        if (active) setDataError(err.message || 'Failed to load transactions.');
      });

    api.getLineItems(selectedOrg)
      .then((liRes) => {
        if (active) setLineItems(liRes);
      })
      .catch((err) => {
        if (active) setDataError(err.message || 'Failed to load line items.');
      });

    return () => {
      active = false;
    };
  }, [selectedOrg, statusFilter]);

  // Handle Organization Switch
  const handleOrgChange = (newOrg) => {
    setSelectedOrg(newOrg);
    setUploadResult(null);
    setUploadError(null);
    setActionError(null);
  };

  // Handle CSV Upload
  const handleUploadSubmit = async (e) => {
    e.preventDefault();
    if (!selectedFile) {
      setUploadError('Please select a CSV file first.');
      return;
    }

    if (!selectedFile.name.toLowerCase().endsWith('.csv')) {
      setUploadError('Invalid file type. Only .csv files are supported.');
      return;
    }

    if (selectedFile.size > 5 * 1024 * 1024) {
      setUploadError('File size exceeds the 5MB limit.');
      return;
    }

    try {
      setUploading(true);
      setUploadError(null);
      setUploadResult(null);
      const res = await api.uploadStatement(selectedOrg, selectedFile);
      setUploadResult(res);
      setSelectedFile(null);
      const fileInput = document.getElementById('csv-file-input');
      if (fileInput) fileInput.value = '';
      await refreshData();
    } catch (err) {
      setUploadError(err.message || 'Upload failed.');
      if (err.data && err.data.errors) {
        setUploadResult(err.data);
      }
    } finally {
      setUploading(false);
    }
  };

  // Actions
  const handleApprove = (matchId, investorName, amount) => {
    setConfirmDialog({
      title: 'Confirm Match Approval',
      message: `Are you sure you want to approve this payment of ${formatMoney(amount)} for ${investorName}? This will atomically fund the capital call line item.`,
      actionBtnClass: 'btn-success',
      actionText: 'Yes, Approve',
      onConfirm: async () => {
        setConfirmDialog(null);
        setActionInProgress((prev) => ({ ...prev, [matchId]: true }));
        setActionError(null);
        try {
          await api.approveMatch(selectedOrg, matchId);
          await refreshData();
        } catch (err) {
          setActionError(err.message || 'Approval failed.');
        } finally {
          setActionInProgress((prev) => ({ ...prev, [matchId]: false }));
        }
      },
    });
  };

  const handleReject = (matchId, investorName) => {
    setConfirmDialog({
      title: 'Confirm Match Rejection',
      message: `Reject proposed match for ${investorName}?`,
      actionBtnClass: 'btn-danger',
      actionText: 'Yes, Reject',
      onConfirm: async () => {
        setConfirmDialog(null);
        setActionInProgress((prev) => ({ ...prev, [matchId]: true }));
        setActionError(null);
        try {
          await api.rejectMatch(selectedOrg, matchId);
          await refreshData();
        } catch (err) {
          setActionError(err.message || 'Rejection failed.');
        } finally {
          setActionInProgress((prev) => ({ ...prev, [matchId]: false }));
        }
      },
    });
  };

  const handleIgnore = (txId, ref) => {
    setConfirmDialog({
      title: 'Ignore Transaction',
      message: `Mark transaction ${ref} as ignored? All outstanding proposed matches will be rejected.`,
      actionBtnClass: 'btn-warning',
      actionText: 'Yes, Ignore',
      onConfirm: async () => {
        setConfirmDialog(null);
        setActionInProgress((prev) => ({ ...prev, [txId]: true }));
        setActionError(null);
        try {
          await api.ignoreTransaction(selectedOrg, txId);
          await refreshData();
        } catch (err) {
          setActionError(err.message || 'Ignore failed.');
        } finally {
          setActionInProgress((prev) => ({ ...prev, [txId]: false }));
        }
      },
    });
  };

  return (
    <div className="portal-container">
      {/* Top Header */}
      <header className="portal-header">
        <div className="brand-group">
          <div className="brand-badge">ALPHA</div>
          <div>
            <h1 className="portal-title">AlphaReconcile</h1>
            <p className="portal-subtitle">Capital Call Bank Statement Reconciliation Portal</p>
          </div>
        </div>

        <div className="header-controls">
          <div className="org-switcher-group">
            <label htmlFor="org-select" className="org-label">Organization:</label>
            {loadingOrgs ? (
              <span className="hint-tag">Loading orgs...</span>
            ) : (
              <select
                id="org-select"
                className="org-select"
                value={selectedOrg}
                onChange={(e) => handleOrgChange(e.target.value)}
              >
                {organizations.map((org) => (
                  <option key={org.slug} value={org.slug}>
                    {org.name} ({org.slug})
                  </option>
                ))}
              </select>
            )}
          </div>

          <button
            className="btn btn-secondary btn-icon"
            onClick={refreshData}
            disabled={loadingData}
            title="Refresh portal data"
          >
            {loadingData ? 'Refreshing...' : '↻ Refresh'}
          </button>
        </div>
      </header>

      {/* Global Alerts */}
      {dataError && (
        <div className="alert alert-danger" role="alert">
          <strong>Error:</strong> {dataError}
          <button className="btn-close" onClick={() => setDataError(null)}>×</button>
        </div>
      )}

      {actionError && (
        <div className="alert alert-danger" role="alert">
          <strong>Action Error:</strong> {actionError}
          <button className="btn-close" onClick={() => setActionError(null)}>×</button>
        </div>
      )}

      {/* Dashboard KPI Summary Cards */}
      <section className="kpi-grid">
        <div className="kpi-card">
          <span className="kpi-label">Total Transactions</span>
          <span className="kpi-value">{summary?.transactions?.total ?? '-'}</span>
          <span className="kpi-subtext">Statement rows imported</span>
        </div>

        <div className="kpi-card kpi-warning">
          <span className="kpi-label">Needs Review</span>
          <span className="kpi-value">{summary?.transactions?.needs_review ?? '-'}</span>
          <span className="kpi-subtext">Ambiguous / Overpayments / Unmatched</span>
        </div>

        <div className="kpi-card kpi-info">
          <span className="kpi-label">Suggested Matches</span>
          <span className="kpi-value">{summary?.transactions?.suggested ?? '-'}</span>
          <span className="kpi-subtext">Ready for GP approval</span>
        </div>

        <div className="kpi-card kpi-success">
          <span className="kpi-label">Reconciled</span>
          <span className="kpi-value">{summary?.transactions?.reconciled ?? '-'}</span>
          <span className="kpi-subtext">Approved and funded</span>
        </div>

        <div className="kpi-card kpi-muted">
          <span className="kpi-label">Ignored</span>
          <span className="kpi-value">{summary?.transactions?.ignored ?? '-'}</span>
          <span className="kpi-subtext">Debits and ignored wires</span>
        </div>

        <div className="kpi-card kpi-highlight">
          <span className="kpi-label">Capital Called vs Funded</span>
          <span className="kpi-value">
            {summary ? formatMoney(summary.line_items.total_funded) : '-'}
          </span>
          <span className="kpi-subtext">
            of {summary ? formatMoney(summary.line_items.total_called) : '-'} called
          </span>
        </div>
      </section>

      {/* CSV Bank Statement Upload Section */}
      <section className="upload-panel card">
        <div className="card-header">
          <h2>Import Bank Statement CSV</h2>
          <span className="hint-tag">Max 5MB • UTF-8 CSV only</span>
        </div>

        <form onSubmit={handleUploadSubmit} className="upload-form">
          <div className="file-input-wrapper">
            <input
              id="csv-file-input"
              type="file"
              accept=".csv"
              onChange={(e) => setSelectedFile(e.target.files[0] || null)}
              disabled={uploading}
              className="file-input"
            />
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            disabled={uploading || !selectedFile}
          >
            {uploading ? 'Processing & Matching...' : 'Upload & Reconcile'}
          </button>
        </form>

        {uploadError && (
          <div className="alert alert-danger" style={{ marginTop: '16px' }}>
            <strong>Upload Failed:</strong> {uploadError}
          </div>
        )}

        {/* Upload Summary & Errors */}
        {uploadResult && (
          <div className="upload-summary-box">
            <div className="summary-badges">
              <span className="badge badge-success">
                Imported: {uploadResult.imported_count ?? 0}
              </span>
              <span className="badge badge-warning">
                Duplicates Skipped: {uploadResult.duplicates_count ?? 0}
              </span>
              <span className="badge badge-danger">
                Errors: {uploadResult.errors?.length ?? 0}
              </span>
            </div>

            {uploadResult.message && (
              <p className="summary-msg">{uploadResult.message}</p>
            )}

            {uploadResult.errors && uploadResult.errors.length > 0 && (
              <div className="errors-table-wrapper">
                <h4>Row-Level Validation Notices:</h4>
                <table className="table errors-table">
                  <thead>
                    <tr>
                      <th style={{ width: '80px' }}>Line</th>
                      <th style={{ width: '120px' }}>Column</th>
                      <th>Issue Description</th>
                    </tr>
                  </thead>
                  <tbody>
                    {uploadResult.errors.map((err, idx) => (
                      <tr key={idx}>
                        <td><strong>#{err.line}</strong></td>
                        <td><code>{err.column}</code></td>
                        <td>{err.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>

      {/* Navigation Tabs */}
      <nav className="tabs-nav">
        <button
          className={`tab-btn ${activeTab === 'transactions' ? 'active' : ''}`}
          onClick={() => setActiveTab('transactions')}
        >
          Bank Transactions ({transactions.length})
        </button>
        <button
          className={`tab-btn ${activeTab === 'lineItems' ? 'active' : ''}`}
          onClick={() => setActiveTab('lineItems')}
        >
          Capital Call Line Items ({lineItems.length})
        </button>
      </nav>

      {/* Tab 1: Bank Transactions Table */}
      {activeTab === 'transactions' && (
        <section className="card">
          <div className="card-header table-controls">
            <div className="filter-group">
              <span className="filter-label">Status Filter:</span>
              <div className="pill-filters">
                <button
                  className={`pill ${statusFilter === '' ? 'active' : ''}`}
                  onClick={() => setStatusFilter('')}
                >
                  All
                </button>
                <button
                  className={`pill ${statusFilter === 'suggested' ? 'active' : ''}`}
                  onClick={() => setStatusFilter('suggested')}
                >
                  Suggested
                </button>
                <button
                  className={`pill ${statusFilter === 'needs_review' ? 'active' : ''}`}
                  onClick={() => setStatusFilter('needs_review')}
                >
                  Needs Review
                </button>
                <button
                  className={`pill ${statusFilter === 'reconciled' ? 'active' : ''}`}
                  onClick={() => setStatusFilter('reconciled')}
                >
                  Reconciled
                </button>
                <button
                  className={`pill ${statusFilter === 'ignored' ? 'active' : ''}`}
                  onClick={() => setStatusFilter('ignored')}
                >
                  Ignored
                </button>
              </div>
            </div>
            <span className="row-counter">{transactions.length} record(s)</span>
          </div>

          {loadingData ? (
            <div className="empty-state">
              <div className="spinner"></div>
              <p>Loading transactions...</p>
            </div>
          ) : transactions.length === 0 ? (
            <div className="empty-state">
              <p>No transactions found for organization <strong>{selectedOrg}</strong>.</p>
              <p className="subtext">Upload a bank statement CSV above to begin reconciliation.</p>
            </div>
          ) : (
            <div className="table-responsive">
              <table className="table portal-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Bank Reference</th>
                    <th>Description</th>
                    <th style={{ textAlign: 'right' }}>Amount</th>
                    <th>Status</th>
                    <th>Suggested Match / Explanation</th>
                    <th style={{ textAlign: 'right' }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {transactions.map((tx) => {
                    const isCredit = tx.transaction_type === 'credit' && parseFloat(tx.signed_amount) > 0;
                    const proposedMatches = tx.matches.filter((m) => m.status === 'proposed');
                    const approvedMatch = tx.matches.find((m) => m.status === 'approved');

                    return (
                      <tr key={tx.id} className={`status-row-${tx.status}`}>
                        <td className="cell-date">{tx.transaction_date}</td>
                        <td><code>{tx.bank_reference}</code></td>
                        <td className="cell-desc">{tx.description}</td>
                        <td className={`cell-amount ${isCredit ? 'amount-positive' : 'amount-negative'}`}>
                          {formatMoney(tx.signed_amount)}
                        </td>
                        <td>
                          <span className={`badge-status badge-${tx.status}`}>
                            {tx.status.replace('_', ' ')}
                          </span>
                        </td>

                        {/* Match details & Reason */}
                        <td className="cell-matches">
                          {approvedMatch && (
                            <div className="match-approved-box">
                              <strong>✓ Reconciled to: {approvedMatch.line_item.investor_name}</strong>
                              <span className="match-sub">
                                Ref: {approvedMatch.line_item.expected_wire_reference} • Call {approvedMatch.line_item.capital_call}
                              </span>
                              <span className="match-reason">{approvedMatch.match_reason}</span>
                            </div>
                          )}

                          {!approvedMatch && proposedMatches.length === 1 && (
                            <div className="match-proposed-box">
                              <strong>Suggested: {proposedMatches[0].line_item.investor_name}</strong>
                              <span className="match-sub">
                                Ref: {proposedMatches[0].line_item.expected_wire_reference} • Remaining: {formatMoney(proposedMatches[0].line_item.remaining_amount)}
                              </span>
                              <span className="match-reason">{proposedMatches[0].match_reason}</span>
                            </div>
                          )}

                          {!approvedMatch && proposedMatches.length > 1 && (
                            <div className="match-ambiguous-box">
                              <span className="badge badge-warning">
                                {proposedMatches.length} Candidate Matches (Ambiguous)
                              </span>
                              <ul className="candidate-list">
                                {proposedMatches.map((cand) => (
                                  <li key={cand.id}>
                                    <span>
                                      <strong>{cand.line_item.investor_name}</strong> ({cand.line_item.expected_wire_reference}) — Remaining: {formatMoney(cand.line_item.remaining_amount)}
                                    </span>
                                    <div className="action-buttons" style={{ display: 'inline-flex', gap: '4px' }}>
                                      <button
                                        className="btn btn-sm btn-success"
                                        disabled={actionInProgress[cand.id]}
                                        onClick={() => handleApprove(cand.id, cand.line_item.investor_name, tx.signed_amount)}
                                      >
                                        {actionInProgress[cand.id] ? '...' : 'Approve'}
                                      </button>
                                      <button
                                        className="btn btn-sm btn-danger"
                                        disabled={actionInProgress[cand.id]}
                                        onClick={() => handleReject(cand.id, cand.line_item.investor_name)}
                                      >
                                        {actionInProgress[cand.id] ? '...' : 'Reject'}
                                      </button>
                                    </div>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {!approvedMatch && proposedMatches.length === 0 && (
                            <div className="no-match-box">
                              <span className="match-reason">
                                {tx.review_reason || (tx.status === 'ignored' ? 'Debits are ignored.' : 'No line item match found.')}
                              </span>
                            </div>
                          )}
                        </td>

                        {/* Actions */}
                        <td className="cell-actions">
                          <div className="action-buttons">
                            {proposedMatches.length === 1 && (
                              <>
                                <button
                                  className="btn btn-sm btn-success"
                                  disabled={actionInProgress[proposedMatches[0].id]}
                                  onClick={() =>
                                    handleApprove(
                                      proposedMatches[0].id,
                                      proposedMatches[0].line_item.investor_name,
                                      tx.signed_amount
                                    )
                                  }
                                >
                                  {actionInProgress[proposedMatches[0].id] ? '...' : 'Approve'}
                                </button>
                                <button
                                  className="btn btn-sm btn-danger"
                                  disabled={actionInProgress[proposedMatches[0].id]}
                                  onClick={() =>
                                    handleReject(
                                      proposedMatches[0].id,
                                      proposedMatches[0].line_item.investor_name
                                    )
                                  }
                                >
                                  {actionInProgress[proposedMatches[0].id] ? '...' : 'Reject'}
                                </button>
                              </>
                            )}

                            {tx.status !== 'reconciled' && tx.status !== 'ignored' && (
                              <button
                                className="btn btn-sm btn-secondary"
                                disabled={actionInProgress[tx.id]}
                                onClick={() => handleIgnore(tx.id, tx.bank_reference)}
                              >
                                {actionInProgress[tx.id] ? '...' : 'Ignore'}
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* Tab 2: Line Items Panel */}
      {activeTab === 'lineItems' && (
        <section className="card">
          <div className="card-header">
            <h2>Capital Call Line Items</h2>
            <span className="hint-tag">Tracked per Limited Partner (LP)</span>
          </div>

          {loadingData ? (
            <div className="empty-state">
              <div className="spinner"></div>
              <p>Loading line items...</p>
            </div>
          ) : lineItems.length === 0 ? (
            <div className="empty-state">
              <p>No capital call line items found for organization <strong>{selectedOrg}</strong>.</p>
            </div>
          ) : (
            <div className="table-responsive">
              <table className="table portal-table">
                <thead>
                  <tr>
                    <th>Call #</th>
                    <th>Investor Legal Entity</th>
                    <th>Expected Wire Ref</th>
                    <th>Call Date</th>
                    <th>Due Date</th>
                    <th style={{ textAlign: 'right' }}>Amount Called</th>
                    <th style={{ textAlign: 'right' }}>Amount Funded</th>
                    <th style={{ textAlign: 'right' }}>Remaining</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {lineItems.map((li) => {
                    const called = parseFloat(li.amount_called) || 0;
                    const funded = parseFloat(li.amount_funded) || 0;
                    const pct = called > 0 ? Math.min(100, (funded / called) * 100) : 0;

                    return (
                      <tr key={li.id}>
                        <td><strong>{li.capital_call.call_number}</strong></td>
                        <td className="cell-investor">{li.investor_name}</td>
                        <td><code>{li.expected_wire_reference}</code></td>
                        <td>{li.capital_call.call_date}</td>
                        <td>{li.capital_call.due_date}</td>
                        <td style={{ textAlign: 'right' }}>{formatMoney(li.amount_called)}</td>
                        <td style={{ textAlign: 'right' }}>{formatMoney(li.amount_funded)}</td>
                        <td style={{ textAlign: 'right', fontWeight: 'bold' }}>{formatMoney(li.remaining_amount)}</td>
                        <td>
                          <div className="funding-progress-group">
                            <span className={`badge-status badge-${li.status}`}>
                              {li.status.replace('_', ' ')}
                            </span>
                            <div className="progress-track" title={`${pct.toFixed(0)}% funded`}>
                              <div className="progress-fill" style={{ width: `${pct}%` }}></div>
                            </div>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* Confirmation Modal */}
      {confirmDialog && (
        <div className="modal-backdrop">
          <div className="modal-card">
            <h3>{confirmDialog.title}</h3>
            <p>{confirmDialog.message}</p>
            <div className="modal-actions">
              <button
                className="btn btn-secondary"
                onClick={() => setConfirmDialog(null)}
              >
                Cancel
              </button>
              <button
                className={`btn ${confirmDialog.actionBtnClass || 'btn-primary'}`}
                onClick={confirmDialog.onConfirm}
              >
                {confirmDialog.actionText || 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
