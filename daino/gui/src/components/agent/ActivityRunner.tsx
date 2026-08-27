import { useEffect, useRef, useState } from "react";
import { useAgentStore } from "../../store/agentStore";
import {
  ACTIVITY_COLOR,
  ACTIVITY_LABELS,
  isRunning,
  type ActivityState,
} from "../../lib/activity";

/**
 * The runner: a dinosaur that runs while D[Ai]NO is working.
 *
 * The terminal client draws this with block glyphs
 * (daino/tui/widgets/checklist.py); the browser draws the same idea as pixel
 * sprites, from the same activity states. It is not decoration — a long turn is
 * otherwise indistinguishable from a stalled one, and the three outcomes the
 * user needs at a glance are "running", "finished", and "failed":
 *
 * - running   — legs cycle, ground scrolls, and a cactus is jumped
 * - completed — the dinosaur stands still, green
 * - failed    — the cactus freezes against it, red, with a cross
 */

// Pixel maps, one character per pixel. Hand-drawn rather than an image so they
// follow the theme (they are filled with currentColor) and cost no request.
const DINO_BODY = [
  "              xxxxxxxx  ",
  "             xxxxxxxxxx ",
  "             xxx xxxxxxx",
  "             xxxxxxxxxxx",
  "             xxxxxxxxxx ",
  "             xxxxxxxx   ",
  "             xxxxxxxxx  ",
  "  x          xxxxxx     ",
  "  xx       xxxxxxxx     ",
  "  xxx     xxxxxxxxxx    ",
  "  xxxx   xxxxxxxxxxxx   ",
  "  xxxxxxxxxxxxxxxxxx    ",
  "   xxxxxxxxxxxxxxxxx    ",
  "    xxxxxxxxxxxxxxx     ",
  "     xxxxxxxxxxxxx      ",
  "      xxxxxxxxxxx       ",
  "       xxxxxxxxx        ",
];

/** Leg frames, appended under the body: two running poses and one standing. */
const LEGS = [
  ["       xxx   xxx        ", "       xx      x        ", "       xx               "],
  ["       xxx   xxx        ", "        x     xx        ", "              xx        "],
];
const LEGS_STANDING = [
  "       xxx   xxx        ",
  "       xx     xx        ",
  "       xx     xx        ",
];

const CACTUS = [
  "    xx    ",
  "    xx    ",
  " x  xx    ",
  " x  xx  x ",
  " x  xx  x ",
  " xxxxx  x ",
  "    xx xx ",
  "    xxxx  ",
  "    xx    ",
  "    xx    ",
  "    xx    ",
];

const CLOUD = [
  "   xxxxx  ",
  " xxxxxxxxx",
  "xxxxxxxxxx",
  " xxxxxxxx ",
];

const PIXEL = 2; // one sprite pixel, in SVG units (= CSS px at 1:1)
const STAGE_HEIGHT = 52;
/** Used until the panel has been measured. */
const FALLBACK_WIDTH = 220;
const GROUND_Y = 44;
const DINO_X = 10;
const SPEED = 62; // SVG units per second
const LEG_INTERVAL_MS = 110;

/** Collapse a pixel map into one SVG path — a rect per pixel would be hundreds. */
function pixelPath(rows: string[], originX: number, originY: number): string {
  const parts: string[] = [];
  rows.forEach((row, y) => {
    let runStart = -1;
    for (let x = 0; x <= row.length; x += 1) {
      const filled = row[x] === "x";
      if (filled && runStart < 0) runStart = x;
      if (!filled && runStart >= 0) {
        const width = (x - runStart) * PIXEL;
        parts.push(
          `M${originX + runStart * PIXEL} ${originY + y * PIXEL}h${width}v${PIXEL}h-${width}z`,
        );
        runStart = -1;
      }
    }
  });
  return parts.join("");
}

const DINO_HEIGHT = (DINO_BODY.length + 3) * PIXEL;
const CACTUS_HEIGHT = CACTUS.length * PIXEL;

const DINO_PATHS = [
  pixelPath([...DINO_BODY, ...LEGS[0]], 0, 0),
  pixelPath([...DINO_BODY, ...LEGS[1]], 0, 0),
];
const DINO_STANDING_PATH = pixelPath([...DINO_BODY, ...LEGS_STANDING], 0, 0);
const CACTUS_PATH = pixelPath(CACTUS, 0, 0);
const CLOUD_PATH = pixelPath(CLOUD, 0, 0);

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

export function ActivityRunner() {
  const activity = useAgentStore((s) => s.activity);
  const running = isRunning(activity.state);
  const failed = activity.state === "failed";

  const [frame, setFrame] = useState({
    legs: 0,
    cactusX: FALLBACK_WIDTH,
    ground: 0,
    cloud: FALLBACK_WIDTH * 0.6,
  });
  const rafRef = useRef<number | null>(null);
  const stateRef = useRef({ ...frame, lastLegSwap: 0 });
  const reduced = prefersReducedMotion();

  /**
   * The stage is measured rather than scaled.
   *
   * Scaling a fixed viewBox to the panel either distorts the pixels or leaves
   * the ground stopping short of the edge; matching the viewBox to the real
   * width keeps one sprite pixel exactly ${PIXEL} CSS px at any panel size, and
   * lets the cactus cross the whole strip.
   */
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  const widthRef = useRef(FALLBACK_WIDTH);
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const observer = new ResizeObserver((entries) => {
      const measured = Math.max(120, Math.round(entries[0].contentRect.width));
      widthRef.current = measured;
      setWidth(measured);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!running || reduced) {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      return;
    }
    // A new run starts the cactus off-stage rather than wherever the last one
    // stopped, so the jump reads as one continuous attempt.
    stateRef.current = {
      legs: 0,
      cactusX: widthRef.current,
      ground: stateRef.current.ground,
      cloud: stateRef.current.cloud,
      lastLegSwap: 0,
    };

    let previous = performance.now();
    const tick = (now: number) => {
      const elapsed = Math.min(now - previous, 100) / 1000;
      previous = now;
      const current = stateRef.current;
      const stage = widthRef.current;
      current.cactusX -= SPEED * elapsed;
      if (current.cactusX < -CACTUS[0].length * PIXEL) current.cactusX = stage + 24;
      current.ground = (current.ground + SPEED * elapsed) % 24;
      current.cloud = (current.cloud - SPEED * 0.12 * elapsed + stage) % stage;
      if (now - current.lastLegSwap > LEG_INTERVAL_MS) {
        current.legs = current.legs === 0 ? 1 : 0;
        current.lastLegSwap = now;
      }
      setFrame({
        legs: current.legs,
        cactusX: current.cactusX,
        ground: current.ground,
        cloud: current.cloud,
      });
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [running, reduced]);

  // Jump over the cactus: a parabola over the span where it would be hit.
  const dinoLeft = DINO_X;
  const dinoRight = DINO_X + 24 * PIXEL;
  const jumpSpan = 46;
  const distance = frame.cactusX - dinoRight;
  const jumping = running && distance > -jumpSpan * 0.7 && distance < jumpSpan;
  const progress = jumping ? 1 - Math.abs(distance) / jumpSpan : 0;
  const lift = jumping ? Math.sin(progress * Math.PI) * 17 : 0;

  const color = ACTIVITY_COLOR[activity.state];
  const dinoY = GROUND_Y - DINO_HEIGHT - lift;
  // Failure parks the obstacle against the dinosaur instead of leaving the
  // error as one more abstract colour, exactly as the TUI does.
  const cactusX = failed ? dinoRight - 6 : frame.cactusX;
  const showCactus = running || failed;

  return (
    <div className="activity-runner" data-state={activity.state} ref={hostRef}>
      <svg
        viewBox={`0 0 ${width} ${STAGE_HEIGHT}`}
        width={width}
        height={STAGE_HEIGHT}
        role="img"
        aria-label={`${ACTIVITY_LABELS[activity.state]}${
          activity.detail ? `: ${activity.detail}` : ""
        }`}
        preserveAspectRatio="xMinYMax meet"
        shapeRendering="crispEdges"
      >
        <path
          d={CLOUD_PATH}
          transform={`translate(${reduced ? width * 0.62 : frame.cloud} 4)`}
          className="runner-cloud"
        />
        <line
          x1="0"
          y1={GROUND_Y + 1}
          x2={width}
          y2={GROUND_Y + 1}
          className="runner-ground"
          strokeDasharray="14 5 3 6"
          strokeDashoffset={-frame.ground}
        />
        {showCactus && (
          <path
            d={CACTUS_PATH}
            transform={`translate(${cactusX} ${GROUND_Y - CACTUS_HEIGHT})`}
            fill={failed ? "var(--red)" : "var(--text-3)"}
          />
        )}
        <path
          d={running && !reduced ? DINO_PATHS[frame.legs] : DINO_STANDING_PATH}
          transform={`translate(${dinoLeft} ${dinoY})`}
          fill={color}
        />
        {failed && (
          <path
            d={pixelPath(["x   x", " x x ", "  x  ", " x x ", "x   x"], 0, 0)}
            transform={`translate(${dinoRight + 6} ${GROUND_Y - DINO_HEIGHT - 4})`}
            fill="var(--red)"
          />
        )}
      </svg>
      <div className="runner-label">
        <span className="state" style={{ color }}>
          {ACTIVITY_LABELS[activity.state]}
        </span>
        {activity.detail && <span className="detail">{activity.detail}</span>}
      </div>
    </div>
  );
}

export type { ActivityState };
