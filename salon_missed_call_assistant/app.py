from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
import streamlit as st


APP_TITLE = "Salon Missed-Call Assistant"
DB_PATH = Path(__file__).with_name("salon_assistant.sqlite3")
SALON_NAME = os.getenv("SALON_NAME", "Luxe Chair Salon")
SALON_PHONE = os.getenv("SALON_PHONE", "(555) 014-2233")
SALON_TIMEZONE = os.getenv("SALON_TIMEZONE", "America/New_York")
SALON_STAFF_PASSCODE = os.getenv("SALON_STAFF_PASSCODE", "")
WEBHOOK_SECRET = os.getenv("SALON_WEBHOOK_SECRET", "")
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "Not configured")
BOOKING_PROVIDER = os.getenv("BOOKING_PROVIDER", "Not configured")
HOSTED_DATABASE_URL = os.getenv("SALON_DATABASE_URL") or os.getenv("SUPABASE_URL") or ""
DEFAULT_REPLY = (
    "Hi {client_name}, sorry we missed your call at {salon_name}. How can we help today? "
    "You can reply with a service, ask for prices, or tell us when you would like to book. "
    "Reply STOP to opt out or HELP for help."
)
BUSINESS_HOURS = {
    0: (time(9, 0), time(18, 0)),
    1: (time(9, 0), time(18, 0)),
    2: (time(9, 0), time(18, 0)),
    3: (time(9, 0), time(20, 0)),
    4: (time(9, 0), time(18, 0)),
    5: (time(10, 0), time(16, 0)),
}
REQUEST_KEYWORDS = {
    "price": ["price", "prices", "cost", "how much", "quote", "rates"],
    "book": ["book", "appointment", "schedule", "available", "availability", "open", "today", "tomorrow"],
    "cancel": ["cancel", "reschedule", "move my appointment", "change my appointment"],
    "stylist": ["stylist", "with", "same person", "favorite", "braider", "colorist"],
}
STOP_KEYWORDS = {"stop", "unsubscribe", "cancel texts", "end"}
HELP_KEYWORDS = {"help", "info", "support"}
CONSENT_STATUSES = ["Unknown", "Transactional okay", "Opted in", "Opted out"]
STAFF_ROLES = ["Owner", "Front desk", "Stylist", "Admin"]
PRODUCTION_REQUIREMENTS = [
    (
        "Phone webhook",
        "Connect the salon phone system so missed calls create conversations automatically.",
        "External setup",
    ),
    (
        "SMS consent and opt-out policy",
        "Confirm when the salon is allowed to text missed callers and support STOP/HELP language.",
        "Owner/legal review",
    ),
    (
        "Real service menu",
        "Replace demo services with the salon's exact services, durations, deposits, add-ons, and price rules.",
        "Salon data",
    ),
    (
        "Staff login",
        "Add stylist/front-desk accounts so only the right people see client messages and bookings.",
        "App upgrade",
    ),
    (
        "Hosted database",
        "Move from local SQLite to Supabase, Postgres, or the salon's existing booking database.",
        "App upgrade",
    ),
    (
        "Calendar integration",
        "Sync bookings with Google Calendar, Square, Fresha, Vagaro, GlossGenius, or the salon's system.",
        "Integration",
    ),
    (
        "Reminder automation",
        "Send confirmation, reminder, deposit, cancellation, and rebooking messages.",
        "App upgrade",
    ),
]
SERVICE_EXTRA_COLUMNS = {
    "deposit_required": "INTEGER DEFAULT 0",
    "deposit_amount": "REAL DEFAULT 0",
    "cancellation_window_hours": "INTEGER DEFAULT 24",
    "requires_consultation": "INTEGER DEFAULT 0",
    "prep_notes": "TEXT DEFAULT ''",
}
CLIENT_EXTRA_COLUMNS = {
    "consent_status": "TEXT DEFAULT 'Unknown'",
    "consent_source": "TEXT DEFAULT ''",
    "consent_updated_at": "TEXT DEFAULT ''",
    "opt_out_at": "TEXT DEFAULT ''",
}
APPOINTMENT_EXTRA_COLUMNS = {
    "deposit_status": "TEXT DEFAULT 'Not required'",
    "deposit_amount": "REAL DEFAULT 0",
    "payment_link": "TEXT DEFAULT ''",
    "calendar_sync_status": "TEXT DEFAULT 'Not synced'",
    "calendar_event_ref": "TEXT DEFAULT ''",
    "cancellation_deadline": "TEXT DEFAULT ''",
}
SERVICE_SYNONYMS = {
    "silk press": ["silk", "press", "straighten"],
    "cut and style": ["cut", "trim", "haircut", "style"],
    "root touch-up": ["root", "touch up", "touch-up", "gray", "grey"],
    "full color": ["color", "dye", "single process"],
    "balayage": ["balayage", "highlights", "lighten"],
    "box braids": ["box braid", "box braids", "braids", "braid"],
    "knotless braids": ["knotless"],
    "loc maintenance": ["loc", "locs", "retwist", "dread"],
    "deep conditioning": ["deep condition", "conditioning", "treatment", "hydration"],
    "wash and blowout": ["wash", "blowout", "blow dry", "shampoo"],
}


@dataclass(frozen=True)
class ServiceMatch:
    id: int
    name: str
    category: str
    duration_minutes: int
    base_price: float
    price_notes: str
    score: int


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column, definition in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                base_price REAL NOT NULL,
                price_notes TEXT DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stylists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                specialties TEXT NOT NULL,
                phone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                active INTEGER DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_intent TEXT DEFAULT '',
                last_message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                sender TEXT NOT NULL,
                body TEXT NOT NULL,
                channel TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'simulated',
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                stylist_id INTEGER NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                status TEXT NOT NULL,
                client_request TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id),
                FOREIGN KEY(service_id) REFERENCES services(id),
                FOREIGN KEY(stylist_id) REFERENCES stylists(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stylist_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stylist_id INTEGER NOT NULL,
                appointment_id INTEGER,
                client_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(stylist_id) REFERENCES stylists(id),
                FOREIGN KEY(appointment_id) REFERENCES appointments(id),
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                phone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS consent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                event_type TEXT NOT NULL,
                phone TEXT NOT NULL,
                client_name TEXT DEFAULT '',
                payload TEXT NOT NULL,
                signature_status TEXT NOT NULL,
                conversation_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                payment_link TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(appointment_id) REFERENCES appointments(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                external_ref TEXT DEFAULT '',
                details TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(appointment_id) REFERENCES appointments(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS appointment_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER NOT NULL,
                reminder_type TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(appointment_id) REFERENCES appointments(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                details TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_columns(connection, "services", SERVICE_EXTRA_COLUMNS)
        ensure_columns(connection, "clients", CLIENT_EXTRA_COLUMNS)
        ensure_columns(connection, "appointments", APPOINTMENT_EXTRA_COLUMNS)
        seed_defaults(connection)


def seed_defaults(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
        services = [
            ("Silk press", "Styling", 90, 85, "Starting price. Add trim or treatment after consultation."),
            ("Cut and style", "Cut", 60, 65, "Includes consultation, shampoo, cut, and finish."),
            ("Root touch-up", "Color", 90, 95, "Starting price for standard root coverage."),
            ("Full color", "Color", 150, 145, "Price may increase for long/thick hair or color correction."),
            ("Balayage", "Color", 210, 235, "Consultation recommended before final quote."),
            ("Box braids", "Protective style", 300, 220, "Starting price. Final quote depends on length and size."),
            ("Knotless braids", "Protective style", 330, 260, "Starting price. Hair not included unless noted."),
            ("Loc maintenance", "Locs", 120, 95, "Includes wash and retwist. Style add-on may vary."),
            ("Deep conditioning", "Treatment", 30, 35, "Can be added to most services."),
            ("Wash and blowout", "Styling", 60, 55, "Starting price for shampoo and blow dry."),
        ]
        connection.executemany(
            """
            INSERT INTO services (name, category, duration_minutes, base_price, price_notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            services,
        )
    if connection.execute("SELECT COUNT(*) FROM stylists").fetchone()[0] == 0:
        stylists = [
            ("Maya", "Cuts, silk press, treatments", "(555) 013-1001", "maya@example.com", 1),
            ("Janelle", "Color, balayage, root touch-up", "(555) 013-1002", "janelle@example.com", 1),
            ("Tasha", "Braids, locs, protective styles", "(555) 013-1003", "tasha@example.com", 1),
        ]
        connection.executemany(
            """
            INSERT INTO stylists (name, specialties, phone, email, active)
            VALUES (?, ?, ?, ?, ?)
            """,
            stylists,
        )
    if connection.execute("SELECT COUNT(*) FROM staff_users").fetchone()[0] == 0:
        connection.executemany(
            """
            INSERT INTO staff_users (name, role, phone, email, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("Salon owner", "Owner", "", "owner@example.com", 1, now_iso()),
                ("Front desk", "Front desk", "", "frontdesk@example.com", 1, now_iso()),
            ],
        )
    apply_default_service_policies(connection)


def apply_default_service_policies(connection: sqlite3.Connection) -> None:
    policies = {
        "Silk press": (0, 0, 24, 0, "Arrive with hair detangled when possible."),
        "Cut and style": (0, 0, 24, 0, "Bring inspiration photos if changing shape."),
        "Root touch-up": (0, 0, 48, 0, "Share formula history if you are new to the salon."),
        "Full color": (1, 35, 48, 1, "Consultation recommended for major color changes."),
        "Balayage": (1, 50, 72, 1, "Consultation and strand history recommended before final quote."),
        "Box braids": (1, 50, 72, 0, "Hair length, size, and hair-included options change final price."),
        "Knotless braids": (1, 60, 72, 0, "Hair length, size, and hair-included options change final price."),
        "Loc maintenance": (0, 0, 24, 0, "Style add-ons may change timing and price."),
        "Deep conditioning": (0, 0, 12, 0, "Usually booked as an add-on."),
        "Wash and blowout": (0, 0, 24, 0, "Add trim or treatment if needed."),
    }
    for service_name, values in policies.items():
        connection.execute(
            """
            UPDATE services
            SET deposit_required = CASE WHEN deposit_required = 0 THEN ? ELSE deposit_required END,
                deposit_amount = CASE WHEN deposit_amount = 0 THEN ? ELSE deposit_amount END,
                cancellation_window_hours = CASE WHEN cancellation_window_hours = 24 THEN ? ELSE cancellation_window_hours END,
                requires_consultation = CASE WHEN requires_consultation = 0 THEN ? ELSE requires_consultation END,
                prep_notes = CASE WHEN prep_notes = '' THEN ? ELSE prep_notes END
            WHERE name = ?
            """,
            values + (service_name,),
        )


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def load_df(query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    with connect() as connection:
        return pd.read_sql_query(query, connection, params=list(params))


def execute(query: str, params: Iterable[object] = ()) -> int:
    with connect() as connection:
        cursor = connection.execute(query, tuple(params))
        connection.commit()
        return int(cursor.lastrowid)


def record_audit(action: str, entity_type: str, entity_id: object, details: str = "") -> None:
    try:
        actor = st.session_state.get("staff_name", "System")
    except Exception:
        actor = "System"
    execute(
        """
        INSERT INTO audit_events (actor, action, entity_type, entity_id, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor, action, entity_type, str(entity_id), details, now_iso()),
    )


def log_consent_event(client_id: int, event_type: str, source: str, notes: str = "") -> None:
    timestamp = now_iso()
    status = {
        "opt_in": "Opted in",
        "transactional_okay": "Transactional okay",
        "opt_out": "Opted out",
        "unknown": "Unknown",
    }.get(event_type, event_type)
    opt_out_at = timestamp if status == "Opted out" else ""
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO consent_events (client_id, event_type, source, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (client_id, event_type, source, notes, timestamp),
        )
        connection.execute(
            """
            UPDATE clients
            SET consent_status = ?, consent_source = ?, consent_updated_at = ?, opt_out_at = ?
            WHERE id = ?
            """,
            (status, source, timestamp, opt_out_at, client_id),
        )
        connection.commit()


def client_by_conversation(conversation_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(
            """
            SELECT clients.*
            FROM conversations
            JOIN clients ON clients.id = conversations.client_id
            WHERE conversations.id = ?
            """,
            (conversation_id,),
        ).fetchone()


def texting_allowed(client_id: int, message_type: str = "transactional") -> bool:
    with connect() as connection:
        row = connection.execute("SELECT consent_status FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row:
        return False
    status = str(row["consent_status"] or "Unknown")
    if status == "Opted out":
        return False
    if status == "Opted in":
        return True
    return message_type == "transactional" and status in {"Unknown", "Transactional okay"}


def handle_stop_help_reply(conversation_id: int, message: str) -> str | None:
    text = message.strip().lower()
    client = client_by_conversation(conversation_id)
    if not client:
        return None
    if text in STOP_KEYWORDS:
        log_consent_event(int(client["id"]), "opt_out", "client_sms", "Client sent STOP-style keyword.")
        execute(
            "UPDATE conversations SET status = 'Opted out', last_intent = 'opt_out', updated_at = ? WHERE id = ?",
            (now_iso(), conversation_id),
        )
        reply = f"You have been opted out of texts from {SALON_NAME}. Call {SALON_PHONE} if you need help."
        add_assistant_message(conversation_id, reply, message_type="compliance")
        return "opt_out"
    if text in HELP_KEYWORDS:
        execute(
            "UPDATE conversations SET last_intent = 'help', updated_at = ? WHERE id = ?",
            (now_iso(), conversation_id),
        )
        reply = f"{SALON_NAME}: call {SALON_PHONE} for help. Reply STOP to opt out of texts."
        add_assistant_message(conversation_id, reply, message_type="compliance")
        return "help"
    return None


def get_or_create_client(name: str, phone: str) -> int:
    phone = normalize_phone(phone)
    with connect() as connection:
        row = connection.execute("SELECT id FROM clients WHERE phone = ?", (phone,)).fetchone()
        if row:
            connection.execute(
                "UPDATE clients SET name = CASE WHEN name = '' THEN ? ELSE name END WHERE id = ?",
                (name.strip(), row["id"]),
            )
            return int(row["id"])
        cursor = connection.execute(
            """
            INSERT INTO clients (name, phone, notes, created_at)
            VALUES (?, ?, '', ?)
            """,
            (name.strip() or "New client", phone, now_iso()),
        )
        connection.commit()
        return int(cursor.lastrowid)


def create_missed_call(name: str, phone: str, consent_basis: str = "transactional_missed_call") -> int:
    client_id = get_or_create_client(name, phone)
    timestamp = now_iso()
    if consent_basis == "opted_in":
        log_consent_event(client_id, "opt_in", "missed_call_form", "Staff marked client as opted in.")
    elif consent_basis == "transactional_missed_call":
        log_consent_event(
            client_id,
            "transactional_okay",
            "missed_call_form",
            "Client called the salon; first response is treated as transactional in this demo.",
        )
    with connect() as connection:
        status = "Consent review" if consent_basis == "unknown_manual_review" else "Waiting for client"
        cursor = connection.execute(
            """
            INSERT INTO conversations (client_id, status, last_intent, last_message, created_at, updated_at)
            VALUES (?, ?, '', 'Missed call', ?, ?)
            """,
            (client_id, status, timestamp, timestamp),
        )
        conversation_id = int(cursor.lastrowid)
        reply = DEFAULT_REPLY.format(client_name=name.strip() or "there", salon_name=SALON_NAME)
        delivery_status = "blocked: consent review" if consent_basis == "unknown_manual_review" else sms_status_for(reply)
        connection.execute(
            """
            INSERT INTO messages (conversation_id, sender, body, channel, created_at, delivery_status)
            VALUES (?, 'Salon assistant', ?, 'sms', ?, ?)
            """,
            (conversation_id, reply, timestamp, delivery_status),
        )
        connection.commit()
    record_audit("missed_call_created", "conversation", conversation_id, f"Consent basis: {consent_basis}")
    return conversation_id


def add_client_reply(conversation_id: int, message: str) -> str:
    intent = detect_intent(message)
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO messages (conversation_id, sender, body, channel, created_at, delivery_status)
            VALUES (?, 'Client', ?, 'sms', ?, 'received')
            """,
            (conversation_id, message.strip(), timestamp),
        )
        connection.execute(
            """
            UPDATE conversations
            SET status = 'Needs booking review', last_intent = ?, last_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (intent, message.strip(), timestamp, conversation_id),
        )
        connection.commit()
    compliance_intent = handle_stop_help_reply(conversation_id, message)
    if compliance_intent:
        return compliance_intent
    return intent


def add_assistant_message(conversation_id: int, message: str, message_type: str = "transactional") -> None:
    timestamp = now_iso()
    client = client_by_conversation(conversation_id)
    if client and not texting_allowed(int(client["id"]), message_type):
        delivery_status = "blocked: opted out"
    else:
        delivery_status = sms_status_for(message)
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO messages (conversation_id, sender, body, channel, created_at, delivery_status)
            VALUES (?, 'Salon assistant', ?, 'sms', ?, ?)
            """,
            (conversation_id, message.strip(), timestamp, delivery_status),
        )
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (timestamp, conversation_id),
        )
        connection.commit()


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone.strip()


def detect_intent(message: str) -> str:
    text = message.lower()
    scores = {
        intent: sum(1 for keyword in keywords if keyword in text)
        for intent, keywords in REQUEST_KEYWORDS.items()
    }
    if scores.get("cancel", 0):
        return "reschedule/cancel"
    if scores.get("book", 0) and scores.get("price", 0):
        return "book and price"
    if scores.get("book", 0):
        return "book appointment"
    if scores.get("price", 0):
        return "price check"
    if any(token in text for aliases in SERVICE_SYNONYMS.values() for token in aliases):
        return "service question"
    return "general question"


def match_services(message: str, limit: int = 4) -> list[ServiceMatch]:
    services = load_df("SELECT * FROM services ORDER BY category, name")
    text = message.lower()
    matches: list[ServiceMatch] = []
    for row in services.to_dict("records"):
        service_name = str(row["name"]).lower()
        aliases = SERVICE_SYNONYMS.get(service_name, [service_name])
        category = str(row["category"]).lower()
        score = 0
        if service_name in text:
            score += 8
        for alias in aliases:
            if alias in text:
                score += 4
            else:
                score += len(set(alias.split()) & set(text.split()))
        if category in text:
            score += 2
        if score > 0:
            matches.append(
                ServiceMatch(
                    id=int(row["id"]),
                    name=str(row["name"]),
                    category=str(row["category"]),
                    duration_minutes=int(row["duration_minutes"]),
                    base_price=float(row["base_price"]),
                    price_notes=str(row["price_notes"] or ""),
                    score=score,
                )
            )
    return sorted(matches, key=lambda item: (-item.score, item.base_price))[:limit]


def quote_for_matches(matches: list[ServiceMatch]) -> str:
    if not matches:
        return "I can help with prices. Which service are you interested in?"
    lines = ["Here are the starting prices I found:"]
    for match in matches:
        lines.append(
            f"- {match.name}: ${match.base_price:,.0f}+ ({match.duration_minutes} min). {match.price_notes}"
        )
    lines.append("Would you like me to help find an appointment time?")
    return "\n".join(lines)


def active_stylists() -> pd.DataFrame:
    return load_df("SELECT * FROM stylists WHERE active = 1 ORDER BY name")


def conversations() -> pd.DataFrame:
    return load_df(
        """
        SELECT
            c.id,
            clients.name AS client,
            clients.phone,
            clients.consent_status,
            c.status,
            c.last_intent,
            c.last_message,
            c.created_at,
            c.updated_at
        FROM conversations c
        JOIN clients ON clients.id = c.client_id
        ORDER BY c.updated_at DESC
        """
    )


def conversation_messages(conversation_id: int) -> pd.DataFrame:
    return load_df(
        """
        SELECT sender, body, channel, delivery_status, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id
        """,
        (conversation_id,),
    )


def selected_conversation(conversation_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(
            """
            SELECT c.*, clients.name AS client_name, clients.phone
            FROM conversations c
            JOIN clients ON clients.id = c.client_id
            WHERE c.id = ?
            """,
            (conversation_id,),
        ).fetchone()


def service_by_id(service_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()


def stylist_by_id(stylist_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute("SELECT * FROM stylists WHERE id = ?", (stylist_id,)).fetchone()


def parse_display_time(value: str) -> time | None:
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def booked_intervals(stylist_id: int, target_date: date) -> list[tuple[datetime, datetime]]:
    df = load_df(
        """
        SELECT appointments.appointment_time, services.duration_minutes
        FROM appointments
        JOIN services ON services.id = appointments.service_id
        WHERE stylist_id = ? AND appointment_date = ? AND status != 'Cancelled'
        """,
        (stylist_id, target_date.isoformat()),
    )
    intervals: list[tuple[datetime, datetime]] = []
    for row in df.to_dict("records"):
        start_time = parse_display_time(str(row["appointment_time"]))
        if not start_time:
            continue
        start_at = datetime.combine(target_date, start_time)
        end_at = start_at + timedelta(minutes=int(row["duration_minutes"]))
        intervals.append((start_at, end_at))
    return intervals


def intervals_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and end_a > start_b


def cancellation_deadline_for(appointment_date: date, appointment_time: str, hours: int) -> str:
    parsed = parse_display_time(appointment_time)
    if not parsed:
        return ""
    return (datetime.combine(appointment_date, parsed) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def service_deposit_status(service: sqlite3.Row | None) -> tuple[str, float]:
    if not service:
        return "Not required", 0.0
    amount = float(service["deposit_amount"] or 0)
    if int(service["deposit_required"] or 0) and amount > 0:
        return "Pending", amount
    return "Not required", 0.0


def build_payment_link(appointment_id: int, amount: float) -> str:
    if amount <= 0:
        return ""
    base = os.getenv("PAYMENT_CHECKOUT_BASE_URL", "").strip()
    if base:
        return f"{base.rstrip('/')}/appointment-{appointment_id}"
    return f"demo://deposit/appointment-{appointment_id}"


def create_payment_request(appointment_id: int, amount: float) -> str:
    if amount <= 0:
        return ""
    link = build_payment_link(appointment_id, amount)
    execute(
        """
        INSERT INTO payment_requests (appointment_id, provider, amount, status, payment_link, created_at)
        VALUES (?, ?, ?, 'Pending', ?, ?)
        """,
        (appointment_id, PAYMENT_PROVIDER, amount, link, now_iso()),
    )
    return link


def appointment_detail(appointment_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(
            """
            SELECT
                a.*,
                clients.name AS client_name,
                clients.phone AS client_phone,
                services.name AS service_name,
                services.duration_minutes,
                services.base_price,
                services.prep_notes,
                stylists.name AS stylist_name
            FROM appointments a
            JOIN clients ON clients.id = a.client_id
            JOIN services ON services.id = a.service_id
            JOIN stylists ON stylists.id = a.stylist_id
            WHERE a.id = ?
            """,
            (appointment_id,),
        ).fetchone()


def create_appointment_reminders(appointment_id: int) -> None:
    detail = appointment_detail(appointment_id)
    if not detail:
        return
    start_time = parse_display_time(str(detail["appointment_time"]))
    if not start_time:
        return
    appointment_at = datetime.combine(date.fromisoformat(str(detail["appointment_date"])), start_time)
    reminder_specs = [
        ("Confirmation", datetime.now() + timedelta(minutes=5)),
        ("24-hour reminder", appointment_at - timedelta(hours=24)),
    ]
    with connect() as connection:
        existing = connection.execute(
            "SELECT COUNT(*) FROM appointment_reminders WHERE appointment_id = ?",
            (appointment_id,),
        ).fetchone()[0]
        if existing:
            return
        for reminder_type, scheduled_for in reminder_specs:
            message = (
                f"{SALON_NAME}: reminder for {detail['service_name']} with {detail['stylist_name']} "
                f"on {detail['appointment_date']} at {detail['appointment_time']}. Reply STOP to opt out."
            )
            connection.execute(
                """
                INSERT INTO appointment_reminders (
                    appointment_id, reminder_type, scheduled_for, status, message, created_at
                )
                VALUES (?, ?, ?, 'Queued', ?, ?)
                """,
                (appointment_id, reminder_type, scheduled_for.replace(microsecond=0).isoformat(), message, now_iso()),
            )
        connection.commit()


def sync_appointment_to_calendar(appointment_id: int) -> tuple[str, str]:
    detail = appointment_detail(appointment_id)
    if not detail:
        return "Error", "Appointment not found"
    provider = BOOKING_PROVIDER
    external_ref = f"local-{appointment_id}"
    status = "Ready for external sync" if provider != "Not configured" else "ICS ready"
    details = "Calendar provider is configured." if provider != "Not configured" else "No calendar provider configured; use the ICS export."
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO calendar_sync_events (appointment_id, provider, status, external_ref, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (appointment_id, provider, status, external_ref, details, now_iso()),
        )
        connection.execute(
            """
            UPDATE appointments
            SET calendar_sync_status = ?, calendar_event_ref = ?
            WHERE id = ?
            """,
            (status, external_ref, appointment_id),
        )
        connection.commit()
    return status, details


def build_ics(detail: sqlite3.Row) -> str:
    start_time = parse_display_time(str(detail["appointment_time"])) or time(9, 0)
    start_at = datetime.combine(date.fromisoformat(str(detail["appointment_date"])), start_time)
    end_at = start_at + timedelta(minutes=int(detail["duration_minutes"]))
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    start_text = start_at.strftime("%Y%m%dT%H%M%S")
    end_text = end_at.strftime("%Y%m%dT%H%M%S")
    summary = f"{detail['service_name']} - {detail['client_name']}"
    description = (
        f"Client: {detail['client_name']} {detail['client_phone']}\\n"
        f"Stylist: {detail['stylist_name']}\\n"
        f"Request: {detail['client_request'] or ''}\\n"
        f"Deposit: {detail['deposit_status']} ${float(detail['deposit_amount'] or 0):,.0f}"
    )
    return "\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Salon Missed Call Assistant//EN",
            "BEGIN:VEVENT",
            f"UID:salon-appointment-{detail['id']}@local",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start_text}",
            f"DTEND:{end_text}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )


def available_slots(stylist_id: int, target_date: date, duration_minutes: int) -> list[str]:
    hours = BUSINESS_HOURS.get(target_date.weekday())
    if not hours:
        return []
    start, end = hours
    blocked = booked_intervals(stylist_id, target_date)
    cursor = datetime.combine(target_date, start)
    close = datetime.combine(target_date, end)
    slots: list[str] = []
    while cursor + timedelta(minutes=duration_minutes) <= close:
        slot = cursor.strftime("%I:%M %p").lstrip("0")
        slot_end = cursor + timedelta(minutes=duration_minutes)
        if not any(intervals_overlap(cursor, slot_end, booked_start, booked_end) for booked_start, booked_end in blocked):
            slots.append(slot)
        cursor += timedelta(minutes=30)
    return slots


def create_appointment(
    conversation_id: int,
    service_id: int,
    stylist_id: int,
    appointment_date: date,
    appointment_time: str,
    client_request: str,
) -> int:
    conversation = selected_conversation(conversation_id)
    if not conversation:
        raise ValueError("Conversation not found")
    service = service_by_id(service_id)
    deposit_status, deposit_amount = service_deposit_status(service)
    cancellation_hours = int(service["cancellation_window_hours"] or 24) if service else 24
    cancellation_deadline = cancellation_deadline_for(appointment_date, appointment_time, cancellation_hours)
    timestamp = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO appointments (
                client_id, service_id, stylist_id, appointment_date, appointment_time,
                status, client_request, created_at, deposit_status, deposit_amount, cancellation_deadline
            )
            VALUES (?, ?, ?, ?, ?, 'Booked', ?, ?, ?, ?, ?)
            """,
            (
                int(conversation["client_id"]),
                service_id,
                stylist_id,
                appointment_date.isoformat(),
                appointment_time,
                client_request,
                timestamp,
                deposit_status,
                deposit_amount,
                cancellation_deadline,
            ),
        )
        appointment_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE conversations SET status = 'Booked', updated_at = ? WHERE id = ?",
            (timestamp, conversation_id),
        )
        connection.commit()
    payment_link = create_payment_request(appointment_id, deposit_amount)
    if payment_link:
        execute("UPDATE appointments SET payment_link = ? WHERE id = ?", (payment_link, appointment_id))
    create_stylist_notification(appointment_id)
    create_appointment_reminders(appointment_id)
    sync_appointment_to_calendar(appointment_id)
    record_audit("appointment_booked", "appointment", appointment_id, f"Deposit status: {deposit_status}")
    return appointment_id


def create_stylist_notification(appointment_id: int) -> None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                a.id,
                a.client_request,
                a.appointment_date,
                a.appointment_time,
                clients.id AS client_id,
                clients.name AS client_name,
                clients.phone,
                services.name AS service_name,
                services.base_price,
                services.prep_notes,
                stylists.id AS stylist_id,
                stylists.name AS stylist_name,
                a.deposit_status,
                a.deposit_amount,
                a.payment_link,
                a.cancellation_deadline
            FROM appointments a
            JOIN clients ON clients.id = a.client_id
            JOIN services ON services.id = a.service_id
            JOIN stylists ON stylists.id = a.stylist_id
            WHERE a.id = ?
            """,
            (appointment_id,),
        ).fetchone()
        if not row:
            return
        summary = (
            f"{row['client_name']} ({row['phone']}) booked {row['service_name']} "
            f"with {row['stylist_name']} on {row['appointment_date']} at {row['appointment_time']}. "
            f"Starting price: ${float(row['base_price']):,.0f}. "
            f"Deposit: {row['deposit_status']} ${float(row['deposit_amount'] or 0):,.0f}. "
            f"Cancellation deadline: {row['cancellation_deadline'] or 'Not set'}. "
            f"Prep notes: {row['prep_notes'] or 'None'}. "
            f"Client asked: {row['client_request'] or 'No extra note.'}"
        )
        connection.execute(
            """
            INSERT INTO stylist_notifications (
                stylist_id, appointment_id, client_id, summary, status, created_at
            )
            VALUES (?, ?, ?, ?, 'Ready to send', ?)
            """,
            (row["stylist_id"], appointment_id, row["client_id"], summary, now_iso()),
        )
        connection.commit()


def sms_status_for(message: str) -> str:
    if not message.strip():
        return "blank"
    return "ready for provider" if sms_provider_ready() else "simulated"


def send_sms_with_twilio(to_phone: str, body: str) -> tuple[bool, str]:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "")
    if not all((sid, token, from_number)):
        return False, "SMS provider is not configured. This message is simulated in the demo."
    response = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data={"From": from_number, "To": normalize_phone(to_phone), "Body": body},
        auth=(sid, token),
        timeout=12,
    )
    if 200 <= response.status_code < 300:
        return True, "SMS sent."
    return False, f"SMS provider returned {response.status_code}: {response.text[:240]}"


def verify_webhook_signature(payload: str, signature: str) -> str:
    if not WEBHOOK_SECRET:
        return "not configured"
    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return "verified" if hmac.compare_digest(expected, signature.strip()) else "failed"


def process_missed_call_webhook(payload: dict[str, object], signature: str = "") -> int:
    raw_payload = json.dumps(payload, sort_keys=True)
    signature_status = verify_webhook_signature(raw_payload, signature)
    phone = str(payload.get("phone") or payload.get("From") or payload.get("caller") or "").strip()
    client_name = str(payload.get("name") or payload.get("CallerName") or "New client").strip()
    provider = str(payload.get("provider") or "manual_webhook")
    if not phone:
        raise ValueError("Webhook payload must include a phone number.")
    conversation_id = create_missed_call(client_name, phone, consent_basis="transactional_missed_call")
    execute(
        """
        INSERT INTO webhook_events (
            provider, event_type, phone, client_name, payload, signature_status, conversation_id, created_at
        )
        VALUES (?, 'missed_call', ?, ?, ?, ?, ?, ?)
        """,
        (provider, normalize_phone(phone), client_name, raw_payload, signature_status, conversation_id, now_iso()),
    )
    record_audit("webhook_missed_call_processed", "conversation", conversation_id, signature_status)
    return conversation_id


def process_inbound_sms_webhook(payload: dict[str, object], signature: str = "") -> str:
    raw_payload = json.dumps(payload, sort_keys=True)
    signature_status = verify_webhook_signature(raw_payload, signature)
    phone = normalize_phone(str(payload.get("phone") or payload.get("From") or ""))
    body = str(payload.get("body") or payload.get("Body") or "")
    if not phone or not body:
        raise ValueError("Inbound SMS payload must include phone and body.")
    inbox = conversations()
    if inbox.empty or phone not in set(inbox["phone"].tolist()):
        conversation_id = create_missed_call("New client", phone, consent_basis="transactional_missed_call")
    else:
        conversation_id = int(inbox.loc[inbox["phone"] == phone, "id"].iloc[0])
    intent = add_client_reply(conversation_id, body)
    execute(
        """
        INSERT INTO webhook_events (
            provider, event_type, phone, client_name, payload, signature_status, conversation_id, created_at
        )
        VALUES (?, 'inbound_sms', ?, '', ?, ?, ?, ?)
        """,
        (str(payload.get("provider") or "manual_webhook"), phone, raw_payload, signature_status, conversation_id, now_iso()),
    )
    record_audit("webhook_sms_processed", "conversation", conversation_id, intent)
    return intent


def save_services(edited: pd.DataFrame) -> None:
    required = [
        "id",
        "name",
        "category",
        "duration_minutes",
        "base_price",
        "price_notes",
        "deposit_required",
        "deposit_amount",
        "cancellation_window_hours",
        "requires_consultation",
        "prep_notes",
    ]
    missing = [column for column in required if column not in edited.columns]
    if missing:
        st.error(f"Missing columns: {', '.join(missing)}")
        return
    with connect() as connection:
        for row in edited.to_dict("records"):
            service_id = row.get("id")
            if pd.isna(service_id):
                connection.execute(
                    """
                    INSERT INTO services (
                        name, category, duration_minutes, base_price, price_notes,
                        deposit_required, deposit_amount, cancellation_window_hours,
                        requires_consultation, prep_notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["name"]),
                        str(row["category"]),
                        int(row["duration_minutes"]),
                        float(row["base_price"]),
                        str(row.get("price_notes") or ""),
                        1 if bool(row.get("deposit_required", False)) else 0,
                        float(row.get("deposit_amount") or 0),
                        int(row.get("cancellation_window_hours") or 24),
                        1 if bool(row.get("requires_consultation", False)) else 0,
                        str(row.get("prep_notes") or ""),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE services
                    SET name = ?, category = ?, duration_minutes = ?, base_price = ?, price_notes = ?,
                        deposit_required = ?, deposit_amount = ?, cancellation_window_hours = ?,
                        requires_consultation = ?, prep_notes = ?
                    WHERE id = ?
                    """,
                    (
                        str(row["name"]),
                        str(row["category"]),
                        int(row["duration_minutes"]),
                        float(row["base_price"]),
                        str(row.get("price_notes") or ""),
                        1 if bool(row.get("deposit_required", False)) else 0,
                        float(row.get("deposit_amount") or 0),
                        int(row.get("cancellation_window_hours") or 24),
                        1 if bool(row.get("requires_consultation", False)) else 0,
                        str(row.get("prep_notes") or ""),
                        int(service_id),
                    ),
                )
        connection.commit()


def save_stylists(edited: pd.DataFrame) -> None:
    with connect() as connection:
        for row in edited.to_dict("records"):
            stylist_id = row.get("id")
            values = (
                str(row["name"]),
                str(row["specialties"]),
                str(row.get("phone") or ""),
                str(row.get("email") or ""),
                1 if bool(row.get("active", True)) else 0,
            )
            if pd.isna(stylist_id):
                connection.execute(
                    """
                    INSERT INTO stylists (name, specialties, phone, email, active)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    values,
                )
            else:
                connection.execute(
                    """
                    UPDATE stylists
                    SET name = ?, specialties = ?, phone = ?, email = ?, active = ?
                    WHERE id = ?
                    """,
                    values + (int(stylist_id),),
                )
        connection.commit()


def save_staff_users(edited: pd.DataFrame) -> None:
    with connect() as connection:
        for row in edited.to_dict("records"):
            staff_id = row.get("id")
            values = (
                str(row["name"]),
                str(row["role"]),
                str(row.get("phone") or ""),
                str(row.get("email") or ""),
                1 if bool(row.get("active", True)) else 0,
            )
            if pd.isna(staff_id):
                connection.execute(
                    """
                    INSERT INTO staff_users (name, role, phone, email, active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    values + (now_iso(),),
                )
            else:
                connection.execute(
                    """
                    UPDATE staff_users
                    SET name = ?, role = ?, phone = ?, email = ?, active = ?
                    WHERE id = ?
                    """,
                    values + (int(staff_id),),
                )
        connection.commit()


def sms_provider_ready() -> bool:
    return all(
        os.getenv(key)
        for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    )


def setup_readiness_items() -> list[tuple[str, bool, str]]:
    services = load_df("SELECT COUNT(*) AS count FROM services")
    stylists = load_df("SELECT COUNT(*) AS count FROM stylists WHERE active = 1")
    staff = load_df("SELECT COUNT(*) AS count FROM staff_users WHERE active = 1")
    return [
        (
            "Service menu",
            int(services.iloc[0]["count"]) > 0,
            "Demo services are loaded; replace them with the salon's real menu before launch.",
        ),
        (
            "Active stylists",
            int(stylists.iloc[0]["count"]) > 0,
            "At least one active stylist is available for booking.",
        ),
        (
            "SMS provider",
            sms_provider_ready(),
            "Twilio credentials are required before texts send outside the demo.",
        ),
        (
            "Staff access",
            bool(SALON_STAFF_PASSCODE) and int(staff.iloc[0]["count"]) > 0,
            "Set SALON_STAFF_PASSCODE and staff users before real client data is used.",
        ),
        (
            "Hosted database",
            bool(HOSTED_DATABASE_URL),
            "Set SALON_DATABASE_URL or SUPABASE_URL for production storage and backups.",
        ),
        (
            "Phone webhook",
            bool(WEBHOOK_SECRET),
            "Set SALON_WEBHOOK_SECRET and deploy the webhook receiver beside the Streamlit app.",
        ),
        (
            "Consent policy",
            bool(os.getenv("SALON_CONSENT_POLICY_APPROVED")),
            "Add opt-in, STOP, HELP, and message-frequency language before real texting.",
        ),
        (
            "Calendar sync",
            BOOKING_PROVIDER != "Not configured",
            "Set BOOKING_PROVIDER after choosing Google Calendar, Square, Fresha, or another system.",
        ),
        (
            "Payment links",
            PAYMENT_PROVIDER != "Not configured",
            "Set PAYMENT_PROVIDER and checkout base URL before collecting deposits.",
        ),
    ]


def status_badge(label: str, kind: str = "neutral") -> str:
    class_name = {
        "good": "status-good",
        "warn": "status-warn",
        "neutral": "status-neutral",
    }.get(kind, "status-neutral")
    return f'<span class="status-pill {class_name}">{escape(label)}</span>'


def require_staff_session() -> bool:
    if not SALON_STAFF_PASSCODE:
        st.session_state.setdefault("staff_authenticated", True)
        st.session_state.setdefault("staff_name", "Demo staff")
        st.session_state.setdefault("staff_role", "Owner")
        return True
    if st.session_state.get("staff_authenticated"):
        return True
    st.set_page_config(page_title=APP_TITLE, layout="centered")
    st.title("Staff access")
    st.write("Enter the staff passcode to open the salon assistant.")
    name = st.text_input("Staff name", value="Front desk")
    role = st.selectbox("Role", STAFF_ROLES, index=1)
    passcode = st.text_input("Passcode", type="password")
    if st.button("Sign in", type="primary", width="stretch"):
        if hmac.compare_digest(passcode, SALON_STAFF_PASSCODE):
            st.session_state["staff_authenticated"] = True
            st.session_state["staff_name"] = name.strip() or "Staff"
            st.session_state["staff_role"] = role
            st.rerun()
        st.error("Passcode did not match.")
    return False


def role_allows_admin() -> bool:
    return st.session_state.get("staff_role", "Owner") in {"Owner", "Admin"}


def action_for_conversation(intent: str, status: str) -> str:
    if status == "Booked":
        return "No action"
    if intent == "book and price":
        return "Confirm service, quote starting price, and offer top slots."
    if intent == "book appointment":
        return "Move to booking and pick a stylist/time."
    if intent == "price check":
        return "Send price menu and ask whether they want to book."
    if intent == "reschedule/cancel":
        return "Route to front desk for schedule change."
    if intent == "service question":
        return "Answer service details and suggest booking."
    return "Review client message."


def open_queue() -> pd.DataFrame:
    inbox = conversations()
    if inbox.empty:
        return pd.DataFrame(columns=["client", "phone", "intent", "status", "next_action", "updated_at"])
    queue = inbox[inbox["status"] != "Booked"].copy()
    if queue.empty:
        return pd.DataFrame(columns=["client", "phone", "intent", "status", "next_action", "updated_at"])
    queue["intent"] = queue["last_intent"].replace("", "Waiting")
    queue["next_action"] = queue.apply(
        lambda row: action_for_conversation(str(row["last_intent"]), str(row["status"])),
        axis=1,
    )
    return queue[["client", "phone", "intent", "status", "next_action", "updated_at"]]


def upcoming_appointments(limit: int = 6) -> pd.DataFrame:
    return load_df(
        """
        SELECT
            a.id,
            clients.name AS client,
            services.name AS service,
            stylists.name AS stylist,
            a.appointment_date,
            a.appointment_time,
            a.status
        FROM appointments a
        JOIN clients ON clients.id = a.client_id
        JOIN services ON services.id = a.service_id
        JOIN stylists ON stylists.id = a.stylist_id
        WHERE a.status != 'Cancelled'
        ORDER BY a.appointment_date, a.appointment_time
        LIMIT ?
        """,
        (limit,),
    )


def analytics_summary() -> dict[str, float]:
    conversations_df = conversations()
    appointments_df = load_df(
        """
        SELECT appointments.*, services.base_price
        FROM appointments
        JOIN services ON services.id = appointments.service_id
        WHERE appointments.status != 'Cancelled'
        """
    )
    messages_df = load_df("SELECT * FROM messages")
    missed_calls = len(conversations_df)
    bookings = len(appointments_df)
    replies = 0
    if not messages_df.empty:
        replies = int((messages_df["sender"] == "Client").sum())
    recovered_revenue = float(appointments_df["base_price"].sum()) if not appointments_df.empty else 0.0
    conversion = (bookings / missed_calls * 100) if missed_calls else 0.0
    response_rate = (replies / missed_calls * 100) if missed_calls else 0.0
    return {
        "missed_calls": missed_calls,
        "client_replies": replies,
        "bookings": bookings,
        "conversion": conversion,
        "response_rate": response_rate,
        "recovered_revenue": recovered_revenue,
    }


def render_header() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        """
        <style>
        :root {
            --salon-ink: #172026;
            --salon-muted: #5f6f72;
            --salon-accent: #006d77;
            --salon-accent-2: #c8553d;
            --salon-gold: #b88a44;
            --salon-soft: #edf6f9;
            --salon-warm: #fff4ed;
            --salon-line: #d7ded9;
            --salon-page: #f7f7f2;
        }
        .stApp {
            background: var(--salon-page);
        }
        .main .block-container {
            padding-top: 1rem;
            padding-bottom: 2.5rem;
            max-width: 1220px;
        }
        h1, h2, h3 {
            color: var(--salon-ink);
            letter-spacing: 0;
        }
        div[data-testid="stMetric"] {
            border: 1px solid var(--salon-line);
            border-radius: 8px;
            padding: 0.9rem 1rem;
            background: #ffffff;
        }
        .app-hero {
            border: 1px solid #14333a;
            border-radius: 8px;
            padding: 1.1rem 1.25rem;
            background: linear-gradient(135deg, #15282f 0%, #20454b 58%, #6b4d2e 100%);
            color: #ffffff;
            margin-bottom: 1rem;
        }
        .app-hero h1 {
            color: #ffffff;
            font-size: 2.05rem;
            line-height: 1.1;
            margin: 0.18rem 0 0.4rem;
        }
        .app-hero p {
            color: #eef7f6;
            margin: 0;
            max-width: 850px;
        }
        .hero-label {
            color: #f9d79a;
            font-size: 0.78rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }
        .workflow-strip {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 0.65rem;
            margin: 0.9rem 0 0;
        }
        .workflow-step {
            border: 1px solid rgba(255,255,255,0.28);
            border-radius: 8px;
            padding: 0.65rem 0.72rem;
            background: rgba(255,255,255,0.10);
            color: #ffffff;
            min-height: 72px;
        }
        .workflow-step strong {
            display: block;
            font-size: 0.9rem;
        }
        .workflow-step span {
            display: block;
            color: #dcebea;
            font-size: 0.78rem;
            margin-top: 0.18rem;
        }
        .assist-panel, .readiness-panel {
            border: 1px solid var(--salon-line);
            border-radius: 8px;
            padding: 1rem;
            background: #ffffff;
        }
        .readiness-panel {
            min-height: 132px;
        }
        .section-label {
            color: var(--salon-muted);
            font-size: 0.78rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            margin-bottom: 0.25rem;
        }
        .status-pill {
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: 0.18rem 0.58rem;
            font-size: 0.78rem;
            font-weight: 700;
            border: 1px solid transparent;
            white-space: nowrap;
        }
        .status-good {
            color: #064e3b;
            background: #d1fae5;
            border-color: #a7f3d0;
        }
        .status-warn {
            color: #7c2d12;
            background: #ffedd5;
            border-color: #fed7aa;
        }
        .status-neutral {
            color: #344054;
            background: #eef2f6;
            border-color: #d0d5dd;
        }
        .queue-row {
            display: grid;
            grid-template-columns: 1.1fr 1fr 1.4fr;
            gap: 0.75rem;
            align-items: center;
            border-bottom: 1px solid var(--salon-line);
            padding: 0.7rem 0;
        }
        .queue-row:last-child {
            border-bottom: 0;
        }
        .queue-title {
            font-weight: 700;
            color: var(--salon-ink);
        }
        .queue-note {
            color: var(--salon-muted);
            font-size: 0.86rem;
        }
        .sms-bubble {
            border-radius: 8px;
            padding: 0.72rem 0.82rem;
            margin: 0.38rem 0;
            max-width: 760px;
            line-height: 1.42;
            border: 1px solid var(--salon-line);
        }
        .sms-assistant {
            background: var(--salon-soft);
        }
        .sms-client {
            background: #fff8f0;
            margin-left: auto;
        }
        .small-muted {
            color: #667085;
            font-size: 0.86rem;
        }
        div[data-testid="stTabs"] button {
            font-weight: 700;
        }
        div[data-testid="stDataFrame"] {
            border-radius: 8px;
            overflow: hidden;
        }
        @media (max-width: 760px) {
            .main .block-container {
                padding-left: 0.85rem;
                padding-right: 0.85rem;
            }
            div[data-testid="stHorizontalBlock"] {
                gap: 0.4rem;
            }
            .workflow-strip, .queue-row {
                grid-template-columns: 1fr;
            }
            .app-hero h1 {
                font-size: 1.55rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    provider_ready = sms_provider_ready()
    provider_label = "SMS provider ready" if provider_ready else "Simulation mode"
    provider_class = "status-good" if provider_ready else "status-warn"
    st.markdown(
        f"""
        <section class="app-hero">
            <div class="hero-label">Front desk command center</div>
            <h1>{escape(SALON_NAME)} missed-call concierge</h1>
            <p>{escape(SALON_PHONE)} | Capture missed calls, answer price questions, book appointments, and brief stylists from one responsive workspace.</p>
            <div style="margin-top:0.72rem;">
                <span class="status-pill {provider_class}">{provider_label}</span>
            </div>
            <div class="workflow-strip">
                <div class="workflow-step"><strong>1. Missed call</strong><span>Client gets a fast text response.</span></div>
                <div class="workflow-step"><strong>2. Intent scan</strong><span>Request is sorted into price, booking, or support.</span></div>
                <div class="workflow-step"><strong>3. Book slot</strong><span>Service duration and stylist availability drive the calendar.</span></div>
                <div class="workflow-step"><strong>4. Staff handoff</strong><span>Stylist gets the client request and booking summary.</span></div>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    with st.sidebar:
        st.markdown("### Salon setup")
        st.caption(f"{SALON_NAME} | {SALON_PHONE}")
        st.markdown(status_badge("SMS live", "good" if provider_ready else "warn"), unsafe_allow_html=True)
        st.caption("SMS sends for real only after provider credentials and consent rules are configured.")
        st.divider()
        for label, ready, detail in setup_readiness_items():
            kind = "good" if ready else "warn"
            st.markdown(status_badge(label, kind), unsafe_allow_html=True)
            st.caption(detail)


def render_metrics() -> None:
    inbox = conversations()
    appointments = load_df("SELECT * FROM appointments WHERE status = 'Booked'")
    notifications = load_df("SELECT * FROM stylist_notifications WHERE status = 'Ready to send'")
    clients = load_df("SELECT * FROM clients")
    today_bookings = 0
    if not appointments.empty:
        today_bookings = int((appointments["appointment_date"] == date.today().isoformat()).sum())
    open_conversations = int((inbox["status"] != "Booked").sum()) if not inbox.empty else 0
    cols = st.columns(4)
    cols[0].metric("Needs attention", open_conversations)
    cols[1].metric("Booked today", today_bookings, delta=f"{len(appointments)} total")
    cols[2].metric("Staff updates", len(notifications))
    cols[3].metric("Client records", len(clients))


def render_overview_tab() -> None:
    st.subheader("Today at a Glance")
    left, right = st.columns([1.35, 0.85], gap="large")
    with left:
        st.markdown("#### Front desk queue")
        queue = open_queue()
        if queue.empty:
            st.success("No open client conversations need attention.")
        else:
            for row in queue.head(5).to_dict("records"):
                st.markdown(
                    f"""
                    <div class="queue-row">
                        <div>
                            <div class="queue-title">{escape(str(row['client']))}</div>
                            <div class="queue-note">{escape(str(row['phone']))}</div>
                        </div>
                        <div>
                            {status_badge(str(row['intent']).title(), "warn")}
                            <div class="queue-note">{escape(str(row['status']))}</div>
                        </div>
                        <div class="queue-note">{escape(str(row['next_action']))}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        st.markdown("#### Upcoming appointments")
        appointments = upcoming_appointments()
        if appointments.empty:
            st.info("No appointments booked yet.")
        else:
            st.dataframe(appointments, hide_index=True, width="stretch")

    with right:
        st.markdown("#### Launch readiness")
        ready_items = setup_readiness_items()
        ready_count = sum(1 for _, ready, _ in ready_items if ready)
        st.progress(ready_count / len(ready_items), text=f"{ready_count} of {len(ready_items)} setup items ready")
        for label, ready, detail in ready_items:
            st.markdown(status_badge(label, "good" if ready else "warn"), unsafe_allow_html=True)
            st.caption(detail)
        st.markdown("#### What is missing")
        missing = [
            "A real missed-call webhook from the salon phone provider.",
            "A confirmed texting consent and opt-out process.",
            "The salon's true service rules, deposits, add-ons, and cancellation policy.",
            "Staff accounts and permissions before storing real client data.",
        ]
        for item in missing:
            st.write(f"- {item}")


def render_missed_call_tab() -> None:
    st.subheader("Missed Call Capture")
    left, right = st.columns([0.95, 1.05], gap="large")
    with left:
        st.markdown("#### Simulate a missed salon call")
        name = st.text_input("Client name", value="Ari Johnson")
        phone = st.text_input("Client phone", value="404-555-0198")
        consent_basis = st.selectbox(
            "Texting basis",
            options=[
                "transactional_missed_call",
                "opted_in",
                "unknown_manual_review",
            ],
            format_func=lambda item: {
                "transactional_missed_call": "Transactional reply to missed call",
                "opted_in": "Client already opted in",
                "unknown_manual_review": "Do not assume consent",
            }[item],
        )
        if st.button("Create missed call and auto-text", type="primary", width="stretch"):
            conversation_id = create_missed_call(name, phone, consent_basis)
            st.session_state["active_conversation_id"] = conversation_id
            st.success("Missed call captured and text response prepared.")
            st.rerun()
        st.info(
            "Production version: this screen becomes a webhook receiver from the salon phone provider. "
            "The auto-text can be sent when SMS credentials and client consent rules are configured."
        )
    with right:
        st.markdown("#### Inbox")
        inbox = conversations()
        if inbox.empty:
            st.write("No missed calls yet.")
            return
        st.dataframe(
            inbox[["id", "client", "phone", "consent_status", "status", "last_intent", "updated_at"]],
            hide_index=True,
            width="stretch",
        )
        options = inbox["id"].tolist()
        default_index = 0
        if st.session_state.get("active_conversation_id") in options:
            default_index = options.index(st.session_state["active_conversation_id"])
        st.session_state["active_conversation_id"] = st.selectbox(
            "Active conversation",
            options=options,
            index=default_index,
            format_func=lambda item: f"#{item} | {inbox.loc[inbox['id'] == item, 'client'].iloc[0]}",
        )


def render_conversation_tab() -> None:
    st.subheader("Client Text Flow")
    conversation_id = st.session_state.get("active_conversation_id")
    if not conversation_id:
        st.warning("Create or select a missed call first.")
        return
    conversation = selected_conversation(int(conversation_id))
    if not conversation:
        st.warning("Conversation not found.")
        return
    top_left, top_right = st.columns([1, 1])
    top_left.markdown(f"**Client:** {conversation['client_name']}  ")
    top_left.markdown(f"**Phone:** {conversation['phone']}")
    top_right.markdown(f"**Status:** {conversation['status']}")
    top_right.markdown(f"**Detected intent:** {conversation['last_intent'] or 'Waiting'}")

    messages = conversation_messages(int(conversation_id))
    st.markdown("#### Message thread")
    for row in messages.to_dict("records"):
        bubble_class = "sms-assistant" if row["sender"] == "Salon assistant" else "sms-client"
        sender = escape(str(row["sender"]))
        body = escape(str(row["body"])).replace(chr(10), "<br>")
        created_at = escape(str(row["created_at"]))
        delivery_status = escape(str(row["delivery_status"]))
        st.markdown(
            f"""
            <div class="sms-bubble {bubble_class}">
                <strong>{sender}</strong><br>
                {body}
                <div class="small-muted">{created_at} | {delivery_status}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("#### Add a client reply")
    sample = st.selectbox(
        "Quick examples",
        [
            "How much are knotless braids and do you have Friday afternoon open?",
            "I need a silk press tomorrow. What is the price?",
            "Can I book a root touch-up with Janelle?",
            "I need to reschedule my appointment.",
            "Do you have prices for loc maintenance?",
        ],
    )
    reply = st.text_area("Client text", value=sample, height=90)
    if st.button("Process client reply", type="primary"):
        intent = add_client_reply(int(conversation_id), reply)
        matches = match_services(reply)
        if intent in {"price check", "book and price", "service question"}:
            add_assistant_message(int(conversation_id), quote_for_matches(matches))
        elif intent == "book appointment":
            add_assistant_message(
                int(conversation_id),
                "I can help book that. Which service would you like, and do you have a preferred stylist or time?",
            )
        elif intent == "reschedule/cancel":
            add_assistant_message(
                int(conversation_id),
                "I can help with that. Please send the appointment date/time you want to change.",
            )
        else:
            add_assistant_message(
                int(conversation_id),
                "Thanks. I sent your message to the salon team and can also help with prices or booking.",
            )
        st.success(f"Detected intent: {intent}")
        st.rerun()


def render_booking_tab() -> None:
    st.subheader("Price Lookup and Booking Calendar")
    conversation_id = st.session_state.get("active_conversation_id")
    if not conversation_id:
        st.warning("Create or select a conversation first.")
        return
    conversation = selected_conversation(int(conversation_id))
    if not conversation:
        st.warning("Conversation not found.")
        return

    left, right = st.columns([0.95, 1.05], gap="large")
    with left:
        request_text = st.text_area(
            "Client request to scan against services",
            value=conversation["last_message"] or "How much are knotless braids and do you have Friday open?",
            height=110,
        )
        matches = match_services(request_text)
        st.markdown("#### Matched services")
        if matches:
            match_df = pd.DataFrame(
                [
                    {
                        "id": item.id,
                        "service": item.name,
                        "category": item.category,
                        "minutes": item.duration_minutes,
                        "starting_price": f"${item.base_price:,.0f}+",
                        "notes": item.price_notes,
                    }
                    for item in matches
                ]
            )
            st.dataframe(match_df, hide_index=True, width="stretch")
        else:
            st.info("No exact service match yet. Choose from the full menu below.")

        services = load_df("SELECT * FROM services ORDER BY category, name")
        default_service_id = matches[0].id if matches else int(services.iloc[0]["id"])
        service_id = st.selectbox(
            "Service",
            options=services["id"].tolist(),
            index=services["id"].tolist().index(default_service_id),
            format_func=lambda item: services.loc[services["id"] == item, "name"].iloc[0],
        )
        selected_service = service_by_id(int(service_id))
        if selected_service:
            deposit_status, deposit_amount = service_deposit_status(selected_service)
            st.success(
                f"{selected_service['name']}: ${float(selected_service['base_price']):,.0f}+ | "
                f"{selected_service['duration_minutes']} minutes"
            )
            st.caption(selected_service["price_notes"])
            st.caption(
                f"Deposit: {deposit_status} ${deposit_amount:,.0f} | "
                f"Cancellation window: {int(selected_service['cancellation_window_hours'] or 24)} hours | "
                f"Consultation: {'Recommended' if int(selected_service['requires_consultation'] or 0) else 'Not required'}"
            )
            if selected_service["prep_notes"]:
                st.info(str(selected_service["prep_notes"]))

    with right:
        stylists = active_stylists()
        if stylists.empty:
            st.warning("Add an active stylist before booking.")
            return
        stylist_id = st.selectbox(
            "Stylist",
            options=stylists["id"].tolist(),
            format_func=lambda item: stylists.loc[stylists["id"] == item, "name"].iloc[0],
        )
        target_date = st.date_input("Appointment date", min_value=date.today(), value=date.today())
        duration = int(selected_service["duration_minutes"]) if selected_service else 60
        slots = available_slots(int(stylist_id), target_date, duration)
        if not slots:
            st.warning("No open slots for that stylist/date.")
            return
        appointment_time = st.selectbox("Open time", options=slots)
        if st.button("Book appointment and notify stylist", type="primary", width="stretch"):
            appointment_id = create_appointment(
                int(conversation_id),
                int(service_id),
                int(stylist_id),
                target_date,
                appointment_time,
                request_text,
            )
            add_assistant_message(
                int(conversation_id),
                f"You are booked for {selected_service['name']} on {target_date:%A, %B %d} at {appointment_time}. "
                f"Starting price is ${float(selected_service['base_price']):,.0f}+. "
                f"Deposit status: {service_deposit_status(selected_service)[0]}. Reply STOP to opt out.",
            )
            st.success(f"Appointment #{appointment_id} booked and stylist notification created.")
            st.rerun()

    st.markdown("#### Upcoming appointments")
    appointments = load_df(
        """
        SELECT
            a.id,
            clients.name AS client,
            clients.phone,
            services.name AS service,
            stylists.name AS stylist,
            a.appointment_date,
            a.appointment_time,
            a.status,
            a.deposit_status,
            a.deposit_amount,
            a.calendar_sync_status
        FROM appointments a
        JOIN clients ON clients.id = a.client_id
        JOIN services ON services.id = a.service_id
        JOIN stylists ON stylists.id = a.stylist_id
        ORDER BY a.appointment_date, a.appointment_time
        """
    )
    st.dataframe(appointments, hide_index=True, width="stretch")


def render_notifications_tab() -> None:
    st.subheader("Stylist Notifications")
    notifications = load_df(
        """
        SELECT
            n.id,
            stylists.name AS stylist,
            clients.name AS client,
            clients.phone,
            n.summary,
            n.status,
            n.created_at
        FROM stylist_notifications n
        JOIN stylists ON stylists.id = n.stylist_id
        JOIN clients ON clients.id = n.client_id
        ORDER BY n.created_at DESC
        """
    )
    if notifications.empty:
        st.info("No stylist notifications yet.")
        return
    st.dataframe(notifications, hide_index=True, width="stretch")
    notification_id = st.selectbox(
        "Notification to send/mark",
        options=notifications["id"].tolist(),
        format_func=lambda item: f"#{item} | {notifications.loc[notifications['id'] == item, 'stylist'].iloc[0]}",
    )
    row = notifications.loc[notifications["id"] == notification_id].iloc[0]
    st.text_area("Stylist message", value=row["summary"], height=120)
    col1, col2 = st.columns(2)
    if col1.button("Mark as sent", width="stretch"):
        execute("UPDATE stylist_notifications SET status = 'Sent' WHERE id = ?", (int(notification_id),))
        st.success("Notification marked as sent.")
        st.rerun()
    if col2.button("Simulate SMS to stylist", width="stretch"):
        stylist = load_df(
            """
            SELECT stylists.phone
            FROM stylist_notifications n
            JOIN stylists ON stylists.id = n.stylist_id
            WHERE n.id = ?
            """,
            (int(notification_id),),
        )
        to_phone = str(stylist.iloc[0]["phone"]) if not stylist.empty else ""
        ok, status = send_sms_with_twilio(to_phone, str(row["summary"]))
        execute(
            "UPDATE stylist_notifications SET status = ? WHERE id = ?",
            ("Sent" if ok else "Provider not configured", int(notification_id)),
        )
        st.info(status)
        st.rerun()


def render_admin_tab() -> None:
    st.subheader("Salon Database")
    if not role_allows_admin():
        st.warning("Only Owner/Admin roles can edit salon database settings.")
        return
    services = load_df("SELECT * FROM services ORDER BY id")
    stylists = load_df("SELECT * FROM stylists ORDER BY id")
    staff_users = load_df("SELECT * FROM staff_users ORDER BY id")
    st.markdown("#### Services and prices")
    edited_services = st.data_editor(
        services,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        disabled=["id"],
        column_config={
            "id": st.column_config.NumberColumn("id"),
            "duration_minutes": st.column_config.NumberColumn(min_value=15, step=15),
            "base_price": st.column_config.NumberColumn(min_value=0.0, step=5.0, format="$%.2f"),
            "deposit_required": st.column_config.CheckboxColumn(),
            "deposit_amount": st.column_config.NumberColumn(min_value=0.0, step=5.0, format="$%.2f"),
            "cancellation_window_hours": st.column_config.NumberColumn(min_value=0, step=12),
            "requires_consultation": st.column_config.CheckboxColumn(),
        },
    )
    if st.button("Save services"):
        save_services(edited_services)
        st.success("Services saved.")
        st.rerun()

    st.markdown("#### Stylists")
    edited_stylists = st.data_editor(
        stylists,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        disabled=["id"],
        column_config={
            "id": st.column_config.NumberColumn("id"),
            "active": st.column_config.CheckboxColumn(),
        },
    )
    if st.button("Save stylists"):
        save_stylists(edited_stylists)
        st.success("Stylists saved.")
        st.rerun()

    st.markdown("#### Staff access")
    edited_staff = st.data_editor(
        staff_users,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        disabled=["id", "created_at"],
        column_config={
            "id": st.column_config.NumberColumn("id"),
            "role": st.column_config.SelectboxColumn(options=STAFF_ROLES),
            "active": st.column_config.CheckboxColumn(),
        },
    )
    if st.button("Save staff users"):
        save_staff_users(edited_staff)
        st.success("Staff users saved.")
        st.rerun()

    st.markdown("#### What I can and cannot write")
    st.write(
        """
        I can write the app UI, database, booking rules, price lookup, stylist handoff,
        admin screens, deployment files, and SMS/provider integration code.
        """
    )
    st.write(
        """
        I cannot personally buy or verify a salon phone number, create your Twilio or phone-provider account,
        approve carrier texting registration, guarantee deliverability, import a real salon's private database
        without the data/schema, or make final legal/compliance decisions about texting consent.
        """
    )
    st.write(
        """
        The production version will need a missed-call webhook from the phone system, SMS credentials,
        a consent/compliance workflow, staff login permissions, and either this local database or a hosted database.
        """
    )


def render_launch_plan_tab() -> None:
    st.subheader("Production Launch Plan")
    st.write(
        "This is the gap list between the demo and a salon-ready product. "
        "The app can be coded around these pieces, but the salon owner still has to choose providers and approve policies."
    )
    rows = []
    for label, description, owner in PRODUCTION_REQUIREMENTS:
        if label in {"Phone webhook", "SMS consent and opt-out policy", "Real service menu"}:
            priority = "Launch blocker"
        elif label in {"Staff login", "Hosted database", "Calendar integration"}:
            priority = "Strongly recommended"
        else:
            priority = "Next upgrade"
        rows.append(
            {
                "item": label,
                "why_it_matters": description,
                "owner": owner,
                "priority": priority,
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.markdown("#### Best next improvements")
    improvements = pd.DataFrame(
        [
            {
                "upgrade": "Real phone/SMS integration",
                "impact": "Missed calls trigger automatically instead of being simulated.",
                "effort": "Medium",
            },
            {
                "upgrade": "Booking-system sync",
                "impact": "Prevents double-booking and keeps the salon's real calendar accurate.",
                "effort": "Medium to high",
            },
            {
                "upgrade": "Client consent records",
                "impact": "Tracks permission, STOP requests, and message history.",
                "effort": "Medium",
            },
            {
                "upgrade": "AI response drafting",
                "impact": "Can draft warmer replies for unusual questions while keeping staff approval.",
                "effort": "Medium",
            },
            {
                "upgrade": "Deposits and no-show policy",
                "impact": "Protects stylist time for long services like braids, color, and extensions.",
                "effort": "Medium",
            },
            {
                "upgrade": "Owner analytics",
                "impact": "Shows missed-call recovery rate, booking conversion, revenue saved, and response speed.",
                "effort": "Low to medium",
            },
        ]
    )
    st.dataframe(improvements, hide_index=True, width="stretch")


def render_consent_tab() -> None:
    st.subheader("Consent and Compliance")
    st.write("Track permission, STOP/HELP responses, and the audit trail before turning on real texting.")
    clients = load_df(
        """
        SELECT id, name, phone, consent_status, consent_source, consent_updated_at, opt_out_at
        FROM clients
        ORDER BY created_at DESC
        """
    )
    if clients.empty:
        st.info("No clients yet.")
        return
    st.dataframe(clients, hide_index=True, width="stretch")
    client_id = st.selectbox(
        "Client consent record",
        options=clients["id"].tolist(),
        format_func=lambda item: f"{clients.loc[clients['id'] == item, 'name'].iloc[0]} | {clients.loc[clients['id'] == item, 'phone'].iloc[0]}",
    )
    selected = clients.loc[clients["id"] == client_id].iloc[0]
    col1, col2, col3 = st.columns(3)
    if col1.button("Mark opted in", width="stretch"):
        log_consent_event(int(client_id), "opt_in", "staff_manual", "Staff confirmed permission to text.")
        record_audit("consent_opt_in", "client", client_id)
        st.success("Client marked opted in.")
        st.rerun()
    if col2.button("Mark transactional only", width="stretch"):
        log_consent_event(int(client_id), "transactional_okay", "staff_manual", "Transactional service replies only.")
        record_audit("consent_transactional", "client", client_id)
        st.success("Client marked transactional only.")
        st.rerun()
    if col3.button("Mark opted out", width="stretch"):
        log_consent_event(int(client_id), "opt_out", "staff_manual", "Staff manually opted client out.")
        record_audit("consent_opt_out", "client", client_id)
        st.success("Client marked opted out.")
        st.rerun()
    st.caption(f"Current status for {selected['name']}: {selected['consent_status']}")
    events = load_df(
        """
        SELECT event_type, source, notes, created_at
        FROM consent_events
        WHERE client_id = ?
        ORDER BY created_at DESC
        """,
        (int(client_id),),
    )
    st.markdown("#### Consent event history")
    st.dataframe(events, hide_index=True, width="stretch")


def render_integrations_tab() -> None:
    st.subheader("Integrations")
    st.write("Use this screen to prepare the external pieces without hiding which provider is still simulated.")
    cols = st.columns(4)
    cols[0].metric("SMS", "Ready" if sms_provider_ready() else "Simulated")
    cols[1].metric("Webhook", "Ready" if WEBHOOK_SECRET else "Needs secret")
    cols[2].metric("Calendar", BOOKING_PROVIDER)
    cols[3].metric("Payments", PAYMENT_PROVIDER)

    st.markdown("#### Missed-call webhook test")
    sample_payload = {
        "provider": "twilio",
        "event_type": "missed_call",
        "name": "Jordan Lee",
        "phone": "404-555-0101",
        "call_id": "demo-call-001",
    }
    payload_text = st.text_area("Webhook payload JSON", value=json.dumps(sample_payload, indent=2), height=150)
    signature = st.text_input("Webhook signature", value="")
    col_a, col_b = st.columns(2)
    if col_a.button("Process missed-call webhook", width="stretch"):
        try:
            conversation_id = process_missed_call_webhook(json.loads(payload_text), signature)
            st.session_state["active_conversation_id"] = conversation_id
            st.success(f"Webhook processed into conversation #{conversation_id}.")
            st.rerun()
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(str(exc))
    if col_b.button("Process inbound SMS webhook", width="stretch"):
        try:
            intent = process_inbound_sms_webhook(json.loads(payload_text), signature)
            st.success(f"Inbound SMS processed. Intent: {intent}")
            st.rerun()
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(str(exc))

    st.markdown("#### Calendar and reminders")
    appointments = upcoming_appointments(limit=25)
    if appointments.empty:
        st.info("Book an appointment before exporting calendar files.")
    else:
        appointment_id = st.selectbox(
            "Appointment",
            appointments["id"].tolist(),
            format_func=lambda item: f"#{item} | {appointments.loc[appointments['id'] == item, 'client'].iloc[0]} | {appointments.loc[appointments['id'] == item, 'appointment_date'].iloc[0]}",
        )
        detail = appointment_detail(int(appointment_id))
        if detail:
            st.download_button(
                "Download ICS calendar event",
                data=build_ics(detail),
                file_name=f"salon-appointment-{appointment_id}.ics",
                mime="text/calendar",
                width="stretch",
            )
            if st.button("Queue calendar sync", width="stretch"):
                status, details = sync_appointment_to_calendar(int(appointment_id))
                st.success(f"{status}: {details}")
                st.rerun()
    reminders = load_df(
        """
        SELECT appointment_id, reminder_type, scheduled_for, status, message
        FROM appointment_reminders
        ORDER BY scheduled_for
        """
    )
    st.markdown("#### Reminder queue")
    st.dataframe(reminders, hide_index=True, width="stretch")

    st.markdown("#### Webhook event log")
    webhook_events = load_df(
        """
        SELECT provider, event_type, phone, signature_status, conversation_id, created_at
        FROM webhook_events
        ORDER BY created_at DESC
        LIMIT 25
        """
    )
    st.dataframe(webhook_events, hide_index=True, width="stretch")


def render_analytics_tab() -> None:
    st.subheader("Owner Analytics")
    summary = analytics_summary()
    cols = st.columns(5)
    cols[0].metric("Missed calls", int(summary["missed_calls"]))
    cols[1].metric("Client replies", int(summary["client_replies"]), delta=f"{summary['response_rate']:.0f}%")
    cols[2].metric("Bookings", int(summary["bookings"]), delta=f"{summary['conversion']:.0f}%")
    cols[3].metric("Recovered revenue", f"${summary['recovered_revenue']:,.0f}+")
    cols[4].metric("Avg response", "Live after webhook")

    funnel = pd.DataFrame(
        [
            {"stage": "Missed calls", "count": int(summary["missed_calls"])},
            {"stage": "Client replies", "count": int(summary["client_replies"])},
            {"stage": "Bookings", "count": int(summary["bookings"])},
        ]
    )
    st.markdown("#### Recovery funnel")
    st.bar_chart(funnel.set_index("stage"))

    by_service = load_df(
        """
        SELECT services.name AS service, COUNT(*) AS bookings, SUM(services.base_price) AS starting_revenue
        FROM appointments
        JOIN services ON services.id = appointments.service_id
        WHERE appointments.status != 'Cancelled'
        GROUP BY services.name
        ORDER BY bookings DESC, starting_revenue DESC
        """
    )
    st.markdown("#### Bookings by service")
    st.dataframe(by_service, hide_index=True, width="stretch")

    audit = load_df(
        """
        SELECT actor, action, entity_type, entity_id, details, created_at
        FROM audit_events
        ORDER BY created_at DESC
        LIMIT 30
        """
    )
    st.markdown("#### Audit trail")
    st.dataframe(audit, hide_index=True, width="stretch")


def main() -> None:
    if not require_staff_session():
        return
    init_db()
    render_header()
    render_metrics()
    tabs = st.tabs(
        [
            "Overview",
            "Missed calls",
            "Text flow",
            "Prices & booking",
            "Stylist updates",
            "Consent",
            "Integrations",
            "Analytics",
            "Database",
            "Launch plan",
        ]
    )
    with tabs[0]:
        render_overview_tab()
    with tabs[1]:
        render_missed_call_tab()
    with tabs[2]:
        render_conversation_tab()
    with tabs[3]:
        render_booking_tab()
    with tabs[4]:
        render_notifications_tab()
    with tabs[5]:
        render_consent_tab()
    with tabs[6]:
        render_integrations_tab()
    with tabs[7]:
        render_analytics_tab()
    with tabs[8]:
        render_admin_tab()
    with tabs[9]:
        render_launch_plan_tab()


if __name__ == "__main__":
    main()
