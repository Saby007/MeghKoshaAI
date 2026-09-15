import { toNarratePayload } from './findings/models';
import { apiFetch } from './apiIdentity';
import { BRAND_NAME } from './brand';
import type { CostFilter, CostWindow } from './report/costDetails';
import type {
  AnomalySummary,
  FullReport,
  Budget,
  BudgetWriteRequest,
  FinOpsActionState,
  ReportSnapshot,
  ReportSnapshotSummary,
  RateOptimizationResponse,
  RecommendationLookBack,
  RecommendationTerm,
  ReservationResourceType,
} from './report/models';

export type PrioritizedFinding = { category: string; priority: string; narrative: string };
export type CostAgentOutput = { executive_summary: string; prioritized_findings: PrioritizedFinding[] } | null;
export type ScheduleState = 'active' | 'paused' | 'not_scheduled' | 'unknown';
export type ScheduleRun = {
  runId: string;
  subscriptionId: string;
  period: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'unknown';
  startedAt: string | null;
  completedAt: string | null;
  durationSeconds: number | null;
  error: string | null;
  completedMonths?: number;
  windowMonths?: 6;
};
export type CostSchedule = {
  subscriptionId: string;
  displayName: string;
  readAccess?: boolean;
  costAccess?: boolean;
  accessCheckMode?: 'permissions' | 'live';
  windowMonths?: 6;
  state: ScheduleState;
  recurrence: string;
  scheduleStartAt: string | null;
  nextRunAt: string | null;
  latestRun: ScheduleRun | null;
  availability?: 'available' | 'access_unavailable' | 'configuration_unavailable' | 'export_unavailable' | 'history_unavailable';
  statusMessage?: string | null;
};
export type FocusExportConfiguration = {
  subscriptionId: string;
  exportName: string;
  storageResourceId: string;
  container: string;
  rootFolderPath: string;
  format: 'Csv';
  dataVersion: string;
  windowMonths: 6;
  nativeSchedule: 'Inactive';
  destinationRole: 'Storage Blob Data Contributor';
  destinationRoleScope: string;
  state: 'missing' | 'configured';
  canConfigure: boolean;
};
export type ExchangeRates = {
  baseCurrency: string;
  provider: string;
  providerUrl: string;
  publishedDate: string;
  fetchedAt: string;
  stale: boolean;
  rates: Record<string, number>;
  disclaimer: string;
};
export type FocusExportFile = {
  blobName: string;
  fileName: string;
  size: number;
  lastModified: string;
  contentType: string;
};
export type FocusExportFileList = {
  subscriptionId: string;
  files: FocusExportFile[];
};
export type ChatMetric = { label: string; value: string; detail: string };
export type ChatResource = {
  resourceName: string;
  resourceId: string;
  subscriptionName: string;
  monthlyCost: number | null;
  currency: string;
  detail: string;
};
export type ChatAnswer = {
  intent: 'overview' | 'subscription_change' | 'unattached_disks' | 'score' | 'trend' | 'forecast' | 'help';
  answer: string;
  metrics: ChatMetric[];
  resources: ChatResource[];
  suggestions: string[];
  dataAsOf: string;
  disclaimer: string;
  responseMode: 'model_router' | 'deterministic_fallback';
  selectedModel: string | null;
  usage: { inputTokens: number; outputTokens: number; totalTokens: number } | null;
  evidenceKeys: string[];
};
export type ChatTurn = { role: 'user' | 'assistant'; content: string };

export class ApiRequestError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = 'ApiRequestError';
  }
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json', ...init.headers } : init?.headers,
  });
  if (res.redirected) throw new ApiRequestError('Your session could not be verified. Sign in again.', 401);
  if (!res.ok) {
    const payload = await res.json().catch(() => null);
    const detail = payload?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    const code = typeof detail?.code === 'string' ? detail.code : undefined;
    throw new ApiRequestError(message || `Request failed: ${res.status}`, res.status, code);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export async function getLatestReport(signal?: AbortSignal, subscriptionIds?: string[], staleDays = 90): Promise<ReportSnapshot | null> {
  const parameters = new URLSearchParams();
  if (subscriptionIds) {
    subscriptionIds.forEach((subscriptionId) => parameters.append('subscriptionId', subscriptionId));
    parameters.set('staleDays', String(staleDays));
  }
  const response = await apiFetch(`/api/report/latest${parameters.size ? `?${parameters.toString()}` : ''}`, {
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
  });
  if (response.status === 404) return null;
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiRequestError(typeof payload?.detail === 'string' ? payload.detail : payload?.detail?.message || `Latest report failed: ${response.status}`, response.status);
  }
  return response.json();
}

export function askFinOpsChat(
  question: string,
  snapshotId: string,
  history: ChatTurn[],
  signal?: AbortSignal,
): Promise<ChatAnswer> {
  return apiRequest('/api/chat', {
    method: 'POST',
    signal,
    body: JSON.stringify({ question, snapshotId, history: history.slice(-6) }),
  });
}

export type ReportExportType = 'executive' | 'full' | 'chargeback' | 'compliance' | 'finops';

export type ReportEmailResult = {
  attemptId: string;
  operationId: string;
  recipient: string;
  status: string;
};

export function emailReportArtifact(snapshotId: string, reportType: ReportExportType): Promise<ReportEmailResult> {
  return apiRequest('/api/report/email', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snapshotId, type: reportType }),
  });
}

export async function downloadReportArtifact(
  snapshotId: string,
  reportType: ReportExportType,
  subscriptionId?: string,
): Promise<{ elapsedMs: number; bytes: number }> {
  const startedAt = performance.now();
  const parameters = new URLSearchParams({ snapshotId, type: reportType });
  if (subscriptionId) parameters.set('subscriptionId', subscriptionId);
  const response = await apiFetch(`/api/report/export?${parameters.toString()}`, {
    credentials: 'same-origin',
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `Report export failed: ${response.status}`);
  }
  const content = await response.blob();
  const disposition = response.headers.get('content-disposition') || '';
  const fileName = disposition.match(/filename="([^"]+)"/)?.[1]
    ?? `${BRAND_NAME}-${reportType === 'executive' ? 'Executive' : reportType === 'full' ? 'Full-Assessment' : reportType === 'chargeback' ? 'Chargeback' : reportType === 'compliance' ? 'Compliance' : 'FinOps-Monthly'}.${reportType === 'executive' ? 'pdf' : 'xlsx'}`;
  const url = URL.createObjectURL(content);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
  return { elapsedMs: performance.now() - startedAt, bytes: content.size };
}

export function listReportSnapshots(
  subscriptionIds: string[],
  signal?: AbortSignal,
): Promise<ReportSnapshotSummary[]> {
  const parameters = new URLSearchParams({ limit: '50' });
  subscriptionIds.forEach((subscriptionId) => parameters.append('subscriptionId', subscriptionId));
  return apiRequest(`/api/report/snapshots?${parameters.toString()}`, { signal });
}

export async function downloadCostDetailReport(snapshotId: string, window: CostWindow, filters: CostFilter, previous?: CostWindow, selectedDates?: string[]): Promise<void> {
  const response = await apiFetch('/api/report/cost-details/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snapshotId, ...window, filters, previousStart: previous?.startDate, previousEnd: previous?.endDate, selectedDates }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(typeof payload?.detail === 'string' ? payload.detail : `Cost detail export failed: ${response.status}`);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${BRAND_NAME}-Cost-Detail-${window.startDate}-${window.endDate}.xlsx`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export type ResourceAvailability = {
  resourceId: string; date: string; status: 'complete' | 'partial' | 'unavailable' | 'unsupported'; statusMessage: string;
  availableHours: number | null; observedAvailableHours: number | null; coverageMinutes: number; expectedMinutes: number; metric: string; observedAt: string;
};

export function getResourceAvailability(snapshotId: string, resourceId: string, date: string, signal?: AbortSignal): Promise<ResourceAvailability> {
  const parameters = new URLSearchParams({ snapshotId, resourceId, date });
  return apiRequest(`/api/report/resource-availability?${parameters}`, { signal });
}

export type ServiceRetirementSummary = {
  snapshotId: string; observedAt: string; costPeriod: string; currency: string;
  notices: { recommendationId: string; subscriptionId: string; resourceId: string; resourceName: string; resourceType: string; feature: string; retirementDate: string | null; guidance: string; matchedCost: number | null }[];
  sources: { subscriptionId: string; available: boolean; message: string }[];
};

export function getServiceRetirements(snapshotId: string, subscriptionId?: string, signal?: AbortSignal): Promise<ServiceRetirementSummary> {
  const parameters = new URLSearchParams({ snapshotId });
  if (subscriptionId) parameters.set('subscriptionId', subscriptionId);
  return apiRequest(`/api/report/retirements?${parameters}`, { signal });
}

export async function downloadServiceRetirements(snapshotId: string, subscriptionId?: string): Promise<void> {
  const parameters = new URLSearchParams({ snapshotId, export: 'true' });
  if (subscriptionId) parameters.set('subscriptionId', subscriptionId);
  const response = await apiFetch(`/api/report/retirements?${parameters}`);
  if (!response.ok) throw new Error(`Retirement export failed: ${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = `${BRAND_NAME}-Service-Retirements.xlsx`; anchor.click();
  URL.revokeObjectURL(url);
}

export async function downloadCustomReport(
  snapshotIds: string[],
  modules: string[],
): Promise<{ elapsedMs: number; bytes: number }> {
  const startedAt = performance.now();
  const response = await apiFetch('/api/report/export/custom', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snapshotIds, modules }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `Custom report failed: ${response.status}`);
  }
  const content = await response.blob();
  const disposition = response.headers.get('content-disposition') || '';
  const fileName = disposition.match(/filename="([^"]+)"/)?.[1] ?? `${BRAND_NAME}-Custom.xlsx`;
  const url = URL.createObjectURL(content);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
  return { elapsedMs: performance.now() - startedAt, bytes: content.size };
}

export function getFinOpsActions(snapshotId: string, signal?: AbortSignal): Promise<FinOpsActionState[]> {
  return apiRequest(`/api/report/actions?snapshotId=${encodeURIComponent(snapshotId)}`, { signal });
}

export function updateFinOpsAction(
  snapshotId: string,
  actionId: string,
  state: Pick<FinOpsActionState, 'status' | 'owner' | 'dueDate' | 'realizedSavingMonth' | 'note'> & { expectedVersion: number },
): Promise<FinOpsActionState> {
  return apiRequest(`/api/report/actions/${encodeURIComponent(actionId)}`, {
    method: 'PUT',
    body: JSON.stringify({ snapshotId, ...state }),
  });
}

export function listBudgets(subscriptionIds: string[], signal?: AbortSignal): Promise<Budget[]> {
  const parameters = new URLSearchParams();
  subscriptionIds.forEach((subscriptionId) => parameters.append('subscriptionId', subscriptionId));
  return apiRequest(`/api/budgets?${parameters.toString()}`, { signal });
}

export function createBudget(request: BudgetWriteRequest): Promise<Budget> {
  return apiRequest('/api/budgets', { method: 'POST', body: JSON.stringify(request) });
}

export function updateBudget(subscriptionId: string, name: string, request: BudgetWriteRequest): Promise<Budget> {
  return apiRequest(`/api/budgets/${encodeURIComponent(subscriptionId)}/${encodeURIComponent(name)}`, {
    method: 'PUT',
    body: JSON.stringify(request),
  });
}

export function deleteBudget(subscriptionId: string, name: string): Promise<void> {
  return apiRequest(`/api/budgets/${encodeURIComponent(subscriptionId)}/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
}

export async function listCostSchedules(signal?: AbortSignal): Promise<CostSchedule[]> {
  const schedules = await apiRequest<CostSchedule[]>('/api/schedules', { signal, cache: 'no-store' });
  if (!Array.isArray(schedules) || schedules.some((item) => !item || typeof item.subscriptionId !== 'string'
    || typeof item.displayName !== 'string' || typeof item.readAccess !== 'boolean' || typeof item.costAccess !== 'boolean'
    || item.windowMonths !== 6 || ((item.readAccess !== true || item.costAccess !== true)
      && (item.state !== 'unknown' || item.availability !== 'access_unavailable'
        || typeof item.statusMessage !== 'string' || !item.statusMessage.trim()
        || item.latestRun !== null || item.nextRunAt !== null || item.scheduleStartAt !== null)))) {
    throw new ApiRequestError('Schedule access or readiness information is missing from the subscription response.', 502);
  }
  return schedules;
}

function validateExportConfiguration(value: FocusExportConfiguration, subscriptionId: string): void {
  const fields = ['exportName', 'storageResourceId', 'container', 'rootFolderPath', 'dataVersion', 'destinationRoleScope'] as const;
  if (!value || value.subscriptionId !== subscriptionId || fields.some((field) => {
    const content = value[field];
    return typeof content !== 'string' || !content.trim() || content.length > 2048;
  }) || value.rootFolderPath !== `focus/${subscriptionId}` || value.windowMonths !== 6
    || value.format !== 'Csv' || value.nativeSchedule !== 'Inactive' || value.destinationRole !== 'Storage Blob Data Contributor'
    || value.destinationRoleScope !== `${value.storageResourceId}/blobServices/default/containers/${value.container}`
    || !((value.state === 'missing' && value.canConfigure === true) || (value.state === 'configured' && value.canConfigure === false))) {
    throw new ApiRequestError('The export configuration response could not be verified for this subscription.', 502);
  }
}

export async function getCostExportConfiguration(subscriptionId: string, signal?: AbortSignal): Promise<FocusExportConfiguration> {
  const value = await apiRequest<FocusExportConfiguration>(`/api/schedules/${encodeURIComponent(subscriptionId)}/export`, { signal, cache: 'no-store' });
  validateExportConfiguration(value, subscriptionId);
  return value;
}

export async function configureCostExport(subscriptionId: string, allowDestinationRoleAssignment: boolean, signal?: AbortSignal): Promise<FocusExportConfiguration & { created: boolean }> {
  if (allowDestinationRoleAssignment !== true) throw new ApiRequestError('Confirm the destination-container role assignment before configuring the export.', 400);
  const value = await apiRequest<FocusExportConfiguration & { created: boolean }>(`/api/schedules/${encodeURIComponent(subscriptionId)}/export`, {
    method: 'PUT', cache: 'no-store', signal, body: JSON.stringify({ allowDestinationRoleAssignment }),
  });
  validateExportConfiguration(value, subscriptionId);
  if (value.state !== 'configured' || typeof value.created !== 'boolean') {
    throw new ApiRequestError('Export configuration could not be confirmed. Refresh its status before trying again.', 502);
  }
  return value;
}

export function createCostSchedule(subscriptionId: string, scheduleStartAt: string): Promise<CostSchedule> {
  return apiRequest('/api/schedules', {
    method: 'POST',
    body: JSON.stringify({ subscriptionId, scheduleStartAt }),
  });
}

export function setCostScheduleState(
  subscriptionId: string,
  state: Extract<ScheduleState, 'active' | 'paused'>,
  scheduleStartAt?: string,
): Promise<CostSchedule> {
  return apiRequest(`/api/schedules/${subscriptionId}`, {
    method: 'PATCH',
    body: JSON.stringify({ state, scheduleStartAt }),
  });
}

export function deleteCostSchedule(subscriptionId: string): Promise<void> {
  return apiRequest(`/api/schedules/${subscriptionId}`, { method: 'DELETE' });
}

export function runCostSchedule(subscriptionId: string): Promise<{ subscriptionId: string; status: string }> {
  return apiRequest(`/api/schedules/${subscriptionId}/run`, { method: 'POST' });
}

export function runAllCostSchedules(subscriptionIds: string[]): Promise<{
  subscriptionId: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  error?: string;
}[]> {
  return apiRequest('/api/schedules/run-all', {
    method: 'POST',
    body: JSON.stringify({ subscriptionIds }),
  });
}

export function listScheduleRuns(subscriptionId: string, signal?: AbortSignal): Promise<ScheduleRun[]> {
  return apiRequest(`/api/schedules/${subscriptionId}/runs`, { signal });
}

export function getExchangeRates(baseCurrency: string, signal?: AbortSignal): Promise<ExchangeRates> {
  return apiRequest(`/api/exchange-rates?base=${encodeURIComponent(baseCurrency)}`, { signal });
}

export function listFocusExportFiles(subscriptionId: string, signal?: AbortSignal): Promise<FocusExportFileList> {
  return apiRequest(`/api/focus-exports?subscriptionId=${encodeURIComponent(subscriptionId)}`, { signal });
}

export function focusExportDownloadUrl(subscriptionId: string, blobName: string): string {
  const parameters = new URLSearchParams({ subscriptionId, blobName });
  return `/api/focus-exports/download?${parameters.toString()}`;
}

export async function downloadFocusExport(
  subscriptionId: string,
  file: FocusExportFile,
): Promise<{ elapsedMs: number; bytes: number }> {
  const startedAt = performance.now();
  const response = await apiFetch(focusExportDownloadUrl(subscriptionId, file.blobName), {
    credentials: 'same-origin',
  });
  if (!response.ok) throw new Error(`Download failed: ${response.status}`);
  const content = await response.blob();
  if (content.size !== file.size) {
    throw new Error(`Downloaded ${content.size} bytes; expected ${file.size}`);
  }
  const url = URL.createObjectURL(content);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = file.fileName;
  anchor.click();
  URL.revokeObjectURL(url);
  return { elapsedMs: performance.now() - startedAt, bytes: content.size };
}

export function getRateOptimization(
  subscriptionIds: string[],
  lookBackPeriod: RecommendationLookBack,
  term: RecommendationTerm,
  reservationResourceType: ReservationResourceType,
  signal?: AbortSignal,
): Promise<RateOptimizationResponse> {
  return apiRequest('/api/rate-optimization', {
    method: 'POST',
    signal,
    body: JSON.stringify({ subscriptionIds, lookBackPeriod, term, reservationResourceType }),
  });
}

export function getCostAnomalies(
  subscriptionIds: string[],
  signal?: AbortSignal,
): Promise<AnomalySummary> {
  return apiRequest('/api/anomalies', {
    method: 'POST',
    signal,
    body: JSON.stringify({ subscriptionIds }),
  });
}

export type StorageOnboardingGuidance = {
  inventoryContainerName: string;
  lastAccessCommand: string;
  networkNotice: string;
  billingNotice: string;
};

export function storageOnboardingTemplateUrl(): string {
  const template = new URL('/api/storage-onboarding/template', window.location.origin).toString();
  return `https://portal.azure.com/#create/Microsoft.Template/uri/${encodeURIComponent(template)}`;
}

export function getStorageOnboardingGuidance(signal?: AbortSignal): Promise<StorageOnboardingGuidance> {
  return apiRequest('/api/storage-onboarding/guidance', { signal });
}

/**
 * Calls our own backend's single endpoint (same-origin, via Static Web Apps' linked
 * Function integration). Auth is enforced by staticwebapp.config.json's
 * "authenticated" role requirement on /api/* - the browser's SWA session cookie is
 * sent automatically with same-origin fetch, so no bearer token is needed here.
 */
export async function narrate(subscriptionIds: string[], report: FullReport): Promise<CostAgentOutput> {
  const res = await apiFetch('/api/narrate', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(toNarratePayload(subscriptionIds, report.executiveSummary.currentMonthlySpend, report.tierACategories)),
  });
  if (!res.ok) throw new Error(`Narration failed: ${res.status}`);
  return res.json();
}
