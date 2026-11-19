import { apiFetch } from './apiIdentity';
import type { BackgroundAssessmentJob } from './api';

export type TargetConnection = {
  revision: number; subscriptionId: string; subscriptionName: string;
  tenantId: string | null; clientId: string | null; hasCredentials: boolean;
};
export type ConnectionInput = {
  subscriptionId: string; tenantId?: string; clientId?: string; clientSecret?: string; clearCredentials?: boolean;
};
export type UploadStatus = {
  datasetId: string; subscriptionId: string; status: 'uploading' | 'queued' | 'processing' | 'ready' | 'failed';
  createdAt: string; chunkBytes: number; error: string | null; job: BackgroundAssessmentJob | null;
  coverage: { startDate: string; endDate: string; observedDays: number; rowCount: number; currency: string } | null;
  files: { fileId: string; name: string; size: number; receivedBytes: number }[];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`/api/admin${path}`, init);
  if (!response.ok) {
    if (!response.headers.get('Content-Type')?.includes('application/json')) {
      throw new Error(`Workspace request failed (HTTP ${response.status}). Check API availability and reverse-proxy upload limits.`);
    }
    const body: { detail?: string | { msg?: string }[] } = await response.json();
    throw new Error(typeof body.detail === 'string' ? body.detail
      : Array.isArray(body.detail) ? body.detail.map((item) => item.msg ?? 'Invalid input.').join(' ')
        : `The request failed (${response.status}).`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export const getConnection = (signal?: AbortSignal) =>
  request<{ connection: TargetConnection | null }>('/configuration', { signal });
export const saveConnection = (body: ConnectionInput) =>
  request<{ connection: TargetConnection }>('/configuration', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
export const getUploads = (signal?: AbortSignal) => request<UploadStatus[]>('/uploads', { signal });
export const getUpload = (id: string, signal?: AbortSignal) => request<UploadStatus>(`/uploads/${encodeURIComponent(id)}`, { signal });
export const completeUpload = (id: string) => request<BackgroundAssessmentJob>(`/uploads/${encodeURIComponent(id)}/complete`, { method: 'POST' });
export const retryUpload = (id: string) => request<BackgroundAssessmentJob>(`/uploads/${encodeURIComponent(id)}/retry`, { method: 'POST' });
export const discardUpload = (id: string) => request<void>(`/uploads/${encodeURIComponent(id)}/discard`, { method: 'POST' });

export function matchingFiles(upload: UploadStatus, files: File[]): boolean {
  return upload.files.length === files.length && upload.files.every((item, index) =>
    item.name === files[index].name && item.size === files[index].size);
}

async function verifyResume(upload: UploadStatus, item: UploadStatus['files'][number], source: File, signal: AbortSignal) {
  let after = -1;
  while (item.receivedBytes > 0) {
    const page = await request<{ items: { offset: number; length: number; sha256: string }[]; nextOffset: number | null }>(
      `/uploads/${upload.datasetId}/files/${item.fileId}/chunks?after=${after}`, { signal },
    );
    for (const chunk of page.items) {
      if (signal.aborted) throw new DOMException('Upload paused', 'AbortError');
      const digest = await crypto.subtle.digest('SHA-256', await source.slice(chunk.offset, chunk.offset + chunk.length).arrayBuffer());
      const actual = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
      if (actual !== chunk.sha256) throw new Error('The selected file differs from the interrupted upload. Start a new upload instead.');
    }
    if (page.nextOffset === null) return;
    after = page.nextOffset;
  }
}

export async function uploadFiles(
  files: File[], resume: UploadStatus | null, signal: AbortSignal,
  progress: (upload: UploadStatus, transferred: number, total: number) => void,
): Promise<UploadStatus> {
  const upload = resume ?? await request<UploadStatus>('/uploads', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ files: files.map((file) => ({ name: file.name, size: file.size })) }), signal,
  });
  if (!matchingFiles(upload, files)) throw new Error('Select the same files in the same order to resume this upload.');
  const total = files.reduce((sum, file) => sum + file.size, 0);
  let transferred = upload.files.reduce((sum, item) => sum + item.receivedBytes, 0);
  progress(upload, transferred, total);
  for (const [index, item] of upload.files.entries()) {
    await verifyResume(upload, item, files[index], signal);
    for (let offset = item.receivedBytes; offset < item.size; offset += upload.chunkBytes) {
      const content = files[index].slice(offset, Math.min(offset + upload.chunkBytes, item.size));
      await request(`/uploads/${upload.datasetId}/files/${item.fileId}?offset=${offset}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: content, signal,
      });
      transferred += content.size;
      progress(upload, transferred, total);
    }
  }
  return getUpload(upload.datasetId, signal);
}
