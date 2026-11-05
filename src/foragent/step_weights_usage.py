"""Cumulative tree CPU and reported memory high water; no signals or polling.

Live members contribute own CPU plus waited descendants. A reaped member moves
into its parent's children counters. On macOS psutil's children counters are zero:
the runner reads its own rusage at phase boundaries. OS memory high-water marks
are lifetime bounds, so an earlier larger peak may conservatively carry forward.
"""

import os
import resource
import sys

import psutil


def _self_usage(pid):
    if pid != os.getpid():
        return None
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    scale = 1 if sys.platform == "darwin" else 1024
    return (own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime,
            own.ru_maxrss * scale, children.ru_maxrss * scale)


def snapshot(root: dict, identities=()) -> dict | None:
    try:
        parent = psutil.Process(root["pid"])
        if parent.create_time() != root["start_time"]:
            return None
        known = {(item["pid"], item["start_time"]) for item in identities}
        known.add((root["pid"], root["start_time"]))
        known.update((child.pid, child.create_time()) for child in parent.children(recursive=True))
        members, total, reaped_peak = [], 0.0, 0
        for pid, start in known:
            try:
                process = psutil.Process(pid)
                if process.create_time() != start or process.status() == psutil.STATUS_ZOMBIE:
                    continue
                times, info = process.cpu_times(), process.memory_info()
                own = times.user + times.system
                children = getattr(times, "children_user", 0) + getattr(times, "children_system", 0)
                peak = max(info.rss, getattr(info, "peak_wset", 0))
                if (value := _self_usage(pid)) is not None:
                    own, children, reported_peak, waited_peak = value
                    peak = max(peak, reported_peak)
                    reaped_peak = max(reaped_peak, waited_peak)
                if psutil.Process(pid).create_time() != start:
                    return None
                total += own + children
                members.append({"pid": pid, "start": start, "memory": peak})
            except psutil.NoSuchProcess:
                continue
        if psutil.Process(root["pid"]).create_time() != root["start_time"]:
            return None
        return {"cpu": total, "members": members, "root": root["pid"], "reaped_peak": reaped_peak}
    except (psutil.Error, OSError):
        return None


def memory(sample, *, before=None):
    members = [item for item in sample["members"] if before is None or item["start"] < before]
    live = sum(item["memory"] for item in members)
    root = next((item["memory"] for item in members if item["pid"] == sample["root"]), 0)
    return max(live, root + sample["reaped_peak"])
