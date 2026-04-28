"""
Yard management platform core module.
Manages trailers, docks, moves, exceptions, gate logs, and real-time status tracking.
"""
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DockStatus(str, Enum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    RESERVED = "reserved"
    MAINTENANCE = "maintenance"
    BLOCKED = "blocked"


class TrailerStatus(str, Enum):
    IN_QUEUE = "in_queue"
    AT_GATE = "at_gate"
    IN_YARD = "in_yard"
    AT_DOCK = "at_dock"
    LOADING = "loading"
    UNLOADING = "unloading"
    READY_TO_DEPART = "ready_to_depart"
    DEPARTED = "departed"
    HELD = "held"


class MoveType(str, Enum):
    GATE_TO_YARD = "gate_to_yard"
    YARD_TO_DOCK = "yard_to_dock"
    DOCK_TO_YARD = "dock_to_yard"
    YARD_TO_GATE = "yard_to_gate"
    DOCK_TO_DOCK = "dock_to_dock"


class ExceptionType(str, Enum):
    UNAUTHORIZED_CARRIER = "unauthorized_carrier"
    INSPECTION_FAILED = "inspection_failed"
    OVERWEIGHT = "overweight"
    DOCUMENT_MISSING = "document_missing"
    DAMAGE_REPORTED = "damage_reported"
    SLA_BREACH = "sla_breach"
    DWELL_TIME_EXCEEDED = "dwell_time_exceeded"


@dataclass
class Dock:
    dock_id: str
    dock_name: str
    dock_type: str
    status: DockStatus = DockStatus.AVAILABLE
    current_trailer_id: Optional[str] = None
    last_updated: float = field(default_factory=time.time)


@dataclass
class Trailer:
    trailer_id: str
    carrier_name: str
    driver_name: str
    license_plate: str
    shipment_type: str
    status: TrailerStatus = TrailerStatus.IN_QUEUE
    assigned_dock_id: Optional[str] = None
    gate_in_at: Optional[float] = None
    gate_out_at: Optional[float] = None
    notes: str = ""

    @property
    def dwell_time_hours(self) -> Optional[float]:
        if self.gate_in_at is None:
            return None
        end = self.gate_out_at or time.time()
        return (end - self.gate_in_at) / 3600


@dataclass
class YardMove:
    move_id: str
    trailer_id: str
    move_type: MoveType
    from_location: str
    to_location: str
    jockey_id: Optional[str]
    requested_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    @property
    def turnaround_minutes(self) -> Optional[float]:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.requested_at) / 60


@dataclass
class YardException:
    exception_id: str
    trailer_id: str
    exception_type: ExceptionType
    description: str
    severity: str
    resolved: bool = False
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None


class YardDatabase:
    """SQLite-backed persistence for yard management."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS docks (
        dock_id TEXT PRIMARY KEY, dock_name TEXT, dock_type TEXT,
        status TEXT, current_trailer_id TEXT, last_updated REAL
    );
    CREATE TABLE IF NOT EXISTS trailers (
        trailer_id TEXT PRIMARY KEY, carrier_name TEXT, driver_name TEXT,
        license_plate TEXT, shipment_type TEXT, status TEXT,
        assigned_dock_id TEXT, gate_in_at REAL, gate_out_at REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS moves (
        move_id TEXT PRIMARY KEY, trailer_id TEXT, move_type TEXT,
        from_location TEXT, to_location TEXT, jockey_id TEXT,
        requested_at REAL, completed_at REAL
    );
    CREATE TABLE IF NOT EXISTS gate_log (
        log_id TEXT PRIMARY KEY, trailer_id TEXT, direction TEXT,
        gate_id TEXT, operator_id TEXT, timestamp REAL
    );
    CREATE TABLE IF NOT EXISTS exceptions (
        exception_id TEXT PRIMARY KEY, trailer_id TEXT, exception_type TEXT,
        description TEXT, severity TEXT, resolved INTEGER, created_at REAL, resolved_at REAL
    );
    CREATE TABLE IF NOT EXISTS audit_trail (
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT, entity_id TEXT,
        action TEXT, old_value TEXT, new_value TEXT, actor TEXT, timestamp REAL
    );
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def save_dock(self, d: Dock) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO docks VALUES (?,?,?,?,?,?)",
            (d.dock_id, d.dock_name, d.dock_type, d.status.value,
             d.current_trailer_id, d.last_updated),
        )
        self.conn.commit()

    def save_trailer(self, t: Trailer) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trailers VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t.trailer_id, t.carrier_name, t.driver_name, t.license_plate,
             t.shipment_type, t.status.value, t.assigned_dock_id,
             t.gate_in_at, t.gate_out_at, t.notes),
        )
        self.conn.commit()

    def save_move(self, m: YardMove) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO moves VALUES (?,?,?,?,?,?,?,?)",
            (m.move_id, m.trailer_id, m.move_type.value, m.from_location,
             m.to_location, m.jockey_id, m.requested_at, m.completed_at),
        )
        self.conn.commit()

    def save_exception(self, exc: YardException) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO exceptions VALUES (?,?,?,?,?,?,?,?)",
            (exc.exception_id, exc.trailer_id, exc.exception_type.value,
             exc.description, exc.severity, int(exc.resolved),
             exc.created_at, exc.resolved_at),
        )
        self.conn.commit()

    def log_gate(self, trailer_id: str, direction: str, gate_id: str, operator_id: str) -> None:
        log_id = hashlib.md5(f"{trailer_id}{time.time()}".encode()).hexdigest()[:12]
        self.conn.execute(
            "INSERT INTO gate_log VALUES (?,?,?,?,?,?)",
            (log_id, trailer_id, direction, gate_id, operator_id, time.time()),
        )
        self.conn.commit()

    def audit(self, entity_type: str, entity_id: str, action: str,
              old_val: str, new_val: str, actor: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_trail (entity_type,entity_id,action,old_value,new_value,actor,timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity_type, entity_id, action, old_val, new_val, actor, time.time()),
        )
        self.conn.commit()

    def query_df(self, sql: str) -> "pd.DataFrame":
        import pandas as pd
        return pd.read_sql_query(sql, self.conn)


class YardManager:
    """
    Core yard management engine handling arrivals, dock assignments, moves,
    departures, and exception handling.
    """

    MAX_DWELL_HOURS = 24.0

    def __init__(self, db: YardDatabase):
        self.db = db
        self._docks: Dict[str, Dock] = {}
        self._trailers: Dict[str, Trailer] = {}

    def register_dock(self, dock: Dock) -> None:
        self._docks[dock.dock_id] = dock
        self.db.save_dock(dock)

    def gate_in(self, trailer: Trailer, gate_id: str = "GATE_1",
                operator_id: str = "OP_001") -> Dict[str, Any]:
        trailer.status = TrailerStatus.AT_GATE
        trailer.gate_in_at = time.time()
        self._trailers[trailer.trailer_id] = trailer
        self.db.save_trailer(trailer)
        self.db.log_gate(trailer.trailer_id, "IN", gate_id, operator_id)
        self.db.audit("trailer", trailer.trailer_id, "gate_in", "", "at_gate", operator_id)
        logger.info("Gate IN: %s (%s)", trailer.trailer_id, trailer.carrier_name)
        return {"trailer_id": trailer.trailer_id, "status": "at_gate", "gate": gate_id}

    def assign_dock(self, trailer_id: str, dock_id: str,
                     jockey_id: str = "JOCKEY_001") -> Dict[str, Any]:
        if trailer_id not in self._trailers:
            return {"error": "Trailer not found."}
        if dock_id not in self._docks:
            return {"error": "Dock not found."}
        dock = self._docks[dock_id]
        if dock.status != DockStatus.AVAILABLE:
            return {"error": f"Dock {dock_id} is {dock.status.value}."}

        trailer = self._trailers[trailer_id]
        old_status = trailer.status.value
        trailer.status = TrailerStatus.AT_DOCK
        trailer.assigned_dock_id = dock_id
        dock.status = DockStatus.OCCUPIED
        dock.current_trailer_id = trailer_id
        dock.last_updated = time.time()

        move = YardMove(
            move_id=hashlib.md5(f"{trailer_id}{dock_id}{time.time()}".encode()).hexdigest()[:12],
            trailer_id=trailer_id,
            move_type=MoveType.YARD_TO_DOCK,
            from_location="yard",
            to_location=dock_id,
            jockey_id=jockey_id,
            completed_at=time.time(),
        )
        self.db.save_trailer(trailer)
        self.db.save_dock(dock)
        self.db.save_move(move)
        self.db.audit("trailer", trailer_id, "dock_assigned", old_status, "at_dock", jockey_id)
        return {"trailer_id": trailer_id, "dock_id": dock_id, "move_id": move.move_id}

    def release_dock(self, dock_id: str) -> None:
        if dock_id not in self._docks:
            return
        dock = self._docks[dock_id]
        if dock.current_trailer_id and dock.current_trailer_id in self._trailers:
            trailer = self._trailers[dock.current_trailer_id]
            trailer.status = TrailerStatus.IN_YARD
            trailer.assigned_dock_id = None
            self.db.save_trailer(trailer)
        dock.status = DockStatus.AVAILABLE
        dock.current_trailer_id = None
        dock.last_updated = time.time()
        self.db.save_dock(dock)

    def gate_out(self, trailer_id: str, gate_id: str = "GATE_2",
                  operator_id: str = "OP_001") -> Dict[str, Any]:
        if trailer_id not in self._trailers:
            return {"error": "Trailer not found."}
        trailer = self._trailers[trailer_id]
        if trailer.assigned_dock_id:
            self.release_dock(trailer.assigned_dock_id)
        trailer.status = TrailerStatus.DEPARTED
        trailer.gate_out_at = time.time()
        self.db.save_trailer(trailer)
        self.db.log_gate(trailer_id, "OUT", gate_id, operator_id)
        dwell = trailer.dwell_time_hours
        self.db.audit("trailer", trailer_id, "gate_out", "in_yard", "departed", operator_id)
        return {"trailer_id": trailer_id, "status": "departed",
                "dwell_hours": round(dwell, 2) if dwell else 0}

    def raise_exception(self, trailer_id: str, exc_type: ExceptionType,
                         description: str, severity: str = "medium") -> YardException:
        exc_id = hashlib.md5(f"{trailer_id}{exc_type.value}{time.time()}".encode()).hexdigest()[:12]
        exc = YardException(exc_id, trailer_id, exc_type, description, severity)
        self.db.save_exception(exc)
        if trailer_id in self._trailers:
            self._trailers[trailer_id].status = TrailerStatus.HELD
            self.db.save_trailer(self._trailers[trailer_id])
        return exc

    def check_dwell_violations(self) -> List[str]:
        violations = []
        for tid, trailer in self._trailers.items():
            dwell = trailer.dwell_time_hours
            if dwell and dwell > self.MAX_DWELL_HOURS and trailer.status != TrailerStatus.DEPARTED:
                violations.append(tid)
                self.raise_exception(
                    tid, ExceptionType.DWELL_TIME_EXCEEDED,
                    f"Trailer {tid} has been in yard for {dwell:.1f}h (max {self.MAX_DWELL_HOURS}h).",
                    severity="high",
                )
        return violations

    def yard_snapshot(self) -> Dict[str, Any]:
        total_docks = len(self._docks)
        available = sum(1 for d in self._docks.values() if d.status == DockStatus.AVAILABLE)
        occupied = sum(1 for d in self._docks.values() if d.status == DockStatus.OCCUPIED)
        active_trailers = sum(1 for t in self._trailers.values()
                               if t.status not in (TrailerStatus.DEPARTED,))
        in_queue = sum(1 for t in self._trailers.values()
                        if t.status == TrailerStatus.IN_QUEUE)
        at_dock = sum(1 for t in self._trailers.values()
                       if t.status == TrailerStatus.AT_DOCK)
        return {
            "total_docks": total_docks,
            "available_docks": available,
            "occupied_docks": occupied,
            "utilization_pct": round(occupied / total_docks * 100, 1) if total_docks else 0,
            "active_trailers": active_trailers,
            "in_queue": in_queue,
            "at_dock": at_dock,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    db = YardDatabase()
    ym = YardManager(db=db)

    for i in range(6):
        ym.register_dock(Dock(f"DOCK_{i+1:02d}", f"Dock {i+1}",
                               "loading" if i < 3 else "unloading"))

    print("Yard Management Demo\n")

    trailers = [
        Trailer("TRL_001", "TransCo India", "Rajesh K", "MH12AB1234", "inbound"),
        Trailer("TRL_002", "SpeedFreight", "Suresh M", "DL04CD5678", "outbound"),
        Trailer("TRL_003", "FastLine", "Vikram S", "KA03EF9012", "inbound"),
    ]

    for t in trailers:
        result = ym.gate_in(t)
        print(f"Gate IN: {result}")

    for t in trailers[:2]:
        dock = f"DOCK_{trailers.index(t)+1:02d}"
        result = ym.assign_dock(t.trailer_id, dock)
        print(f"Dock assigned: {result}")

    print(f"\nYard snapshot: {ym.yard_snapshot()}")

    exc = ym.raise_exception("TRL_003", ExceptionType.DOCUMENT_MISSING,
                              "Bill of lading not provided.")
    print(f"Exception raised: {exc.exception_id} ({exc.exception_type.value})")

    result = ym.gate_out("TRL_001")
    print(f"Gate OUT: {result}")

    print(f"\nFinal yard snapshot: {ym.yard_snapshot()}")
