"""
Yard management dashboard data layer.
Computes live KPIs, queue lengths, turn times, dock utilization, and exception summaries.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from yard_manager import YardDatabase, YardManager, Dock, Trailer, DockStatus, TrailerStatus
    MANAGER_AVAILABLE = True
except ImportError:
    MANAGER_AVAILABLE = False


@dataclass
class KPITile:
    name: str
    value: float
    unit: str
    target: Optional[float] = None
    status: str = "normal"

    @property
    def on_target(self) -> bool:
        if self.target is None:
            return True
        return self.value <= self.target


@dataclass
class DashboardSnapshot:
    generated_at: float
    dock_utilization_pct: float
    avg_dwell_hours: float
    avg_turn_time_minutes: float
    queue_length: int
    active_trailers: int
    exceptions_open: int
    on_time_departure_pct: float
    tiles: List[KPITile] = field(default_factory=list)


class YardKPIEngine:
    """Computes real-time yard management KPIs from the database."""

    TARGET_DWELL_HOURS = 8.0
    TARGET_TURN_TIME_MIN = 120.0
    ON_TIME_THRESHOLD_HOURS = 12.0

    def __init__(self, db: "YardDatabase"):
        self.db = db

    def dock_utilization(self) -> float:
        df = self.db.query_df("SELECT status FROM docks")
        if df.empty:
            return 0.0
        occupied = (df["status"] == "occupied").sum()
        return round(float(occupied / len(df) * 100), 1)

    def avg_dwell_hours(self) -> float:
        now = time.time()
        df = self.db.query_df("SELECT gate_in_at, gate_out_at FROM trailers WHERE gate_in_at IS NOT NULL")
        if df.empty:
            return 0.0
        df["end"] = df["gate_out_at"].fillna(now)
        df["dwell_h"] = (df["end"] - df["gate_in_at"]) / 3600
        return round(float(df["dwell_h"].mean()), 2)

    def avg_turn_time_minutes(self) -> float:
        df = self.db.query_df(
            "SELECT requested_at, completed_at FROM moves WHERE completed_at IS NOT NULL"
        )
        if df.empty:
            return 0.0
        df["turn_min"] = (df["completed_at"] - df["requested_at"]) / 60
        return round(float(df["turn_min"].mean()), 1)

    def queue_length(self) -> int:
        df = self.db.query_df("SELECT COUNT(*) AS cnt FROM trailers WHERE status='in_queue'")
        return int(df["cnt"].iloc[0]) if not df.empty else 0

    def active_trailer_count(self) -> int:
        df = self.db.query_df("SELECT COUNT(*) AS cnt FROM trailers WHERE status NOT IN ('departed')")
        return int(df["cnt"].iloc[0]) if not df.empty else 0

    def open_exceptions(self) -> int:
        df = self.db.query_df("SELECT COUNT(*) AS cnt FROM exceptions WHERE resolved=0")
        return int(df["cnt"].iloc[0]) if not df.empty else 0

    def on_time_departure_pct(self) -> float:
        df = self.db.query_df(
            "SELECT gate_in_at, gate_out_at FROM trailers WHERE gate_out_at IS NOT NULL"
        )
        if df.empty:
            return 100.0
        df["dwell_h"] = (df["gate_out_at"] - df["gate_in_at"]) / 3600
        on_time = (df["dwell_h"] <= self.ON_TIME_THRESHOLD_HOURS).sum()
        return round(float(on_time / len(df) * 100), 1)

    def build_tiles(self) -> List[KPITile]:
        return [
            KPITile("Dock Utilization", self.dock_utilization(), "%", target=85.0),
            KPITile("Avg Dwell Time", self.avg_dwell_hours(), "hours",
                    target=self.TARGET_DWELL_HOURS),
            KPITile("Avg Turn Time", self.avg_turn_time_minutes(), "min",
                    target=self.TARGET_TURN_TIME_MIN),
            KPITile("Queue Length", float(self.queue_length()), "trailers"),
            KPITile("Active Trailers", float(self.active_trailer_count()), "trailers"),
            KPITile("Open Exceptions", float(self.open_exceptions()), "issues"),
            KPITile("On-Time Departure", self.on_time_departure_pct(), "%", target=90.0),
        ]


class YardDashboard:
    """
    Aggregates yard KPIs, dock status, trailer queue, exception summaries for dashboard rendering.
    """

    def __init__(self, db: "YardDatabase"):
        self.db = db
        self.kpi_engine = YardKPIEngine(db=db)

    def snapshot(self) -> DashboardSnapshot:
        tiles = self.kpi_engine.build_tiles()
        return DashboardSnapshot(
            generated_at=time.time(),
            dock_utilization_pct=self.kpi_engine.dock_utilization(),
            avg_dwell_hours=self.kpi_engine.avg_dwell_hours(),
            avg_turn_time_minutes=self.kpi_engine.avg_turn_time_minutes(),
            queue_length=self.kpi_engine.queue_length(),
            active_trailers=self.kpi_engine.active_trailer_count(),
            exceptions_open=self.kpi_engine.open_exceptions(),
            on_time_departure_pct=self.kpi_engine.on_time_departure_pct(),
            tiles=tiles,
        )

    def dock_status_board(self) -> pd.DataFrame:
        return self.db.query_df(
            "SELECT dock_id, dock_name, dock_type, status, current_trailer_id FROM docks ORDER BY dock_id"
        )

    def trailer_queue(self) -> pd.DataFrame:
        return self.db.query_df(
            """SELECT trailer_id, carrier_name, status, assigned_dock_id,
                      ROUND((COALESCE(gate_out_at, {now}) - gate_in_at) / 3600.0, 2) AS dwell_h
               FROM trailers
               WHERE status NOT IN ('departed')
               ORDER BY gate_in_at""".format(now=time.time())
        )

    def exception_summary(self) -> pd.DataFrame:
        return self.db.query_df(
            """SELECT exception_type, severity, COUNT(*) AS count,
                      SUM(resolved) AS resolved_count
               FROM exceptions
               GROUP BY exception_type, severity
               ORDER BY count DESC"""
        )

    def carrier_performance(self) -> pd.DataFrame:
        now = time.time()
        return self.db.query_df(f"""
            SELECT carrier_name,
                   COUNT(*) AS total_visits,
                   ROUND(AVG(COALESCE(gate_out_at, {now}) - gate_in_at) / 3600.0, 2) AS avg_dwell_h,
                   SUM(CASE WHEN status='departed' THEN 1 ELSE 0 END) AS departed
            FROM trailers
            WHERE gate_in_at IS NOT NULL
            GROUP BY carrier_name
            ORDER BY total_visits DESC
        """)

    def move_type_distribution(self) -> pd.DataFrame:
        return self.db.query_df(
            """SELECT move_type, COUNT(*) AS count,
                      ROUND(AVG(CASE WHEN completed_at IS NOT NULL
                                     THEN (completed_at - requested_at)/60.0
                                     ELSE NULL END), 1) AS avg_turn_min
               FROM moves GROUP BY move_type ORDER BY count DESC"""
        )

    def hourly_activity(self) -> pd.DataFrame:
        return self.db.query_df(
            """SELECT CAST(strftime('%H', timestamp, 'unixepoch') AS INTEGER) AS hour,
                      direction, COUNT(*) AS count
               FROM gate_log
               GROUP BY hour, direction
               ORDER BY hour"""
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if not MANAGER_AVAILABLE:
        print("yard_manager.py not found.")
    else:
        from yard_manager import (YardDatabase, YardManager, Dock, Trailer,
                                   ExceptionType, MoveType, YardMove)

        db = YardDatabase()
        ym = YardManager(db=db)

        for i in range(8):
            ym.register_dock(Dock(f"D{i+1:02d}", f"Dock {i+1}",
                                   "loading" if i < 4 else "unloading"))

        rng = np.random.default_rng(42)
        carriers = ["TransCo", "SpeedLine", "FastFreight", "QuickHaul", "MetroLog"]
        for i in range(12):
            t = Trailer(f"TRL_{i+1:03d}",
                         carriers[rng.integers(0, len(carriers))],
                         f"Driver_{i}", f"XX00YY{i:04d}",
                         "inbound" if i % 2 == 0 else "outbound")
            ym.gate_in(t)
            if i < 7:
                ym.assign_dock(t.trailer_id, f"D{(i % 8) + 1:02d}")
            if i < 4:
                ym.gate_out(t.trailer_id)

        ym.raise_exception("TRL_008", ExceptionType.OVERWEIGHT, "Over 40T limit.", "high")
        ym.raise_exception("TRL_010", ExceptionType.DOCUMENT_MISSING, "BOL not provided.", "medium")

        dashboard = YardDashboard(db=db)
        snap = dashboard.snapshot()

        print("Yard Dashboard\n")
        for tile in snap.tiles:
            target_str = f" (target: {tile.target})" if tile.target else ""
            print(f"  {tile.name}: {tile.value} {tile.unit}{target_str}")

        print("\nDock status board:")
        print(dashboard.dock_status_board().to_string(index=False))

        print("\nTrailer queue:")
        tq = dashboard.trailer_queue()
        if not tq.empty:
            print(tq.to_string(index=False))

        print("\nException summary:")
        es = dashboard.exception_summary()
        if not es.empty:
            print(es.to_string(index=False))

        print("\nCarrier performance:")
        cp = dashboard.carrier_performance()
        if not cp.empty:
            print(cp.to_string(index=False))
