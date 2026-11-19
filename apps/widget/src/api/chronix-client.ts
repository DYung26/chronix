import type { ActiveTask } from "../domain/active-task";

export interface ActiveTasksResponse {
  serverTime: string;
  tasks: ActiveTask[];
  pollAfterMs: number;
}

interface ApiPayload {
  ok?: unknown;
  serverTime?: unknown;
  tasks?: unknown;
  pollAfterMs?: unknown;
}

interface FetchActiveTasksOptions {
  baseUrl?: string;
  signal?: AbortSignal;
  fetcher?: typeof fetch;
}

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8766";
const DEFAULT_POLL_INTERVAL_MS = 10_000;

function isActiveTask(value: unknown): value is ActiveTask {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const task = value as Record<string, unknown>;
  return (
    typeof task.id === "string" &&
    typeof task.title === "string" &&
    typeof task.startAt === "string" &&
    typeof task.endAt === "string" &&
    (task.track === undefined || typeof task.track === "string")
  );
}

export async function fetchActiveTasks(
  options: FetchActiveTasksOptions = {},
): Promise<ActiveTasksResponse> {
  const baseUrl =
    options.baseUrl ??
    import.meta.env.VITE_CHRONIX_API_BASE_URL ??
    DEFAULT_API_BASE_URL;
  const fetcher = options.fetcher ?? fetch;
  const endpoint = `${baseUrl.replace(/\/+$/, "")}/api/v1/active-tasks`;
  const response = await fetcher(endpoint, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal: options.signal,
  });

  if (!response.ok) {
    throw new Error(`Chronix API returned HTTP ${response.status}`);
  }

  const payload = (await response.json()) as ApiPayload;
  const serverTime =
    typeof payload.serverTime === "string" ? payload.serverTime : "";
  const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];

  if (
    payload.ok !== true ||
    !Number.isFinite(Date.parse(serverTime)) ||
    !tasks.every(isActiveTask)
  ) {
    throw new Error("Chronix API returned an invalid active-task payload");
  }

  return {
    serverTime,
    tasks,
    pollAfterMs:
      typeof payload.pollAfterMs === "number" &&
      Number.isFinite(payload.pollAfterMs) &&
      payload.pollAfterMs >= 1_000
        ? payload.pollAfterMs
        : DEFAULT_POLL_INTERVAL_MS,
  };
}
