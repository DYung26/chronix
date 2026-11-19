import { useEffect, useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { LogicalSize } from "@tauri-apps/api/dpi";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { fetchActiveTasks } from "./api/chronix-client";
import {
  getTaskRingsWindowSize,
  TaskRings,
} from "./components/TaskRings";
import { isTaskActive, type ActiveTask } from "./domain/active-task";
import "./App.css";

const DEFAULT_POLL_INTERVAL_MS = 10_000;

export default function App() {
  const [now, setNow] = useState(() => Date.now());
  const [clockOffsetMs, setClockOffsetMs] = useState(0);
  const [tasks, setTasks] = useState<ActiveTask[]>([]);

  useEffect(() => {
    const interval = window.setInterval(
      () => setNow(Date.now() + clockOffsetMs),
      1000,
    );
    return () => window.clearInterval(interval);
  }, [clockOffsetMs]);

  useEffect(() => {
    let disposed = false;
    let timeout: number | undefined;
    let pollIntervalMs = DEFAULT_POLL_INTERVAL_MS;

    const poll = async () => {
      try {
        const response = await fetchActiveTasks();
        if (disposed) {
          return;
        }

        setTasks(response.tasks);
        const serverTime = Date.parse(response.serverTime);
        if (Number.isFinite(serverTime)) {
          setClockOffsetMs(serverTime - Date.now());
        }
        pollIntervalMs = response.pollAfterMs;
      } catch {
        // Keep the last known task set; expired intervals are filtered locally.
      }

      if (!disposed) {
        timeout = window.setTimeout(poll, pollIntervalMs);
      }
    };

    void poll();

    return () => {
      disposed = true;
      if (timeout !== undefined) {
        window.clearTimeout(timeout);
      }
    };
  }, []);

  const activeTasks = tasks.filter((task) => isTaskActive(task, now));

  useEffect(() => {
    if (!isTauri()) {
      return;
    }

    const size = getTaskRingsWindowSize(activeTasks.length);
    void getCurrentWindow()
      .setSize(new LogicalSize(size, size))
      .catch((error: unknown) => {
        console.warn("Unable to resize the Chronix widget window", error);
      });
  }, [activeTasks.length]);

  return (
    <main className="widget-surface">
      <TaskRings tasks={activeTasks} now={now} />
    </main>
  );
}
