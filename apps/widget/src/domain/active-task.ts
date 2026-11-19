export interface ActiveTask {
  id: string;
  title: string;
  startAt: string;
  endAt: string;
  track?: string;
}

export function getTaskProgress(task: ActiveTask, now: number): number {
  const start = Date.parse(task.startAt);
  const end = Date.parse(task.endAt);

  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return 0;
  }

  return Math.min(1, Math.max(0, (end - now) / (end - start)));
}

export function isTaskActive(task: ActiveTask, now: number): boolean {
  const start = Date.parse(task.startAt);
  const end = Date.parse(task.endAt);

  return (
    Number.isFinite(start) &&
    Number.isFinite(end) &&
    end > start &&
    start <= now &&
    now < end
  );
}
