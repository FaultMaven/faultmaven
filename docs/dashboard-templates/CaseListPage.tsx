import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';

interface Case {
  id: string;
  title: string;
  description: string;
  status: 'open' | 'in_progress' | 'resolved' | 'closed';
  priority: 'low' | 'medium' | 'high' | 'critical';
  created_at: string;
  updated_at: string;
  tags?: string[];
}

interface CaseListPageProps {
  apiUrl?: string;
}

export function CaseListPage({ apiUrl = import.meta.env.VITE_API_URL || 'http://localhost:8000' }: CaseListPageProps) {
  const [cases, setCases] = useState<Case[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | 'open' | 'in_progress' | 'resolved' | 'closed'>('all');
  const [sortBy, setSortBy] = useState<'created' | 'updated' | 'priority'>('updated');

  useEffect(() => {
    fetchCases();
  }, [apiUrl]);

  const fetchCases = async () => {
    setLoading(true);
    setError(null);

    try {
      const token = localStorage.getItem('auth_token');
      if (!token) {
        setError('Authentication required. Please log in.');
        setLoading(false);
        return;
      }

      const response = await fetch(`${apiUrl}/v1/cases`, {
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        if (response.status === 401) {
          setError('Authentication failed. Please log in again.');
        } else if (response.status === 403) {
          setError('You do not have permission to access cases.');
        } else {
          setError('Failed to fetch cases. Please try again later.');
        }
        return;
      }

      const data = await response.json();
      setCases(data.cases || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch cases');
      console.error('Error fetching cases:', err);
    } finally {
      setLoading(false);
    }
  };

  const filteredCases = cases.filter(c => {
    if (filter === 'all') return true;
    return c.status === filter;
  });

  const sortedCases = [...filteredCases].sort((a, b) => {
    if (sortBy === 'created') {
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    } else if (sortBy === 'updated') {
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
    } else if (sortBy === 'priority') {
      const priorityOrder = { critical: 0, high: 1, medium: 2, low: 3 };
      return priorityOrder[a.priority] - priorityOrder[b.priority];
    }
    return 0;
  });

  const getStatusBadgeClass = (status: Case['status']) => {
    const classes = {
      open: 'bg-blue-100 text-blue-800',
      in_progress: 'bg-yellow-100 text-yellow-800',
      resolved: 'bg-green-100 text-green-800',
      closed: 'bg-gray-100 text-gray-800',
    };
    return classes[status];
  };

  const getPriorityBadgeClass = (priority: Case['priority']) => {
    const classes = {
      critical: 'bg-red-100 text-red-800',
      high: 'bg-orange-100 text-orange-800',
      medium: 'bg-yellow-100 text-yellow-800',
      low: 'bg-gray-100 text-gray-800',
    };
    return classes[priority];
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto"></div>
          <p className="mt-4 text-gray-600">Loading cases...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-gray-900">Cases</h1>
              <p className="mt-1 text-sm text-gray-600">
                View all troubleshooting cases (read-only)
              </p>
            </div>
            <button
              onClick={fetchCases}
              aria-label="Refresh case list"
              className="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500"
            >
              <svg className="h-5 w-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              Refresh
            </button>
          </div>
        </div>
      </header>

      {/* Filters */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="bg-white rounded-lg shadow p-4">
          <div className="flex flex-wrap gap-4">
            {/* Status Filter */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Status
              </label>
              <select
                value={filter}
                onChange={(e) => {
                  const value = e.target.value as 'all' | 'open' | 'in_progress' | 'resolved' | 'closed';
                  setFilter(value);
                }}
                className="block w-full pl-3 pr-10 py-2 text-base border-gray-300 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm rounded-md"
              >
                <option value="all">All</option>
                <option value="open">Open</option>
                <option value="in_progress">In Progress</option>
                <option value="resolved">Resolved</option>
                <option value="closed">Closed</option>
              </select>
            </div>

            {/* Sort By */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Sort By
              </label>
              <select
                value={sortBy}
                onChange={(e) => {
                  const value = e.target.value as 'created' | 'updated' | 'priority';
                  setSortBy(value);
                }}
                className="block w-full pl-3 pr-10 py-2 text-base border-gray-300 focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm rounded-md"
              >
                <option value="updated">Last Updated</option>
                <option value="created">Date Created</option>
                <option value="priority">Priority</option>
              </select>
            </div>

            {/* Stats */}
            <div className="ml-auto flex items-end">
              <p className="text-sm text-gray-600">
                Showing <span className="font-medium">{sortedCases.length}</span> of{' '}
                <span className="font-medium">{cases.length}</span> cases
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Cases List */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pb-12">
        {error && (
          <div className="rounded-md bg-red-50 p-4 mb-6">
            <div className="flex">
              <div className="flex-shrink-0">
                <svg className="h-5 w-5 text-red-400" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                </svg>
              </div>
              <div className="ml-3">
                <h3 className="text-sm font-medium text-red-800">Error loading cases</h3>
                <p className="mt-1 text-sm text-red-700">{error}</p>
              </div>
            </div>
          </div>
        )}

        {sortedCases.length === 0 ? (
          <div className="text-center py-12">
            <svg className="mx-auto h-12 w-12 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <h3 className="mt-2 text-sm font-medium text-gray-900">No cases found</h3>
            <p className="mt-1 text-sm text-gray-500">
              {filter === 'all'
                ? 'Get started by creating a case in the browser extension.'
                : `No ${filter.replace('_', ' ')} cases found.`
              }
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {sortedCases.map((caseItem) => (
              <div key={caseItem.id} className="bg-white rounded-lg shadow hover:shadow-md transition-shadow">
                <div className="p-6">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3 mb-2">
                        <h3 className="text-lg font-medium text-gray-900">
                          {caseItem.title}
                        </h3>
                        <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${getStatusBadgeClass(caseItem.status)}`}>
                          {caseItem.status.replace('_', ' ')}
                        </span>
                        <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${getPriorityBadgeClass(caseItem.priority)}`}>
                          {caseItem.priority}
                        </span>
                      </div>

                      <p className="text-sm text-gray-600 mb-3 line-clamp-2">
                        {caseItem.description}
                      </p>

                      {caseItem.tags && caseItem.tags.length > 0 && (
                        <div className="flex flex-wrap gap-2 mb-3">
                          {caseItem.tags.map((tag, idx) => (
                            <span key={idx} className="inline-flex items-center px-2 py-1 rounded-md text-xs font-medium bg-gray-100 text-gray-800">
                              #{tag}
                            </span>
                          ))}
                        </div>
                      )}

                      <div className="flex items-center gap-4 text-sm text-gray-500">
                        <span className="flex items-center">
                          <svg className="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          Updated {formatDate(caseItem.updated_at)}
                        </span>
                        <span className="flex items-center">
                          <svg className="h-4 w-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                          </svg>
                          Created {formatDate(caseItem.created_at)}
                        </span>
                      </div>
                    </div>

                    <div className="ml-4">
                      <a
                        href={`${apiUrl}/v1/cases/${caseItem.id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center px-3 py-2 border border-gray-300 shadow-sm text-sm leading-4 font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500"
                      >
                        View Details
                        <svg className="ml-2 h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                        </svg>
                      </a>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Note about editing */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pb-12">
        <div className="rounded-md bg-blue-50 p-4">
          <div className="flex">
            <div className="flex-shrink-0">
              <svg className="h-5 w-5 text-blue-400" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
              </svg>
            </div>
            <div className="ml-3 flex-1">
              <p className="text-sm text-blue-700">
                <strong>Note:</strong> This is a read-only view. To create or edit cases, use the FaultMaven browser extension.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default CaseListPage;
