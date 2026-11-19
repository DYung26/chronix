import type { ActiveTask } from "../domain/active-task";
import { getTaskProgress } from "../domain/active-task";

interface TaskRingsProps {
  tasks: ActiveTask[];
  now: number;
}

const INNER_RADIUS = 27;
const RING_SPACING = 7;
const STROKE_WIDTH = 3;
const EDGE_PADDING = 5;
const MINIMUM_WINDOW_SIZE = 90;

export function getTaskRingsSize(taskCount: number): number {
  const count = Math.max(0, Math.floor(taskCount));
  const outerRadius = INNER_RADIUS + Math.max(0, count - 1) * RING_SPACING;
  return (outerRadius + EDGE_PADDING) * 2;
}

export function getTaskRingsWindowSize(taskCount: number): number {
  return Math.max(MINIMUM_WINDOW_SIZE, getTaskRingsSize(taskCount));
}

export function TaskRings({ tasks, now }: TaskRingsProps) {
  const size = getTaskRingsSize(tasks.length);
  const center = size / 2;

  return (
    <svg
      className="task-rings"
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={`${tasks.length} active tasks`}
    >
      {tasks.map((task, index) => {
        const radius = INNER_RADIUS + index * RING_SPACING;
        const circumference = 2 * Math.PI * radius;
        const progress = getTaskProgress(task, now);

        return (
          <g key={task.id}>
            <circle
              className="task-rings__track"
              cx={center}
              cy={center}
              r={radius}
              strokeWidth={STROKE_WIDTH}
            />
            <circle
              className="task-rings__progress"
              cx={center}
              cy={center}
              r={radius}
              strokeWidth={STROKE_WIDTH}
              strokeDasharray={circumference}
              strokeDashoffset={circumference * (1 - progress)}
              transform={`rotate(-90 ${center} ${center})`}
            />
          </g>
        );
      })}
      <text className="task-rings__count" x={center} y={center}>
        {tasks.length}
      </text>
    </svg>
  );
}
