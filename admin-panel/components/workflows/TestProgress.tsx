"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

export type TestStateCheck = {
  event?: number;
  matched?: boolean;
  workflow_status?: string | null;
  events_received?: number | null;
  completed_steps?: string[];
  outcome?: string;
  error?: string | null;
};

export type TestResults = {
  all_passed?: boolean;
  title?: string;
  summary?: string[];
  scenario?: { description?: string; type?: string };
  events_processed?: number;
  workflow_triggered?: number;
  state_checks?: TestStateCheck[];
  failures?: { event?: number; error?: string; outcome?: string }[];
};

export type TestJob = {
  job_id: string;
  stage: "queued" | "checking_temporal" | "running" | "cleanup" | "completed" | "failed";
  started_at?: string;
  updated_at?: string;
  events_total?: number;
  events_processed?: number;
  events?: TestStateCheck[];
  scenario?: { description?: string; type?: string };
  workflow_name?: string;
  failures_so_far?: number;
  all_passed?: boolean;
  error?: string;
  hint?: string;
  results?: TestResults;
};

const STAGE_LABEL: Record<TestJob["stage"], string> = {
  queued: "Queued",
  checking_temporal: "Checking Temporal and worker",
  running: "Running synthetic events",
  cleanup: "Cleaning up test instances",
  completed: "Completed",
  failed: "Failed",
};

export function isJobActive(job: TestJob | null): boolean {
  return Boolean(job) && job!.stage !== "completed" && job!.stage !== "failed";
}

/** Poll a test job until it finishes. `path` is null while no job is running. */
export function useTestJobPolling(
  path: string | null,
  onFinished?: (job: TestJob) => void,
) {
  const [job, setJob] = useState<TestJob | null>(null);
  const [error, setError] = useState("");
  const finishedRef = useRef(onFinished);

  useEffect(() => {
    finishedRef.current = onFinished;
  }, [onFinished]);

  useEffect(() => {
    if (!path) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await api<TestJob>(path);
        if (stopped) return;
        setJob(next);
        if (isJobActive(next)) {
          timer = setTimeout(poll, 1000);
        } else {
          finishedRef.current?.(next);
        }
      } catch (cause) {
        if (stopped) return;
        setError((cause as Error).message);
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [path]);

  return { job, error, reset: () => { setJob(null); setError(""); } };
}

function Elapsed({ startedAt, frozen }: { startedAt?: string; frozen: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (frozen) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [frozen]);
  if (!startedAt) return null;
  const seconds = Math.max(0, Math.round((now - new Date(startedAt).getTime()) / 1000));
  const minutes = Math.floor(seconds / 60);
  return <span className="mono">{minutes ? `${minutes}m ` : ""}{seconds % 60}s</span>;
}

export function TestProgress({ job }: { job: TestJob }) {
  const total = job.events_total ?? 0;
  const done = job.events_processed ?? 0;
  const percent = total ? Math.round((done / total) * 100) : job.stage === "completed" ? 100 : 8;
  const events = job.stage === "completed" ? job.results?.state_checks ?? job.events : job.events;

  return <div className="stack gap3 mt3">
    <div className="row gap3" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
      <span className="flabel">{STAGE_LABEL[job.stage]}</span>
      <span className="faint">
        {total ? <span className="mono">{done}/{total} events · </span> : null}
        <Elapsed startedAt={job.started_at} frozen={!isJobActive(job)} />
      </span>
    </div>
    <div className={`bar ${job.stage === "failed" ? "bar-neg" : job.stage === "completed" ? "bar-pos" : "bar-striped"}`}>
      <i style={{ width: `${percent}%` }} />
    </div>
    {job.scenario?.description ? <p className="faint">Scenario: {job.scenario.description}</p> : null}
    {job.stage === "failed" ? <div className="alert alert-neg"><div>
      <b>{job.error || "Test failed"}</b>
      {job.hint ? <p>{job.hint}</p> : null}
    </div></div> : null}
    {events?.length ? <div className="trace">
      {events.map((check, index) => (
        <div className="trace-node" data-kind={check.error ? "neg" : check.matched ? "ok" : "brand"} key={check.event ?? index}>
          <span className="trace-mark">·</span>
          <div>
            <div className="trace-title">Event {check.event ?? index + 1} · {check.outcome || check.workflow_status || "pending"}</div>
            <div className="trace-meta">
              {check.events_received ?? 0} events in instance · {(check.completed_steps || []).join(", ") || "waiting"}
              {check.error ? ` · ${check.error}` : ""}
            </div>
          </div>
        </div>
      ))}
    </div> : null}
  </div>;
}
