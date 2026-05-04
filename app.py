from __future__ import annotations

import os
import sqlite3
import re
import textwrap
from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import pandas as pd
import requests
import streamlit as st


USASPENDING_AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PUBLIC_SEARCH_URL = "https://duckduckgo.com/html/"
SAM_OPPORTUNITIES_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_EMAIL_VERIFIER_URL = "https://api.hunter.io/v2/email-verifier"
HUBSPOT_API_BASE_URL = "https://api.hubapi.com"
APP_DB_PATH = Path("application0_crm.sqlite3")
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_STATUSES = ["New", "Researching", "Contact found", "Emailed", "Meeting booked", "Nurture", "Disqualified"]
SCAN_BUDGET_SECONDS = 28
SEARCH_TIMEOUT_SECONDS = 6
PAGE_TIMEOUT_SECONDS = 6
REQUEST_HEADERS = {
    "User-Agent": "Application-0 GovDash SDR research prototype (public web discovery; +https://github.com/kapone2010-hash/application-0)"
}
BLOCKED_FETCH_DOMAINS = {
    "linkedin.com",
    "www.linkedin.com",
    "facebook.com",
    "www.facebook.com",
    "x.com",
    "twitter.com",
    "www.google.com",
    "duckduckgo.com",
    "www.duckduckgo.com",
    "instagram.com",
    "www.instagram.com",
}
CONTACT_TITLE_KEYWORDS = [
    "chief executive",
    "ceo",
    "president",
    "founder",
    "business development",
    "bd",
    "growth",
    "capture",
    "proposal",
    "contracts",
    "contracting",
    "program manager",
    "program director",
    "operations",
    "vp",
    "vice president",
    "director",
    "cto",
    "chief technology",
]
GENERIC_EMAIL_PREFIXES = {"info", "support", "sales", "contact", "admin", "hello", "careers", "jobs", "hr"}
CONSUMER_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}
NON_COMPANY_SITE_DOMAINS = {
    "acquisition.gov",
    "bloomberg.com",
    "crunchbase.com",
    "defense.gov",
    "dnb.com",
    "facebook.com",
    "fpds.gov",
    "govinfo.gov",
    "instagram.com",
    "linkedin.com",
    "prnewswire.com",
    "sam.gov",
    "twitter.com",
    "usaspending.gov",
    "x.com",
    "zoominfo.com",
}
COMPANY_LEGAL_SUFFIXES = {
    "and",
    "co",
    "company",
    "corp",
    "corporation",
    "dba",
    "inc",
    "incorporated",
    "llc",
    "llp",
    "lp",
    "ltd",
    "pllc",
    "services",
    "solutions",
    "the",
}
DEFAULT_CADENCE = [
    ("Day 1", "Email", "Congratulate them on the award, cite the agency/value, and ask who owns capture or proposal operations."),
    ("Day 2", "LinkedIn", "Connect with the named target and reference the award without pitching hard."),
    ("Day 4", "Call", "Ask for the capture/proposal/contracts owner and mention the award-specific GovDash workflow."),
    ("Day 7", "Email", "Send the short demo premise: award record, compliance matrix, reusable past performance, and option-year evidence."),
    ("Day 10", "Call", "Follow up with one concrete question about kickoff, recompete, or follow-on capture process."),
    ("Day 14", "Nurture", "Send a useful GovDash use case and move to monthly nurture if there is no engagement."),
]


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def parse_source_datetime(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19] if "%H" in fmt else value[:10], fmt)
        except ValueError:
            continue
    return None


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(APP_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    with db_connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_accounts (
                company TEXT PRIMARY KEY,
                status TEXT,
                owner TEXT,
                persona TEXT,
                cadence_stage TEXT,
                next_action TEXT,
                next_step TEXT,
                emailed INTEGER DEFAULT 0,
                called INTEGER DEFAULT 0,
                email_outcome TEXT,
                call_outcome TEXT,
                notes TEXT,
                updated_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                full_name TEXT NOT NULL,
                title TEXT,
                email TEXT,
                phone TEXT,
                linkedin_url TEXT,
                source_url TEXT,
                source_type TEXT,
                verification_status TEXT,
                verified_at TEXT,
                verified_by TEXT,
                verification_method TEXT,
                notes TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        existing_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(verified_contacts)").fetchall()
        }
        if "verified_by" not in existing_columns:
            connection.execute("ALTER TABLE verified_contacts ADD COLUMN verified_by TEXT DEFAULT ''")
        if "verification_method" not in existing_columns:
            connection.execute("ALTER TABLE verified_contacts ADD COLUMN verification_method TEXT DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_verified_contacts_company ON verified_contacts(company)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                contact_name TEXT,
                subject TEXT,
                outcome TEXT,
                notes TEXT,
                due_date TEXT,
                completed INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_crm_activities_company ON crm_activities(company)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_audit_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                evidence_type TEXT,
                item TEXT,
                evidence_level TEXT,
                recency_gate TEXT,
                captured_verified TEXT,
                source TEXT,
                source_url TEXT,
                evidence_snippet TEXT,
                audit_status TEXT,
                sdr_action TEXT,
                reviewer TEXT,
                review_note TEXT,
                created_at TEXT
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_source_audit_company ON source_audit_records(company)")


def secret_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        value = st.secrets.get(name, "")
    except Exception:
        return ""
    return str(value).strip()


def supabase_config() -> dict[str, str]:
    return {
        "url": secret_value("SUPABASE_URL").rstrip("/"),
        "service_role_key": secret_value("SUPABASE_SERVICE_ROLE_KEY"),
    }


def supabase_enabled() -> bool:
    config = supabase_config()
    return bool(config["url"] and config["service_role_key"])


def storage_backend_name() -> str:
    return "Supabase" if supabase_enabled() else "SQLite local"


def storage_warning(message: str) -> None:
    try:
        st.session_state["storage_warning"] = message
    except Exception:
        pass


def supabase_headers(prefer: str = "") -> dict[str, str]:
    config = supabase_config()
    headers = {
        "apikey": config["service_role_key"],
        "Authorization": f"Bearer {config['service_role_key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_endpoint(table: str) -> str:
    return f"{supabase_config()['url']}/rest/v1/{table}"


def supabase_request(method: str, table: str, **kwargs: object) -> object:
    response = requests.request(
        method,
        supabase_endpoint(table),
        headers=kwargs.pop("headers", supabase_headers()),
        timeout=12,
        **kwargs,
    )
    response.raise_for_status()
    if not response.content:
        return None
    return response.json()


def supabase_select(table: str, params: dict[str, object]) -> list[dict[str, object]]:
    result = supabase_request("GET", table, params=params)
    return result if isinstance(result, list) else []


def supabase_insert(table: str, payload: dict[str, object]) -> dict[str, object]:
    result = supabase_request(
        "POST",
        table,
        headers=supabase_headers("return=representation"),
        json=payload,
    )
    return result[0] if isinstance(result, list) and result else {}


def supabase_upsert(table: str, payload: dict[str, object], conflict_columns: str) -> dict[str, object]:
    result = supabase_request(
        "POST",
        table,
        headers=supabase_headers("resolution=merge-duplicates,return=representation"),
        params={"on_conflict": conflict_columns},
        json=payload,
    )
    return result[0] if isinstance(result, list) and result else {}


def supabase_patch(table: str, row_id: int, payload: dict[str, object]) -> None:
    supabase_request(
        "PATCH",
        table,
        headers=supabase_headers(),
        params={"id": f"eq.{row_id}"},
        json=payload,
    )


def supabase_delete(table: str, row_id: int) -> None:
    supabase_request("DELETE", table, params={"id": f"eq.{row_id}"})


def supabase_ping() -> tuple[bool, str]:
    if not supabase_enabled():
        return False, "Supabase secrets are not configured. Using local SQLite."
    try:
        supabase_select("crm_accounts", {"select": "company", "limit": "1"})
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return False, f"Supabase configured but not ready. HTTP {status}: {exc.response.text[:180] if exc.response is not None else exc}"
    except requests.RequestException as exc:
        return False, f"Supabase configured but unreachable: {exc}"
    return True, "Supabase is configured and reachable."


def sam_api_key() -> str:
    return secret_value("SAM_API_KEY")


def sam_enabled() -> bool:
    return bool(sam_api_key())


def hunter_api_key() -> str:
    return secret_value("HUNTER_API_KEY")


def hunter_enabled() -> bool:
    return bool(hunter_api_key())


def hubspot_access_token() -> str:
    return secret_value("HUBSPOT_ACCESS_TOKEN")


def hubspot_enabled() -> bool:
    return bool(hubspot_access_token())


def hubspot_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {hubspot_access_token()}",
        "Content-Type": "application/json",
        "User-Agent": REQUEST_HEADERS["User-Agent"],
    }


def hubspot_request(method: str, path: str, **kwargs: object) -> dict[str, object]:
    response = requests.request(
        method,
        f"{HUBSPOT_API_BASE_URL}{path}",
        headers=hubspot_headers(),
        timeout=30,
        **kwargs,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def hubspot_ping() -> tuple[bool, str]:
    if not hubspot_enabled():
        return False, "HUBSPOT_ACCESS_TOKEN is not configured."
    try:
        hubspot_request("GET", "/crm/v3/objects/companies", params={"limit": "1", "properties": "name,domain"})
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:180] if exc.response is not None else str(exc)
        return False, f"HubSpot configured but not ready. HTTP {status}: {body}"
    except requests.RequestException as exc:
        return False, f"HubSpot configured but unreachable: {exc}"
    return True, "HubSpot is configured for company/contact sync."


@dataclass(frozen=True)
class Prospect:
    award_id: str
    company: str
    uei: str
    amount: float
    base_obligation_date: str
    last_modified_date: str
    start_date: str
    end_date: str
    awarding_agency: str
    awarding_sub_agency: str
    funding_agency: str
    funding_sub_agency: str
    description: str
    naics_code: str
    naics_description: str
    psc_code: str
    psc_description: str
    address: str
    city: str
    state: str
    country: str

    @property
    def location(self) -> str:
        parts = [self.city, self.state, self.country]
        return ", ".join(part for part in parts if part)

    @property
    def contract_focus(self) -> str:
        text = " ".join([self.description, self.naics_description, self.psc_description]).lower()
        if any(term in text for term in ["software", "data", "cyber", "cloud", "telecom", "network", "information technology", "it and telecom"]):
            return "technical delivery, security evidence, and fast recompete readiness"
        if any(term in text for term in ["facilities", "construction", "maintenance", "repair", "operation"]):
            return "field delivery documentation, subcontractor coordination, and compliance tracking"
        if any(term in text for term in ["research", "engineering", "laboratory", "professional", "management"]):
            return "capture research, past-performance reuse, and technical-volume drafting"
        return "award kickoff, compliance organization, and future-opportunity capture"

    @property
    def govdash_fit_score(self) -> int:
        score = 45
        if self.amount >= 10_000_000:
            score += 25
        elif self.amount >= 1_000_000:
            score += 18
        elif self.amount >= 250_000:
            score += 10

        description = self.description.lower()
        if any(term in description for term in ["option", "idiq", "task order", "subscription", "support", "management"]):
            score += 12
        if self.end_date:
            score += 5
        if self.naics_code.startswith(("541", "517", "561")):
            score += 10
        return min(score, 99)

    @property
    def urgency(self) -> str:
        if self.start_date and self.start_date <= date.today().isoformat():
            return "Active now"
        if self.start_date:
            return "Starts soon"
        return "Newly reported"


@dataclass(frozen=True)
class Account:
    company: str
    prospects: tuple[Prospect, ...]

    @property
    def primary(self) -> Prospect:
        return sorted(self.prospects, key=lambda item: (item.govdash_fit_score, item.amount), reverse=True)[0]

    @property
    def award_count(self) -> int:
        return len(self.prospects)

    @property
    def total_amount(self) -> float:
        return sum(prospect.amount for prospect in self.prospects)

    @property
    def largest_award(self) -> float:
        return max((prospect.amount for prospect in self.prospects), default=0)

    @property
    def agencies(self) -> list[str]:
        names = {
            prospect.funding_sub_agency
            or prospect.awarding_sub_agency
            or prospect.awarding_agency
            for prospect in self.prospects
        }
        return sorted(name for name in names if name)

    @property
    def latest_award_date(self) -> str:
        dates = [prospect.base_obligation_date for prospect in self.prospects if prospect.base_obligation_date]
        return max(dates) if dates else ""

    @property
    def latest_source_modified_date(self) -> str:
        dates = [prospect.last_modified_date for prospect in self.prospects if prospect.last_modified_date]
        return max(dates) if dates else ""

    @property
    def priority_score(self) -> int:
        primary = self.primary
        score = primary.govdash_fit_score
        if self.award_count >= 3:
            score += 8
        elif self.award_count == 2:
            score += 4
        if self.total_amount >= 5_000_000:
            score += 8
        elif self.total_amount >= 1_000_000:
            score += 4
        if len(self.agencies) >= 2:
            score += 5
        if any("option" in prospect.description.lower() for prospect in self.prospects):
            score += 4
        return min(score, 100)

    @property
    def tier(self) -> str:
        if self.priority_score >= 85:
            return "Tier 1"
        if self.priority_score >= 70:
            return "Tier 2"
        return "Tier 3"


@dataclass(frozen=True)
class ContactTarget:
    rank: int
    title: str
    why: str
    message_angle: str
    search_query: str


@dataclass(frozen=True)
class PublicContact:
    full_name: str
    title: str
    email: str
    phone: str
    source_url: str
    evidence: str
    confidence: int
    recommended_reason: str


@dataclass(frozen=True)
class VerifiedContact:
    id: int
    company: str
    full_name: str
    title: str
    email: str
    phone: str
    linkedin_url: str
    source_url: str
    source_type: str
    verification_status: str
    verified_at: str
    verified_by: str
    verification_method: str
    notes: str


@dataclass(frozen=True)
class CrmActivity:
    id: int
    company: str
    activity_type: str
    contact_name: str
    subject: str
    outcome: str
    notes: str
    due_date: str
    completed: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SourceAuditRecord:
    id: int
    company: str
    evidence_type: str
    item: str
    evidence_level: str
    recency_gate: str
    captured_verified: str
    source: str
    source_url: str
    evidence_snippet: str
    audit_status: str
    sdr_action: str
    reviewer: str
    review_note: str
    created_at: str


@dataclass(frozen=True)
class HunterContact:
    full_name: str
    first_name: str
    last_name: str
    title: str
    email: str
    phone: str
    linkedin_url: str
    department: str
    seniority: str
    confidence: int
    verification_status: str
    result: str
    score: int
    domain: str
    company: str
    source_url: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    query: str


@dataclass(frozen=True)
class AccountSignal:
    signal_type: str
    title: str
    url: str
    snippet: str
    source: str
    recency_hint: str
    call_angle: str
    search_query: str


@dataclass(frozen=True)
class SourceFreshness:
    status: str
    checked_at: str
    latest_modified_date: str
    latest_award_date: str
    award_id: str
    recipient: str
    amount: float
    lag_days: int | None
    message: str
    api_messages: tuple[str, ...]


@dataclass(frozen=True)
class ContactQuality:
    status: str
    score: int
    relevance: str
    freshness: str
    next_step: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PainPoint:
    industry: str
    pain_point: str
    evidence_level: str
    evidence_title: str
    source_url: str
    source: str
    snippet: str
    severity: str
    govdash_angle: str
    recommended_question: str


@dataclass(frozen=True)
class SamOpportunity:
    notice_id: str
    title: str
    solicitation_number: str
    notice_type: str
    posted_date: str
    response_deadline: str
    set_aside: str
    set_aside_code: str
    naics_code: str
    classification_code: str
    department: str
    subtier: str
    office: str
    organization_path: str
    award_number: str
    award_amount: str
    award_date: str
    awardee_name: str
    awardee_uei: str
    place_of_performance: str
    point_of_contact: str
    poc_email: str
    poc_phone: str
    description_url: str
    ui_link: str
    resource_links: tuple[str, ...]
    match_reason: str


@dataclass(frozen=True)
class CompanyIntel:
    company: str
    website: str
    what_they_do: str
    why_they_may_have_won: str
    contacts: tuple[PublicContact, ...]
    linkedin_contacts: tuple[PublicContact, ...]
    linkedin_signals: tuple[WebSearchResult, ...]
    account_signals: tuple[AccountSignal, ...]
    pain_points: tuple[PainPoint, ...]
    sources: tuple[str, ...]
    scanned_urls: tuple[str, ...]
    scanned_at: str = ""


def build_search_payload(
    start: date,
    end: date,
    limit: int,
    min_amount: int,
    keyword: str,
    sort_field: str = "Base Obligation Date",
    order: str = "desc",
) -> dict:
    filters: dict[str, object] = {
        "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
        "award_type_codes": ["A", "B", "C", "D"],
    }
    if keyword:
        filters["keywords"] = [keyword]
    if min_amount:
        filters["award_amounts"] = [{"lower_bound": min_amount}]

    return {
        "filters": filters,
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient UEI",
            "Award Amount",
            "Base Obligation Date",
            "Last Modified Date",
            "Start Date",
            "End Date",
            "Awarding Agency",
            "Awarding Sub Agency",
            "Funding Agency",
            "Funding Sub Agency",
            "Description",
            "NAICS",
            "PSC",
            "recipient_location_city_name",
            "recipient_location_state_code",
            "recipient_location_country_name",
            "recipient_location_address_line1",
        ],
        "page": 1,
        "limit": limit,
        "sort": sort_field,
        "order": order,
        "subawards": False,
    }


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_recent_awards(start: date, end: date, limit: int, min_amount: int, keyword: str) -> tuple[list[dict], list[str]]:
    payload = build_search_payload(start, end, limit, min_amount, keyword)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(USASPENDING_AWARD_SEARCH_URL, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get("results", []), data.get("messages", [])
        except requests.RequestException as exc:
            last_error = exc
            sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"USAspending did not respond after 3 attempts: {last_error}") from last_error


def sam_date_window(account: Account) -> tuple[str, str]:
    latest = parse_iso_date(account.latest_award_date) or date.today()
    posted_from = max(latest - timedelta(days=365), date.today() - timedelta(days=730))
    posted_to = min(max(latest + timedelta(days=180), posted_from + timedelta(days=30)), date.today(), posted_from + timedelta(days=364))
    return posted_from.strftime("%m/%d/%Y"), posted_to.strftime("%m/%d/%Y")


def sam_query_params(account: Account, limit: int = 50) -> dict[str, str]:
    posted_from, posted_to = sam_date_window(account)
    params = {
        "api_key": sam_api_key(),
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "ptype": "a",
        "limit": str(limit),
        "offset": "0",
    }
    if account.primary.naics_code:
        params["ncode"] = account.primary.naics_code[:6]
    return params


def company_match_score(account: Account, item: dict[str, object]) -> tuple[int, list[str]]:
    company = account.company.lower()
    company_tokens = [token for token in re.split(r"[^a-z0-9]+", company) if len(token) > 3]
    award = item.get("award") if isinstance(item.get("award"), dict) else {}
    awardee = award.get("awardee") if isinstance(award.get("awardee"), dict) else {}
    haystack = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("solicitationNumber") or ""),
            str(award.get("number") or ""),
            str(awardee.get("name") or ""),
            str(awardee.get("ueiSAM") or ""),
            str(item.get("naicsCode") or ""),
            str(item.get("fullParentPathName") or ""),
        ]
    ).lower()
    reasons: list[str] = []
    score = 0
    if account.primary.uei and account.primary.uei.lower() in haystack:
        score += 70
        reasons.append("UEI match")
    if company and company in haystack:
        score += 45
        reasons.append("awardee/company name match")
    token_hits = [token for token in company_tokens if token in haystack]
    if token_hits:
        score += min(30, len(token_hits) * 8)
        reasons.append(f"company token match: {', '.join(token_hits[:3])}")
    if account.primary.naics_code and str(item.get("naicsCode") or "").startswith(account.primary.naics_code[:4]):
        score += 12
        reasons.append("NAICS context match")
    if account.primary.award_id and account.primary.award_id.lower() in haystack:
        score += 30
        reasons.append("award/solicitation number match")
    if not reasons:
        reasons.append("same date-window award-notice context")
    return score, reasons


def parse_sam_opportunity(account: Account, item: dict[str, object]) -> SamOpportunity:
    award = item.get("award") if isinstance(item.get("award"), dict) else {}
    awardee = award.get("awardee") if isinstance(award.get("awardee"), dict) else {}
    poc = first_poc(item.get("pointOfContact"))
    score, reasons = company_match_score(account, item)
    resource_links = item.get("resourceLinks") if isinstance(item.get("resourceLinks"), list) else []
    return SamOpportunity(
        notice_id=str(item.get("noticeId") or ""),
        title=str(item.get("title") or ""),
        solicitation_number=str(item.get("solicitationNumber") or ""),
        notice_type=str(item.get("type") or item.get("baseType") or ""),
        posted_date=str(item.get("postedDate") or ""),
        response_deadline=str(item.get("responseDeadLine") or item.get("reponseDeadLine") or ""),
        set_aside=str(item.get("typeOfSetAsideDescription") or item.get("setAside") or ""),
        set_aside_code=str(item.get("typeOfSetAside") or item.get("setAsideCode") or ""),
        naics_code=str(item.get("naicsCode") or ""),
        classification_code=str(item.get("classificationCode") or ""),
        department=str(item.get("department") or ""),
        subtier=str(item.get("subTier") or item.get("subtier") or ""),
        office=str(item.get("office") or ""),
        organization_path=str(item.get("fullParentPathName") or ""),
        award_number=str(award.get("number") or ""),
        award_amount=str(award.get("amount") or ""),
        award_date=str(award.get("date") or ""),
        awardee_name=str(awardee.get("name") or ""),
        awardee_uei=str(awardee.get("ueiSAM") or ""),
        place_of_performance=stateful_place(item.get("placeOfPerformance")),
        point_of_contact=str(poc.get("fullName") or poc.get("fullname") or poc.get("title") or ""),
        poc_email=str(poc.get("email") or ""),
        poc_phone=str(poc.get("phone") or ""),
        description_url=clean_sam_url(str(item.get("description") or "")),
        ui_link=clean_sam_url(str(item.get("uiLink") or "")),
        resource_links=tuple(str(link) for link in resource_links if link),
        match_reason=f"{score} match score; {'; '.join(reasons)}",
    )


def sam_match_score(opportunity: SamOpportunity) -> int:
    match = re.match(r"(\d+)", opportunity.match_reason)
    return int(match.group(1)) if match else 0


def fetch_sam_opportunities(account: Account, limit: int = 50) -> tuple[tuple[SamOpportunity, ...], str]:
    if not sam_enabled():
        return tuple(), "SAM_API_KEY is not configured."
    params = sam_query_params(account, limit)
    try:
        response = requests.get(
            SAM_OPPORTUNITIES_SEARCH_URL,
            params=params,
            headers=REQUEST_HEADERS,
            timeout=75,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:180] if exc.response is not None else str(exc)
        return tuple(), f"SAM.gov returned HTTP {status}: {body}"
    except requests.RequestException as exc:
        return tuple(), f"SAM.gov request failed: {exc}"
    rows = payload.get("opportunitiesData") or []
    if not isinstance(rows, list):
        rows = []
    parsed = [parse_sam_opportunity(account, row) for row in rows if isinstance(row, dict)]
    parsed.sort(key=lambda item: (sam_match_score(item), item.posted_date), reverse=True)
    posted_from, posted_to = sam_date_window(account)
    message = (
        f"Returned {len(parsed)} award notice candidate(s) from SAM.gov for {posted_from} to {posted_to}. "
        f"Total API records reported: {payload.get('totalRecords', 'unknown')}."
    )
    return tuple(parsed[:20]), message


def hunter_source_urls(row: dict[str, object]) -> tuple[str, ...]:
    sources = row.get("sources") if isinstance(row.get("sources"), list) else []
    urls = []
    for source in sources:
        if isinstance(source, dict):
            url = str(source.get("uri") or source.get("domain") or "")
            if url and url not in urls:
                urls.append(url)
    return tuple(urls[:5])


def parse_hunter_contact(row: dict[str, object], domain: str, company: str) -> HunterContact:
    first_name = str(row.get("first_name") or "")
    last_name = str(row.get("last_name") or "")
    full_name = str(row.get("value") or "")
    if not full_name or "@" in full_name:
        full_name = " ".join(part for part in [first_name, last_name] if part)
    email = str(row.get("value") or row.get("email") or "")
    linkedin_url = str(row.get("linkedin") or "")
    sources = hunter_source_urls(row)
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    return HunterContact(
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        title=str(row.get("position") or ""),
        email=email,
        phone=str(row.get("phone_number") or ""),
        linkedin_url=linkedin_url,
        department=str(row.get("department") or ""),
        seniority=str(row.get("seniority") or ""),
        confidence=int(row.get("confidence") or 0),
        verification_status=str(verification.get("status") or row.get("verification_status") or ""),
        result=str(verification.get("result") or row.get("result") or ""),
        score=int(verification.get("score") or row.get("score") or 0),
        domain=domain,
        company=company,
        source_url=sources[0] if sources else linkedin_url,
        sources=sources,
    )


def hunter_rank(contact: HunterContact) -> int:
    haystack = " ".join([contact.title, contact.department, contact.seniority]).lower()
    score = contact.confidence
    if contact.full_name:
        score += 20
    if contact.verification_status == "valid" or contact.result == "deliverable":
        score += 25
    elif contact.verification_status in {"accept_all", "unknown"}:
        score += 8
    if contact.phone:
        score += 10
    if any(keyword in haystack for keyword in CONTACT_TITLE_KEYWORDS):
        score += 25
    if any(term in haystack for term in ["business development", "capture", "proposal", "contracts", "executive", "management", "sales", "operations"]):
        score += 15
    return score


def fetch_hunter_contacts(company: str, domain: str = "", limit: int = 25) -> tuple[tuple[HunterContact, ...], str]:
    if not hunter_enabled():
        return tuple(), "HUNTER_API_KEY is not configured."
    clean_domain = clean_company_domain(domain)
    params: dict[str, object] = {
        "api_key": hunter_api_key(),
        "limit": min(max(limit, 1), 100),
        "type": "personal",
        "department": "executive,management,sales,operations,legal,it",
    }
    if clean_domain:
        params["domain"] = clean_domain
    else:
        params["company"] = company
    try:
        response = requests.get(
            HUNTER_DOMAIN_SEARCH_URL,
            params=params,
            headers=REQUEST_HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:180] if exc.response is not None else str(exc)
        return tuple(), f"Hunter returned HTTP {status}: {body}"
    except requests.RequestException as exc:
        return tuple(), f"Hunter request failed: {exc}"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result_domain = str(data.get("domain") or clean_domain)
    result_company = str(data.get("organization") or company)
    emails = data.get("emails") if isinstance(data.get("emails"), list) else []
    contacts = [parse_hunter_contact(row, result_domain, result_company) for row in emails if isinstance(row, dict)]
    contacts.sort(key=hunter_rank, reverse=True)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return tuple(contacts), (
        f"Hunter returned {len(contacts)} personal contact(s)"
        f"{' for ' + result_domain if result_domain else ''}. "
        f"Results reported: {meta.get('results', len(contacts))}."
    )


def hunter_contacts_dataframe(contacts: tuple[HunterContact, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Rank score": hunter_rank(contact),
                "Confidence label": confidence_label("Hunter contact", contact.verification_status or contact.result, "Hunter", contact.source_url),
                "Name": contact.full_name or "Name not returned",
                "Title": contact.title,
                "Email": contact.email,
                "Phone": contact.phone,
                "LinkedIn URL": contact.linkedin_url,
                "Department": contact.department,
                "Seniority": contact.seniority,
                "Hunter confidence": contact.confidence,
                "Verification": contact.verification_status,
                "Verifier result": contact.result,
                "Verifier score": contact.score,
                "Domain": contact.domain,
                "Company": contact.company,
                "Source URL": contact.source_url,
                "Sources": ", ".join(contact.sources),
            }
            for contact in contacts
        ]
    )


def hunter_contact_options(contacts: tuple[HunterContact, ...]) -> dict[str, int]:
    return {
        f"{contact.full_name or contact.email} | {contact.title or 'No title'} | {contact.email}": index
        for index, contact in enumerate(contacts)
    }


def company_name_similarity(left: str, right: str) -> int:
    left_norm = normalize_company_name(left)
    right_norm = normalize_company_name(right)
    if not left_norm or not right_norm:
        return 0
    return int(SequenceMatcher(None, left_norm, right_norm).ratio() * 100)


def hubspot_company_search_payload(filter_item: dict[str, str], limit: int = 5) -> dict[str, object]:
    return {
        "filterGroups": [{"filters": [filter_item]}],
        "properties": ["name", "domain"],
        "limit": limit,
    }


def hubspot_company_matches(company: str, domain: str = "", limit: int = 5) -> list[dict[str, str]]:
    filters: list[dict[str, str]] = []
    clean_domain = clean_company_domain(domain)
    if clean_domain:
        filters.append({"propertyName": "domain", "operator": "EQ", "value": clean_domain})
    filters.append({"propertyName": "name", "operator": "EQ", "value": company})
    if company:
        filters.append({"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": company})

    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    for filter_item in filters:
        try:
            result = hubspot_request(
                "POST",
                "/crm/v3/objects/companies/search",
                json=hubspot_company_search_payload(filter_item, limit),
            )
        except requests.HTTPError:
            if filter_item.get("operator") == "CONTAINS_TOKEN":
                continue
            raise
        rows = result.get("results") if isinstance(result.get("results"), list) else []
        for row in rows:
            company_id = str(row.get("id") or "")
            if not company_id or company_id in seen:
                continue
            properties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
            match_name = str(properties.get("name") or "")
            match_domain = clean_company_domain(str(properties.get("domain") or ""))
            reason_parts = []
            if clean_domain and match_domain == clean_domain:
                reason_parts.append("domain match")
            if match_name.lower() == company.lower():
                reason_parts.append("exact name")
            similarity = company_name_similarity(company, match_name)
            if similarity >= 82:
                reason_parts.append(f"name similarity {similarity}")
            matches.append(
                {
                    "id": company_id,
                    "name": match_name,
                    "domain": match_domain,
                    "similarity": str(similarity),
                    "reason": ", ".join(reason_parts) or "HubSpot search result",
                }
            )
            seen.add(company_id)
    matches.sort(
        key=lambda item: (
            item.get("domain") == clean_domain and bool(clean_domain),
            int(item.get("similarity") or 0),
        ),
        reverse=True,
    )
    return matches[:limit]


def hubspot_company_duplicate_warning(company: str, domain: str, matches: list[dict[str, str]]) -> str:
    clean_domain = clean_company_domain(domain)
    warning_matches = []
    for match in matches:
        similarity = int(match.get("similarity") or 0)
        same_domain = clean_domain and match.get("domain") == clean_domain
        same_name = match.get("name", "").lower() == company.lower()
        if same_domain or same_name or similarity >= 90:
            continue
        if similarity >= 65:
            warning_matches.append(match)
    if not warning_matches:
        return ""
    preview = "; ".join(
        f"{match.get('name') or 'Unnamed'}"
        f"{' (' + match.get('domain', '') + ')' if match.get('domain') else ''}"
        for match in warning_matches[:3]
    )
    return f"Potential HubSpot duplicate found before create: {preview}. Verify before creating a new company."


def hubspot_company_matches_dataframe(matches: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "HubSpot ID": match.get("id", ""),
                "Confidence label": "Verified source" if "domain match" in str(match.get("reason", "")).lower() or "exact name match" in str(match.get("reason", "")).lower() else "Needs verification",
                "Name": match.get("name", ""),
                "Domain": match.get("domain", ""),
                "Similarity": match.get("similarity", ""),
                "Match reason": match.get("reason", ""),
            }
            for match in matches
        ]
    )


def hubspot_search_company(company: str, domain: str = "") -> str:
    matches = hubspot_company_matches(company, domain, limit=5)
    clean_domain = clean_company_domain(domain)
    for match in matches:
        if clean_domain and match.get("domain") == clean_domain:
            return str(match.get("id") or "")
    for match in matches:
        if match.get("name", "").lower() == company.lower():
            return str(match.get("id") or "")
    for match in matches:
        if int(match.get("similarity") or 0) >= 90:
            return str(match.get("id") or "")
    return ""


def hubspot_upsert_company(account: Account, domain: str = "") -> tuple[str, str]:
    if not hubspot_enabled():
        return "", "HubSpot token is not configured."
    clean_domain = clean_company_domain(domain)
    properties = {"name": account.company}
    if clean_domain:
        properties["domain"] = clean_domain
    try:
        matches = hubspot_company_matches(account.company, clean_domain, limit=5)
        duplicate_warning = hubspot_company_duplicate_warning(account.company, clean_domain, matches)
        if duplicate_warning:
            return "", duplicate_warning
        company_id = hubspot_search_company(account.company, clean_domain)
        if company_id:
            hubspot_request("PATCH", f"/crm/v3/objects/companies/{company_id}", json={"properties": properties})
            return company_id, "HubSpot company updated."
        created = hubspot_request("POST", "/crm/v3/objects/companies", json={"properties": properties})
        return str(created.get("id") or ""), "HubSpot company created."
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:180] if exc.response is not None else str(exc)
        return "", f"HubSpot company sync failed. HTTP {status}: {body}"
    except requests.RequestException as exc:
        return "", f"HubSpot company sync failed: {exc}"


def hubspot_search_contact(email: str) -> str:
    if not email:
        return ""
    result = hubspot_request(
        "POST",
        "/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname"],
            "limit": 1,
        },
    )
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    return str(rows[0].get("id") or "") if rows else ""


def hubspot_contact_properties(contact: VerifiedContact) -> dict[str, str]:
    first_name, last_name = split_name(contact.full_name)
    properties = {
        "email": contact.email,
        "firstname": first_name,
        "lastname": last_name,
        "jobtitle": contact.title,
        "phone": contact.phone,
    }
    return {key: value for key, value in properties.items() if value}


def hubspot_associate_contact_to_company(contact_id: str, company_id: str) -> str:
    if not contact_id or not company_id:
        return "No association attempted."
    for association_type in ("contact_to_company", "1"):
        try:
            hubspot_request(
                "PUT",
                f"/crm/v3/objects/contacts/{contact_id}/associations/companies/{company_id}/{association_type}",
            )
            return "Contact associated to company."
        except requests.RequestException:
            continue
    return "Contact synced, but automatic company association was not available."


def hubspot_upsert_contact(contact: VerifiedContact, company_id: str = "") -> tuple[str, str]:
    if not hubspot_enabled():
        return "", "HubSpot token is not configured."
    if not contact.email:
        return "", "HubSpot contact sync requires an email address."
    properties = hubspot_contact_properties(contact)
    try:
        contact_id = hubspot_search_contact(contact.email)
        if contact_id:
            hubspot_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": properties})
            action = "HubSpot contact updated."
        else:
            created = hubspot_request("POST", "/crm/v3/objects/contacts", json={"properties": properties})
            contact_id = str(created.get("id") or "")
            action = "HubSpot contact created."
        association_message = hubspot_associate_contact_to_company(contact_id, company_id) if company_id else "No company association requested."
        return contact_id, f"{action} {association_message}"
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = exc.response.text[:180] if exc.response is not None else str(exc)
        return "", f"HubSpot contact sync failed. HTTP {status}: {body}"
    except requests.RequestException as exc:
        return "", f"HubSpot contact sync failed: {exc}"


def hubspot_sync_verified_contacts(
    contacts: tuple[VerifiedContact, ...],
    company_id: str,
) -> tuple[int, int, list[str]]:
    synced = 0
    skipped = 0
    errors: list[str] = []
    for contact in contacts:
        if not contact.email:
            skipped += 1
            continue
        contact_id, message = hubspot_upsert_contact(contact, company_id)
        if contact_id:
            synced += 1
        else:
            errors.append(f"{contact.full_name or contact.email}: {message}")
    return synced, skipped, errors


def hubspot_timestamp(value: str = "") -> str:
    parsed_date = parse_iso_date(value)
    if parsed_date:
        return f"{parsed_date.isoformat()}T14:00:00Z"
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def hubspot_activity_body(activity: CrmActivity, account: Account) -> str:
    parts = [
        f"Application 0 activity for {account.company}",
        f"Type: {activity.activity_type}",
        f"Contact: {activity.contact_name or 'Not specified'}",
        f"Outcome: {activity.outcome or 'Not specified'}",
        f"Due date: {activity.due_date or 'Not specified'}",
        f"Completed: {'yes' if activity.completed else 'no'}",
        "",
        activity.notes or activity.subject,
    ]
    return "\n".join(part for part in parts if part is not None).strip()


def hubspot_task_type(activity_type: str) -> str:
    normalized = activity_type.lower()
    if normalized == "call":
        return "CALL"
    if normalized == "email":
        return "EMAIL"
    return "TODO"


def hubspot_call_status(activity: CrmActivity) -> str:
    outcome = activity.outcome.lower()
    if "no answer" in outcome:
        return "NO_ANSWER"
    if "bad number" in outcome:
        return "FAILED"
    if "left voicemail" in outcome or "completed" in outcome or "connected" in outcome or activity.completed:
        return "COMPLETED"
    return "QUEUED"


def hubspot_activity_kind(activity: CrmActivity) -> str:
    activity_type = activity.activity_type.lower()
    if activity_type == "note":
        return "notes"
    if activity_type == "call" and (activity.completed or activity.outcome):
        return "calls"
    return "tasks"


def hubspot_activity_properties(activity: CrmActivity, account: Account, kind: str) -> dict[str, str]:
    body = hubspot_activity_body(activity, account)
    subject = activity.subject or f"{activity.activity_type}: {account.company}"
    timestamp = hubspot_timestamp(activity.due_date)
    if kind == "notes":
        return {
            "hs_timestamp": hubspot_timestamp(activity.created_at),
            "hs_note_body": body,
        }
    if kind == "calls":
        return {
            "hs_timestamp": timestamp,
            "hs_call_title": subject,
            "hs_call_body": body,
            "hs_call_status": hubspot_call_status(activity),
            "hs_call_direction": "OUTBOUND",
        }
    return {
        "hs_timestamp": timestamp,
        "hs_task_subject": subject,
        "hs_task_body": body,
        "hs_task_status": "COMPLETED" if activity.completed else "NOT_STARTED",
        "hs_task_type": hubspot_task_type(activity.activity_type),
        "hs_task_priority": "HIGH" if activity.activity_type.lower() in {"call", "demo follow-up"} else "MEDIUM",
    }


def hubspot_activity_paths(kind: str) -> tuple[str, ...]:
    return (
        f"/crm/objects/2026-03/{kind}",
        f"/crm/v3/objects/{kind}",
    )


def hubspot_create_activity(kind: str, properties: dict[str, str]) -> tuple[str, str]:
    last_error = ""
    for path in hubspot_activity_paths(kind):
        try:
            created = hubspot_request("POST", path, json={"properties": properties})
            return str(created.get("id") or ""), f"HubSpot {kind[:-1]} created."
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            body = exc.response.text[:180] if exc.response is not None else str(exc)
            last_error = f"HTTP {status}: {body}"
            if status not in {404, 405}:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
            break
    return "", f"HubSpot {kind[:-1]} sync failed. {last_error}"


def hubspot_associate_default(from_type: str, from_id: str, to_type: str, to_id: str) -> str:
    if not from_id or not to_id:
        return "No association attempted."
    for source_type in (from_type, from_type.rstrip("s")):
        for target_type in (to_type, to_type.rstrip("s")):
            try:
                hubspot_request(
                    "PUT",
                    f"/crm/v4/objects/{source_type}/{from_id}/associations/default/{target_type}/{to_id}",
                )
                return f"Associated {source_type} to {target_type}."
            except requests.RequestException:
                continue
    return "Activity created, but automatic association was not available."


def verified_contact_for_activity(company: str, contact_name: str) -> VerifiedContact | None:
    clean_name = (contact_name or "").strip().lower()
    if not clean_name:
        return None
    for contact in load_verified_contacts(company):
        if contact.full_name.strip().lower() == clean_name:
            return contact
    for contact in load_verified_contacts(company):
        if clean_name in contact.full_name.strip().lower() or contact.full_name.strip().lower() in clean_name:
            return contact
    return None


def hubspot_sync_activity(account: Account, activity: CrmActivity, domain: str = "") -> tuple[str, str]:
    if not hubspot_enabled():
        return "", "HubSpot token is not configured."
    company_id, company_message = hubspot_upsert_company(account, domain)
    if not company_id:
        return "", company_message

    contact = verified_contact_for_activity(account.company, activity.contact_name)
    contact_id = ""
    contact_message = ""
    if contact is not None and contact.email:
        contact_id, contact_message = hubspot_upsert_contact(contact, company_id)

    kind = hubspot_activity_kind(activity)
    activity_id, activity_message = hubspot_create_activity(kind, hubspot_activity_properties(activity, account, kind))
    if not activity_id:
        return "", activity_message

    association_messages = [hubspot_associate_default(kind, activity_id, "companies", company_id)]
    if contact_id:
        association_messages.append(hubspot_associate_default(kind, activity_id, "contacts", contact_id))

    details = "; ".join(part for part in [company_message, contact_message, *association_messages] if part)
    return activity_id, f"{activity_message} ID: {activity_id}. {details}"


def cadence_day_offset(day_label: str) -> int:
    match = re.search(r"\d+", day_label or "")
    if not match:
        return 0
    return max(int(match.group(0)) - 1, 0)


def cadence_activity_subject(account: Account, day_label: str, action: str) -> str:
    return f"{day_label} {action}: {account.company} GovDash follow-up"


def build_cadence_activities(account: Account, contact_name: str, start: date) -> tuple[CrmActivity, ...]:
    now = datetime.now().isoformat(timespec="seconds")
    activities: list[CrmActivity] = []
    for day_label, action, detail in DEFAULT_CADENCE:
        due = start + timedelta(days=cadence_day_offset(day_label))
        activities.append(
            CrmActivity(
                id=0,
                company=account.company,
                activity_type=action,
                contact_name=contact_name,
                subject=cadence_activity_subject(account, day_label, action),
                outcome="Planned",
                notes=detail,
                due_date=due.isoformat(),
                completed=False,
                created_at=now,
                updated_at=now,
            )
        )
    return tuple(activities)


def cadence_preview_dataframe(activities: tuple[CrmActivity, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Due date": activity.due_date,
                "Type": activity.activity_type,
                "Contact": activity.contact_name,
                "Subject": activity.subject,
                "Notes": activity.notes,
            }
            for activity in activities
        ]
    )


def save_cadence_activities(activities: tuple[CrmActivity, ...]) -> int:
    for activity in activities:
        save_crm_activity(
            activity.company,
            activity.activity_type,
            activity.contact_name,
            activity.subject,
            activity.outcome,
            activity.notes,
            activity.due_date,
            activity.completed,
        )
    return len(activities)


def hubspot_sync_cadence(account: Account, activities: tuple[CrmActivity, ...], domain: str = "") -> tuple[int, list[str]]:
    synced = 0
    errors: list[str] = []
    for activity in activities:
        activity_id, message = hubspot_sync_activity(account, activity, domain)
        if activity_id:
            synced += 1
        else:
            errors.append(f"{activity.subject}: {message}")
    return synced, errors


@st.cache_data(ttl=900, show_spinner=False)
def check_usaspending_freshness(start: date, end: date, min_amount: int, keyword: str) -> SourceFreshness:
    payload = build_search_payload(start, end, 1, min_amount, keyword, sort_field="Last Modified Date")
    last_error: Exception | None = None
    checked_at = datetime.now()
    for attempt in range(3):
        try:
            response = requests.post(USASPENDING_AWARD_SEARCH_URL, json=payload, timeout=20)
            response.raise_for_status()
            data = response.json()
            rows = data.get("results", [])
            messages = tuple(str(message) for message in data.get("messages", []))
            if not rows:
                return SourceFreshness(
                    status="No matching data",
                    checked_at=checked_at.strftime("%b %d, %Y %I:%M %p"),
                    latest_modified_date="",
                    latest_award_date="",
                    award_id="",
                    recipient="",
                    amount=0,
                    lag_days=None,
                    message="USAspending responded, but no matching records were found for the current filters.",
                    api_messages=messages,
                )

            row = rows[0]
            latest_modified = str(row.get("Last Modified Date") or "")
            modified_at = parse_source_datetime(latest_modified)
            lag_days = (checked_at.date() - modified_at.date()).days if modified_at else None
            if lag_days is None:
                status = "Unknown freshness"
                message = "USAspending responded, but the latest modified timestamp could not be parsed."
            elif lag_days <= 7:
                status = "Current"
                message = "USAspending has recent modifications for these filters."
            elif lag_days <= 14:
                status = "Aging"
                message = "USAspending has matching data, but the newest modification is more than a week old."
            else:
                status = "Stale"
                message = "USAspending has matching data, but the newest modification is more than two weeks old."

            return SourceFreshness(
                status=status,
                checked_at=checked_at.strftime("%b %d, %Y %I:%M %p"),
                latest_modified_date=latest_modified,
                latest_award_date=str(row.get("Base Obligation Date") or ""),
                award_id=str(row.get("Award ID") or ""),
                recipient=str(row.get("Recipient Name") or ""),
                amount=float(row.get("Award Amount") or 0),
                lag_days=lag_days,
                message=message,
                api_messages=messages,
            )
        except requests.RequestException as exc:
            last_error = exc
            sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"USAspending freshness check failed after 3 attempts: {last_error}") from last_error


def nested_field(row: dict, field: str, child: str) -> str:
    value = row.get(field) or {}
    if isinstance(value, dict):
        return str(value.get(child) or "")
    return ""


def nested_text(value: object, *path: str) -> str:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    if current is None:
        return ""
    return str(current)


def stateful_place(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    city = nested_text(value, "city", "name") or str(value.get("city") or "")
    state = nested_text(value, "state", "code") or nested_text(value, "state", "name") or str(value.get("state") or "")
    country = nested_text(value, "country", "code") or nested_text(value, "country", "name") or str(value.get("country") or "")
    zip_code = str(value.get("zip") or value.get("zipcode") or "")
    return ", ".join(part for part in [city, state, zip_code, country] if part and part != "{}")


def first_poc(value: object) -> dict[str, object]:
    if isinstance(value, list) and value:
        item = value[0]
        return item if isinstance(item, dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def load_crm_record(company: str) -> dict[str, object]:
    if supabase_enabled():
        try:
            rows = supabase_select("crm_accounts", {"select": "*", "company": f"eq.{company}", "limit": "1"})
            return rows[0] if rows else {}
        except requests.RequestException as exc:
            storage_warning(f"Supabase CRM read failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM crm_accounts WHERE company = ?", (company,)).fetchone()
    return dict(row) if row else {}


def save_crm_record(company: str, crm: dict[str, object]) -> None:
    payload = {
        "company": company,
        "status": str(crm.get("status", "New")),
        "owner": str(crm.get("owner", "")),
        "persona": str(crm.get("persona", "")),
        "cadence_stage": str(crm.get("cadence_stage", "")),
        "next_action": str(crm.get("next_action", "")),
        "next_step": str(crm.get("next_step", "")),
        "emailed": bool(crm.get("emailed", False)),
        "called": bool(crm.get("called", False)),
        "email_outcome": str(crm.get("email_outcome", "")),
        "call_outcome": str(crm.get("call_outcome", "")),
        "notes": str(crm.get("notes", "")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if supabase_enabled():
        try:
            supabase_upsert("crm_accounts", payload, "company")
            return
        except requests.RequestException as exc:
            storage_warning(f"Supabase CRM save failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO crm_accounts (
                company, status, owner, persona, cadence_stage, next_action, next_step,
                emailed, called, email_outcome, call_outcome, notes, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company) DO UPDATE SET
                status = excluded.status,
                owner = excluded.owner,
                persona = excluded.persona,
                cadence_stage = excluded.cadence_stage,
                next_action = excluded.next_action,
                next_step = excluded.next_step,
                emailed = excluded.emailed,
                called = excluded.called,
                email_outcome = excluded.email_outcome,
                call_outcome = excluded.call_outcome,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                payload["company"],
                payload["status"],
                payload["owner"],
                payload["persona"],
                payload["cadence_stage"],
                payload["next_action"],
                payload["next_step"],
                int(bool(payload["emailed"])),
                int(bool(payload["called"])),
                payload["email_outcome"],
                payload["call_outcome"],
                payload["notes"],
                payload["updated_at"],
            ),
        )


def save_verified_contact(
    company: str,
    full_name: str,
    title: str,
    email: str,
    phone: str,
    linkedin_url: str,
    source_url: str,
    source_type: str,
    verification_status: str,
    notes: str,
    verified_by: str = "",
    verification_method: str = "",
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    values = (
        company.strip(),
        full_name.strip(),
        title.strip(),
        email.strip(),
        phone.strip(),
        linkedin_url.strip(),
        source_url.strip(),
        source_type.strip(),
        verification_status.strip(),
        now,
        verified_by.strip(),
        verification_method.strip(),
        notes.strip(),
        now,
        now,
    )
    if supabase_enabled():
        payload = {
            "company": values[0],
            "full_name": values[1],
            "title": values[2],
            "email": values[3],
            "phone": values[4],
            "linkedin_url": values[5],
            "source_url": values[6],
            "source_type": values[7],
            "verification_status": values[8],
            "verified_at": values[9],
            "verified_by": values[10],
            "verification_method": values[11],
            "notes": values[12],
            "created_at": values[13],
            "updated_at": values[14],
        }
        try:
            rows = supabase_select(
                "verified_contacts",
                {
                    "select": "id",
                    "company": f"eq.{values[0]}",
                    "full_name": f"eq.{values[1]}",
                    "title": f"eq.{values[2]}",
                    "email": f"eq.{values[3]}",
                    "linkedin_url": f"eq.{values[5]}",
                    "order": "id.desc",
                    "limit": "1",
                },
            )
            if rows:
                patch_payload = {
                    "phone": values[4],
                    "source_url": values[6],
                    "source_type": values[7],
                    "verification_status": values[8],
                    "verified_at": values[9],
                    "verified_by": values[10],
                    "verification_method": values[11],
                    "notes": values[12],
                    "updated_at": values[14],
                }
                try:
                    supabase_patch("verified_contacts", int(rows[0]["id"]), patch_payload)
                except requests.RequestException:
                    patch_payload.pop("verified_by", None)
                    patch_payload.pop("verification_method", None)
                    supabase_patch("verified_contacts", int(rows[0]["id"]), patch_payload)
            else:
                try:
                    supabase_insert("verified_contacts", payload)
                except requests.RequestException:
                    payload.pop("verified_by", None)
                    payload.pop("verification_method", None)
                    supabase_insert("verified_contacts", payload)
            return
        except requests.RequestException as exc:
            storage_warning(f"Supabase verified-contact save failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        existing = connection.execute(
            """
            SELECT id FROM verified_contacts
            WHERE company = ?
              AND lower(full_name) = lower(?)
              AND lower(COALESCE(title, '')) = lower(?)
              AND lower(COALESCE(email, '')) = lower(?)
              AND COALESCE(linkedin_url, '') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (values[0], values[1], values[2], values[3], values[5]),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE verified_contacts
                SET phone = ?,
                    source_url = ?,
                    source_type = ?,
                    verification_status = ?,
                    verified_at = ?,
                    verified_by = ?,
                    verification_method = ?,
                    notes = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (values[4], values[6], values[7], values[8], values[9], values[10], values[11], values[12], values[14], int(existing["id"])),
            )
            return
        connection.execute(
            """
            INSERT INTO verified_contacts (
                company, full_name, title, email, phone, linkedin_url, source_url, source_type,
                verification_status, verified_at, verified_by, verification_method, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def load_verified_contacts(company: str) -> tuple[VerifiedContact, ...]:
    if supabase_enabled():
        try:
            rows = supabase_select(
                "verified_contacts",
                {"select": "*", "company": f"eq.{company}", "order": "verified_at.desc,id.desc"},
            )
            return tuple(
                VerifiedContact(
                    id=int(row.get("id") or 0),
                    company=str(row.get("company") or ""),
                    full_name=str(row.get("full_name") or ""),
                    title=str(row.get("title") or ""),
                    email=str(row.get("email") or ""),
                    phone=str(row.get("phone") or ""),
                    linkedin_url=str(row.get("linkedin_url") or ""),
                    source_url=str(row.get("source_url") or ""),
                    source_type=str(row.get("source_type") or ""),
                    verification_status=str(row.get("verification_status") or ""),
                    verified_at=str(row.get("verified_at") or ""),
                    verified_by=str(row.get("verified_by") or ""),
                    verification_method=str(row.get("verification_method") or ""),
                    notes=str(row.get("notes") or ""),
                )
                for row in rows
            )
        except requests.RequestException as exc:
            storage_warning(f"Supabase verified-contact read failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM verified_contacts
            WHERE company = ?
            ORDER BY verified_at DESC, id DESC
            """,
            (company,),
        ).fetchall()
    return tuple(
        VerifiedContact(
            id=int(row["id"]),
            company=str(row["company"] or ""),
            full_name=str(row["full_name"] or ""),
            title=str(row["title"] or ""),
            email=str(row["email"] or ""),
            phone=str(row["phone"] or ""),
            linkedin_url=str(row["linkedin_url"] or ""),
            source_url=str(row["source_url"] or ""),
            source_type=str(row["source_type"] or ""),
            verification_status=str(row["verification_status"] or ""),
            verified_at=str(row["verified_at"] or ""),
            verified_by=str(row["verified_by"] or "") if "verified_by" in row.keys() else "",
            verification_method=str(row["verification_method"] or "") if "verification_method" in row.keys() else "",
            notes=str(row["notes"] or ""),
        )
        for row in rows
    )


def delete_verified_contact(contact_id: int) -> None:
    if supabase_enabled():
        try:
            supabase_delete("verified_contacts", contact_id)
            return
        except requests.RequestException as exc:
            storage_warning(f"Supabase verified-contact delete failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        connection.execute("DELETE FROM verified_contacts WHERE id = ?", (contact_id,))


def save_hunter_contact(company: str, contact: HunterContact) -> None:
    save_verified_contact(
        company=company,
        full_name=contact.full_name or contact.email,
        title=contact.title,
        email=contact.email,
        phone=contact.phone,
        linkedin_url=contact.linkedin_url,
        source_url=contact.source_url or (contact.sources[0] if contact.sources else ""),
        source_type="Hunter",
        verification_status=f"Hunter {contact.verification_status or contact.result or 'enriched'}".strip(),
        notes=(
            f"Hunter confidence {contact.confidence}; result {contact.result}; department {contact.department}; "
            f"seniority {contact.seniority}; domain {contact.domain}; sources {', '.join(contact.sources[:3])}"
        ),
    )


def email_ready_contacts(contacts: tuple[VerifiedContact, ...]) -> tuple[VerifiedContact, ...]:
    return tuple(contact for contact in contacts if contact.email.strip())


def auto_import_hunter_contacts_for_hubspot(
    account: Account,
    domain: str,
    existing_contacts: tuple[VerifiedContact, ...],
    limit: int = 5,
) -> tuple[tuple[VerifiedContact, ...], list[str], int]:
    messages: list[str] = []
    if email_ready_contacts(existing_contacts):
        return existing_contacts, messages, 0
    if not hunter_enabled():
        return existing_contacts, ["No saved contacts have an email yet, and Hunter is not configured."], 0

    hunter_contacts, hunter_message = fetch_hunter_contacts(account.company, domain, limit=max(limit * 2, 10))
    messages.append(hunter_message)
    existing_emails = {contact.email.strip().lower() for contact in existing_contacts if contact.email.strip()}
    imported = 0
    for hunter_contact in hunter_contacts:
        email = hunter_contact.email.strip().lower()
        if not email or email in existing_emails:
            continue
        save_verified_contact(
            company=account.company,
            full_name=hunter_contact.full_name or hunter_contact.email,
            title=hunter_contact.title,
            email=hunter_contact.email,
            phone=hunter_contact.phone,
            linkedin_url=hunter_contact.linkedin_url,
            source_url=hunter_contact.source_url or (hunter_contact.sources[0] if hunter_contact.sources else ""),
            source_type="Hunter",
            verification_status="Imported for verification",
            verification_method="Vendor enrichment checked",
            verified_by="Application 0 auto-enrichment",
            notes=(
                "Auto-imported during HubSpot sync because no saved email-ready contacts existed. "
                f"Hunter confidence {hunter_contact.confidence}; result {hunter_contact.result}; "
                f"status {hunter_contact.verification_status}; department {hunter_contact.department}; "
                f"seniority {hunter_contact.seniority}; domain {hunter_contact.domain}; "
                f"sources {', '.join(hunter_contact.sources[:3])}"
            ),
        )
        existing_emails.add(email)
        imported += 1
        if imported >= limit:
            break

    if imported:
        messages.append(f"Auto-imported {imported} Hunter contact(s) with email because no email-ready contacts were saved.")
        return load_verified_contacts(account.company), messages, imported
    messages.append("No email-ready contacts were found to sync. Run Contact Finder enrichment or save a verified contact with an email.")
    return existing_contacts, messages, 0


def hubspot_sync_result_key(company: str) -> str:
    return f"hubspot_sync_result_{company}"


def summarize_hubspot_matches(matches: list[dict[str, str]]) -> str:
    if not matches:
        return "No duplicate company matches returned."
    exact = [
        match
        for match in matches
        if "domain match" in str(match.get("reason", "")).lower()
        or "exact name match" in str(match.get("reason", "")).lower()
    ]
    if exact:
        names = ", ".join(str(match.get("name") or match.get("id") or "HubSpot company") for match in exact[:3])
        return f"{len(matches)} match(es), including exact domain/name match: {names}."
    names = ", ".join(str(match.get("name") or match.get("id") or "HubSpot company") for match in matches[:3])
    return f"{len(matches)} possible fuzzy match(es): {names}."


def hubspot_sync_result_dataframe(result: dict[str, object]) -> pd.DataFrame:
    rows = [
        {
            "Step": "Domain",
            "Confidence label": confidence_label("Domain", str(result.get("domain_source") or ""), "Public web", str(result.get("domain") or "")),
            "Result": str(result.get("domain") or "Not found"),
            "Why": str(result.get("domain_source") or "No source returned"),
            "Next action": str(result.get("domain_action") or ""),
        },
        {
            "Step": "Duplicate check",
            "Confidence label": "Verified source" if "exact" in str(result.get("duplicate_summary") or "").lower() else "Needs verification",
            "Result": str(result.get("duplicate_summary") or ""),
            "Why": "HubSpot search ran before the company sync.",
            "Next action": str(result.get("duplicate_action") or ""),
        },
        {
            "Step": "Company sync",
            "Confidence label": "Verified source" if result.get("company_id") else "Needs verification",
            "Result": str(result.get("company_message") or ""),
            "Why": f"HubSpot company ID: {result.get('company_id') or 'not created'}",
            "Next action": str(result.get("company_action") or ""),
        },
        {
            "Step": "Contact sync",
            "Confidence label": "Verified source" if int(result.get("synced_count") or 0) else ("Vendor enrichment" if int(result.get("imported_count") or 0) else "Needs verification"),
            "Result": (
                f"{int(result.get('synced_count') or 0)} synced, "
                f"{int(result.get('imported_count') or 0)} auto-imported, "
                f"{int(result.get('skipped_count') or 0)} skipped"
            ),
            "Why": str(result.get("contact_reason") or ""),
            "Next action": str(result.get("contact_action") or ""),
        },
    ]
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        rows.append(
            {
                "Step": "Errors",
                "Confidence label": "Needs verification",
                "Result": f"{len(errors)} issue(s)",
                "Why": "; ".join(str(error) for error in errors[:3]),
                "Next action": "Open the source contact, fix the email/permission issue, and sync again.",
            }
        )
    return pd.DataFrame(rows)


def render_hubspot_sync_result(result: dict[str, object]) -> None:
    st.markdown("#### Last HubSpot Sync Results")
    checked_at = str(result.get("checked_at") or "")
    if checked_at:
        st.caption(f"Last run: {checked_at}")
    result_cols = st.columns(4)
    result_cols[0].metric("Company ID", str(result.get("company_id") or "None"))
    result_cols[1].metric("Contacts synced", int(result.get("synced_count") or 0))
    result_cols[2].metric("Auto-imported", int(result.get("imported_count") or 0))
    result_cols[3].metric("Skipped", int(result.get("skipped_count") or 0))
    dataframe_with_links(hubspot_sync_result_dataframe(result), width="stretch", hide_index=True)
    enrichment_messages = result.get("enrichment_messages")
    if isinstance(enrichment_messages, list) and enrichment_messages:
        with st.expander("Enrichment detail", expanded=False):
            for message in enrichment_messages[:5]:
                st.write(str(message))


def verified_contact_to_public(contact: VerifiedContact) -> PublicContact:
    source = contact.linkedin_url or contact.source_url
    evidence = (
        f"Verified contact saved in CRM on {contact.verified_at}. "
        f"Status: {contact.verification_status}. Source type: {contact.source_type}. "
        f"Verified by: {contact.verified_by or 'not recorded'}. Method: {contact.verification_method or 'not recorded'}. Notes: {contact.notes}"
    )
    return PublicContact(
        full_name=contact.full_name,
        title=contact.title or "Verified contact",
        email=contact.email,
        phone=contact.phone,
        source_url=source,
        evidence=evidence,
        confidence=98 if contact.verification_status == "Verified current role" else 88,
        recommended_reason="Saved as a verified contact for this account. Use after confirming fit for the selected persona.",
    )


def verified_contact_evidence_grade(contact: VerifiedContact) -> str:
    status = contact.verification_status.lower()
    has_source = bool(contact.linkedin_url or contact.source_url)
    has_contact_path = bool(contact.email or contact.phone)
    has_role = bool(contact.title)
    if "do not" in status:
        return "D - blocked"
    if "verified current role" in status and has_source and has_contact_path and has_role:
        return "A - source-backed"
    if "verified current role" in status:
        return "B - verified, missing detail"
    if "needs recheck" in status:
        return "C - stale/recheck"
    if "imported" in status:
        return "C - imported"
    return "C - needs review"


def verified_contact_gate(contact: VerifiedContact) -> dict[str, str]:
    status = contact.verification_status.lower()
    age_days = days_since(contact.verified_at)
    has_source = bool(contact.linkedin_url or contact.source_url)
    has_contact_path = bool(contact.email or contact.phone)
    has_role = bool(contact.title)
    age = age_bucket(contact.verified_at)

    if "do not" in status:
        gate = "Do not sequence"
        action = "Keep out of cadence until a new source clears this contact."
    elif age_days is not None and age_days > 180:
        gate = "Recheck before sequence"
        action = "Open the source or LinkedIn profile and update the verified date before outreach."
    elif "needs recheck" in status or "imported" in status:
        gate = "Verify before sequence"
        action = "Confirm current role, company, and contact path before adding to cadence."
    elif "verified current role" in status and has_source and has_contact_path and has_role:
        gate = "Ready to sequence"
        action = "Use in cadence and keep the source link in HubSpot notes."
    elif "verified current role" in status:
        gate = "Verify missing fields"
        action = "Add a source URL, role, email, or phone before sequencing."
    else:
        gate = "Verify before sequence"
        action = "Treat as a research lead until verified."

    return {
        "gate": gate,
        "age": age,
        "evidence_grade": verified_contact_evidence_grade(contact),
        "action": action,
    }


def verified_contacts_dataframe(company: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ID": contact.id,
                "Confidence label": confidence_label_for_verified_contact(contact),
                "Sequence gate": verified_contact_gate(contact)["gate"],
                "Verified age": verified_contact_gate(contact)["age"],
                "Evidence grade": verified_contact_gate(contact)["evidence_grade"],
                "Name": contact.full_name,
                "Title": contact.title,
                "Email": contact.email,
                "Phone": contact.phone,
                "LinkedIn URL": contact.linkedin_url,
                "Source URL": contact.source_url,
                "Status": contact.verification_status,
                "Verified at": contact.verified_at,
                "Verified by": contact.verified_by,
                "Verification method": contact.verification_method,
                "SDR action": verified_contact_gate(contact)["action"],
                "Notes": contact.notes,
            }
            for contact in load_verified_contacts(company)
        ]
    )


def clean_import_value(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def short_text(value: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def days_since(value: str) -> int | None:
    parsed = parse_source_datetime(value)
    if parsed is None:
        return None
    return max((date.today() - parsed.date()).days, 0)


def age_bucket(value: str, fresh_days: int = 90, aging_days: int = 180) -> str:
    age_days = days_since(value)
    if age_days is None:
        return "Date not visible"
    if age_days <= fresh_days:
        return f"Fresh ({age_days}d)"
    if age_days <= aging_days:
        return f"Aging ({age_days}d)"
    return f"Stale ({age_days}d)"


def clean_sam_url(url: str) -> str:
    if not url or url.lower() == "null":
        return ""
    return url


def clean_company_domain(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    domain = parsed.netloc.lower().replace("www.", "")
    if not domain or "." not in domain:
        return ""
    return domain.split("/")[0]


def company_domain_from_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    domain = clean_company_domain(email.rsplit("@", 1)[-1])
    if not domain or domain in CONSUMER_EMAIL_DOMAINS:
        return ""
    return domain


def likely_company_domain_from_contacts(contacts: tuple[VerifiedContact, ...]) -> str:
    counts: dict[str, int] = {}
    for contact in contacts:
        domain = company_domain_from_email(contact.email)
        if domain:
            counts[domain] = counts.get(domain, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]


def business_domain_candidate(domain: str) -> bool:
    domain = clean_company_domain(domain)
    if not domain:
        return False
    if domain in CONSUMER_EMAIL_DOMAINS:
        return False
    if domain in NON_COMPANY_SITE_DOMAINS:
        return False
    return not any(domain.endswith(f".{blocked}") for blocked in NON_COMPANY_SITE_DOMAINS)


def normalize_company_name(company: str) -> str:
    text = re.sub(r"[^a-z0-9\s]", " ", (company or "").lower())
    words = [word for word in text.split() if word not in COMPANY_LEGAL_SUFFIXES]
    return " ".join(words)


def account_uei_values(account: Account) -> tuple[str, ...]:
    values = sorted({prospect.uei.strip().upper() for prospect in account.prospects if prospect.uei.strip()})
    return tuple(values)


def account_state_values(account: Account) -> tuple[str, ...]:
    values = sorted({prospect.state.strip().upper() for prospect in account.prospects if prospect.state.strip()})
    return tuple(values)


def account_address_values(account: Account) -> tuple[str, ...]:
    values = sorted({prospect.address.strip().lower() for prospect in account.prospects if prospect.address.strip()})
    return tuple(values)


def account_known_domain(account: Account, include_verified_contacts: bool = True) -> str:
    intel = st.session_state.get(public_intel_key(account.company))
    if isinstance(intel, CompanyIntel) and intel.website:
        domain = clean_company_domain(intel.website)
        if business_domain_candidate(domain):
            return domain
    if not include_verified_contacts:
        return ""
    return likely_company_domain_from_contacts(load_verified_contacts(account.company))


def split_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in re.split(r"\s+", full_name.strip()) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def import_verified_contacts_csv(uploaded_file: object, default_company: str) -> int:
    df = pd.read_csv(uploaded_file)
    normalized = {column.lower().strip(): column for column in df.columns}
    count = 0
    for _, row in df.iterrows():
        company = clean_import_value(row.get(normalized.get("company", ""), "")) or default_company
        full_name = clean_import_value(row.get(normalized.get("full_name", normalized.get("name", "")), ""))
        if not company or not full_name:
            continue
        save_verified_contact(
            company=company,
            full_name=full_name,
            title=clean_import_value(row.get(normalized.get("title", ""), "")),
            email=clean_import_value(row.get(normalized.get("email", ""), "")),
            phone=clean_import_value(row.get(normalized.get("phone", ""), "")),
            linkedin_url=clean_import_value(row.get(normalized.get("linkedin_url", normalized.get("linkedin", "")), "")),
            source_url=clean_import_value(row.get(normalized.get("source_url", normalized.get("source", "")), "")),
            source_type=clean_import_value(row.get(normalized.get("source_type", ""), "")) or "CSV import",
            verification_status=clean_import_value(row.get(normalized.get("verification_status", normalized.get("status", "")), "")) or "Imported for verification",
            notes=clean_import_value(row.get(normalized.get("notes", ""), "")),
            verified_by=clean_import_value(row.get(normalized.get("verified_by", normalized.get("reviewer", "")), "")),
            verification_method=clean_import_value(row.get(normalized.get("verification_method", normalized.get("method", "")), "")),
        )
        count += 1
    return count


def save_crm_activity(
    company: str,
    activity_type: str,
    contact_name: str,
    subject: str,
    outcome: str,
    notes: str,
    due_date: str,
    completed: bool = False,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "company": company.strip(),
        "activity_type": activity_type.strip(),
        "contact_name": contact_name.strip(),
        "subject": subject.strip(),
        "outcome": outcome.strip(),
        "notes": notes.strip(),
        "due_date": due_date.strip(),
        "completed": bool(completed),
        "created_at": now,
        "updated_at": now,
    }
    if supabase_enabled():
        try:
            supabase_insert("crm_activities", payload)
            return
        except requests.RequestException as exc:
            storage_warning(f"Supabase activity save failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO crm_activities (
                company, activity_type, contact_name, subject, outcome, notes,
                due_date, completed, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["company"],
                payload["activity_type"],
                payload["contact_name"],
                payload["subject"],
                payload["outcome"],
                payload["notes"],
                payload["due_date"],
                int(bool(payload["completed"])),
                payload["created_at"],
                payload["updated_at"],
            ),
        )


def load_crm_activities(company: str, limit: int = 50) -> tuple[CrmActivity, ...]:
    if supabase_enabled():
        try:
            rows = supabase_select(
                "crm_activities",
                {
                    "select": "*",
                    "company": f"eq.{company}",
                    "order": "completed.asc,due_date.asc,created_at.desc",
                    "limit": str(limit),
                },
            )
            return tuple(
                CrmActivity(
                    id=int(row.get("id") or 0),
                    company=str(row.get("company") or ""),
                    activity_type=str(row.get("activity_type") or ""),
                    contact_name=str(row.get("contact_name") or ""),
                    subject=str(row.get("subject") or ""),
                    outcome=str(row.get("outcome") or ""),
                    notes=str(row.get("notes") or ""),
                    due_date=str(row.get("due_date") or ""),
                    completed=bool(row.get("completed", False)),
                    created_at=str(row.get("created_at") or ""),
                    updated_at=str(row.get("updated_at") or ""),
                )
                for row in rows
            )
        except requests.RequestException as exc:
            storage_warning(f"Supabase activity read failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM crm_activities
            WHERE company = ?
            ORDER BY completed ASC, COALESCE(due_date, '') ASC, created_at DESC
            LIMIT ?
            """,
            (company, limit),
        ).fetchall()
    return tuple(
        CrmActivity(
            id=int(row["id"]),
            company=str(row["company"] or ""),
            activity_type=str(row["activity_type"] or ""),
            contact_name=str(row["contact_name"] or ""),
            subject=str(row["subject"] or ""),
            outcome=str(row["outcome"] or ""),
            notes=str(row["notes"] or ""),
            due_date=str(row["due_date"] or ""),
            completed=bool(row["completed"]),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )
        for row in rows
    )


def update_crm_activity_completed(activity_id: int, completed: bool) -> None:
    if supabase_enabled():
        try:
            supabase_patch(
                "crm_activities",
                activity_id,
                {"completed": bool(completed), "updated_at": datetime.now().isoformat(timespec="seconds")},
            )
            return
        except requests.RequestException as exc:
            storage_warning(f"Supabase activity update failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        connection.execute(
            """
            UPDATE crm_activities
            SET completed = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(completed), datetime.now().isoformat(timespec="seconds"), activity_id),
        )


def delete_crm_activity(activity_id: int) -> None:
    if supabase_enabled():
        try:
            supabase_delete("crm_activities", activity_id)
            return
        except requests.RequestException as exc:
            storage_warning(f"Supabase activity delete failed, using SQLite fallback: {exc}")
    with db_connect() as connection:
        connection.execute("DELETE FROM crm_activities WHERE id = ?", (activity_id,))


def crm_activities_dataframe(company: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ID": activity.id,
                "Done": activity.completed,
                "Due date": activity.due_date,
                "Type": activity.activity_type,
                "Contact": activity.contact_name,
                "Subject": activity.subject,
                "Outcome": activity.outcome,
                "Notes": activity.notes,
                "Created": activity.created_at,
            }
            for activity in load_crm_activities(company)
        ]
    )


def parse_prospect(row: dict) -> Prospect:
    return Prospect(
        award_id=str(row.get("Award ID") or ""),
        company=str(row.get("Recipient Name") or "Unknown recipient").strip(),
        uei=str(row.get("Recipient UEI") or ""),
        amount=float(row.get("Award Amount") or 0),
        base_obligation_date=str(row.get("Base Obligation Date") or ""),
        last_modified_date=str(row.get("Last Modified Date") or ""),
        start_date=str(row.get("Start Date") or ""),
        end_date=str(row.get("End Date") or ""),
        awarding_agency=str(row.get("Awarding Agency") or ""),
        awarding_sub_agency=str(row.get("Awarding Sub Agency") or ""),
        funding_agency=str(row.get("Funding Agency") or ""),
        funding_sub_agency=str(row.get("Funding Sub Agency") or ""),
        description=str(row.get("Description") or "").strip(),
        naics_code=nested_field(row, "NAICS", "code"),
        naics_description=nested_field(row, "NAICS", "description"),
        psc_code=nested_field(row, "PSC", "code"),
        psc_description=nested_field(row, "PSC", "description"),
        address=str(row.get("recipient_location_address_line1") or ""),
        city=str(row.get("recipient_location_city_name") or "").title(),
        state=str(row.get("recipient_location_state_code") or ""),
        country=str(row.get("recipient_location_country_name") or "").title(),
    )


def money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,.0f}"


def search_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"


def linkedin_url(company: str, persona: str = "") -> str:
    query = f'site:linkedin.com/in "{company}" {persona}'.strip()
    return search_url(query)


def target_search_url(company: str, title: str) -> str:
    return search_url(f'site:linkedin.com/in "{company}" "{title}"')


def public_links(prospect: Prospect) -> dict[str, str]:
    company = prospect.company
    return {
        "Company site": search_url(f'"{company}" official website'),
        "Leadership": search_url(f'"{company}" leadership government contracts'),
        "LinkedIn contacts": linkedin_url(company, "capture proposal contracts"),
        "LinkedIn company": search_url(f'site:linkedin.com/company "{company}"'),
        "LinkedIn jobs": search_url(f'site:linkedin.com/jobs "{company}" government'),
        "Contracts contact": search_url(f'"{company}" contracts manager email government'),
        "Proposal team": search_url(f'"{company}" proposal manager capture manager'),
        "News": search_url(f'"{company}" "{prospect.award_id}" contract award'),
        "USAspending": search_url(f'site:usaspending.gov "{prospect.award_id}"'),
        "SAM.gov": search_url(f'site:sam.gov "{prospect.award_id}" "{company}"'),
    }


def html_escape(value: object) -> str:
    return escape(str(value or ""), quote=True)


def anchor_slug(value: str) -> str:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1-\2", value or "")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", spaced.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def normalize_search_result_url(href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])
    return href


def url_domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def domain_root(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def fetchable_public_url(url: str) -> bool:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if parsed.scheme not in {"http", "https"} or not domain:
        return False
    if domain in BLOCKED_FETCH_DOMAINS or domain.replace("www.", "") in BLOCKED_FETCH_DOMAINS:
        return False
    if any(domain.endswith(f".{blocked}") for blocked in BLOCKED_FETCH_DOMAINS):
        return False
    return True


def clean_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def source_links_from_html(html: str, base_url: str, limit: int = 5) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    root = url_domain(base_url)
    keywords = ("about", "leadership", "team", "management", "contact", "news", "contract", "federal")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True).lower()
        href = anchor.get("href") or ""
        url = urljoin(base_url, href)
        if url_domain(url) != root:
            continue
        if not any(keyword in f"{label} {url.lower()}" for keyword in keywords):
            continue
        if url not in links and fetchable_public_url(url):
            links.append(url)
        if len(links) >= limit:
            break
    return links


@st.cache_data(ttl=86400, show_spinner=False)
def search_web_results(
    query: str,
    max_results: int = 5,
    allowed_domains: tuple[str, ...] = tuple(),
    fetchable_only: bool = True,
) -> tuple[WebSearchResult, ...]:
    try:
        response = requests.get(PUBLIC_SEARCH_URL, params={"q": query}, headers=REQUEST_HEADERS, timeout=SEARCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException:
        return tuple()

    soup = BeautifulSoup(response.text, "html.parser")
    results: list[WebSearchResult] = []
    seen: set[str] = set()
    for result in soup.select(".result"):
        anchor = result.select_one("a.result__a")
        if not anchor:
            continue
        url = normalize_search_result_url(anchor.get("href", ""))
        if not url or url in seen:
            continue
        domain = url_domain(url)
        if allowed_domains and not any(domain == allowed or domain.endswith(f".{allowed}") for allowed in allowed_domains):
            continue
        if fetchable_only and not fetchable_public_url(url):
            continue
        snippet = result.select_one(".result__snippet")
        results.append(
            WebSearchResult(
                title=anchor.get_text(" ", strip=True),
                url=url,
                snippet=snippet.get_text(" ", strip=True) if snippet else "",
                query=query,
            )
        )
        seen.add(url)
        if len(results) >= max_results:
            break
    return tuple(results)


@st.cache_data(ttl=86400, show_spinner=False)
def search_public_web(query: str, max_results: int = 5) -> tuple[str, ...]:
    results = search_web_results(query, max_results=max_results, fetchable_only=True)
    return tuple(result.url for result in results)


@st.cache_data(ttl=86400, show_spinner=False)
def discover_company_domain_from_web(company: str) -> tuple[str, str]:
    queries = [
        f'"{company}" official website',
        f'"{company}" company website',
    ]
    for query in queries:
        for result in search_web_results(query, max_results=5, fetchable_only=True):
            domain = clean_company_domain(result.url)
            if business_domain_candidate(domain):
                return domain, domain_root(result.url)
    return "", ""


def suggested_hubspot_domain(
    account: Account,
    intel: CompanyIntel | None,
    verified_contacts: tuple[VerifiedContact, ...],
) -> tuple[str, str]:
    contact_domain = likely_company_domain_from_contacts(verified_contacts)
    if contact_domain:
        return contact_domain, "verified contact email"

    if isinstance(intel, CompanyIntel) and intel.website:
        domain = clean_company_domain(intel.website)
        if business_domain_candidate(domain):
            return domain, "company intel website"

    return "", "not found yet"


def resolve_hubspot_domain(
    account: Account,
    intel: CompanyIntel | None,
    verified_contacts: tuple[VerifiedContact, ...],
    manual_domain: str = "",
    allow_public_search: bool = False,
) -> tuple[str, str, str]:
    manual = clean_company_domain(manual_domain)
    if manual:
        return manual, "manual override", ""
    suggested_domain, suggested_source = suggested_hubspot_domain(account, intel, verified_contacts)
    if suggested_domain:
        return suggested_domain, suggested_source, ""
    if allow_public_search:
        discovered_domain, discovered_website = discover_company_domain_from_web(account.company)
        if discovered_domain:
            return discovered_domain, "public website search", discovered_website
    return "", "not found yet", ""


@st.cache_data(ttl=86400, show_spinner=False)
def search_linkedin_web(query: str, max_results: int = 5) -> tuple[WebSearchResult, ...]:
    return search_web_results(
        query,
        max_results=max_results,
        allowed_domains=("linkedin.com",),
        fetchable_only=False,
    )


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_public_page(url: str) -> tuple[str, str]:
    if not fetchable_public_url(url):
        return "", ""
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=PAGE_TIMEOUT_SECONDS, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException:
        return "", ""
    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type and "text" not in content_type:
        return "", response.url
    return response.text[:400_000], response.url


def extract_emails(text: str) -> list[str]:
    emails = re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, flags=re.IGNORECASE)
    cleaned = []
    for email in emails:
        email = email.strip(".,;:()[]{}<>").lower()
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if email not in cleaned:
            cleaned.append(email)
    return cleaned[:12]


def extract_phones(text: str) -> list[str]:
    phones = re.findall(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}", text)
    cleaned = []
    for phone in phones:
        normalized = re.sub(r"\s+", " ", phone).strip(" .,-")
        digits = re.sub(r"\D", "", normalized)
        if len(digits) in {10, 11} and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned[:10]


def title_from_line(line: str) -> str:
    lower = line.lower()
    title_map = {
        "chief executive": "Chief Executive Officer",
        "ceo": "Chief Executive Officer",
        "president": "President",
        "founder": "Founder",
        "business development": "Business Development Leader",
        "capture": "Capture Leader",
        "proposal": "Proposal Leader",
        "contracts": "Contracts Leader",
        "contracting": "Contracts Leader",
        "program manager": "Program Manager",
        "program director": "Program Director",
        "operations": "Operations Leader",
        "vice president": "Vice President",
        "vp": "Vice President",
        "director": "Director",
        "chief technology": "Chief Technology Officer",
        "cto": "Chief Technology Officer",
    }
    for keyword, title in title_map.items():
        if keyword in lower:
            return title
    return ""


def reason_for_title(title: str) -> str:
    lower = title.lower()
    if any(term in lower for term in ["capture", "business development", "growth", "president", "chief executive", "founder"]):
        return "Likely cares about converting the new award into follow-on pipeline and repeatable capture process."
    if "proposal" in lower:
        return "Likely feels the pain around compliance matrices, reusable past performance, and proposal drafting speed."
    if "contract" in lower:
        return "Likely owns award records, modifications, option years, and contract evidence."
    if any(term in lower for term in ["technology", "program", "operations"]):
        return "Likely cares about delivery evidence, kickoff organization, and program execution."
    return "Public source suggests this person may be relevant to GovDash evaluation or referral."


def likely_person_name(value: str) -> bool:
    banned_words = {
        "United States",
        "Privacy Policy",
        "Terms Conditions",
        "Contact Us",
        "About Us",
        "Read More",
        "Learn More",
        "Press Release",
        "Small Business",
        "Department Defense",
        "Federal Government",
        "Contract Award",
    }
    if value in banned_words:
        return False
    parts = value.split()
    if not (2 <= len(parts) <= 4):
        return False
    return all(part[:1].isupper() and len(part.strip("., ")) > 1 for part in parts)


def extract_contacts_from_text(text: str, source_url: str) -> list[PublicContact]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    contacts: list[PublicContact] = []
    for line in lines:
        compact = re.sub(r"\s+", " ", line)
        lower = compact.lower()
        if not any(keyword in lower for keyword in CONTACT_TITLE_KEYWORDS):
            continue
        if len(compact) > 280:
            compact = compact[:280]
        title = title_from_line(compact)
        names = re.findall(r"\b([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){1,3})\b", compact)
        emails = extract_emails(compact)
        phones = extract_phones(compact)
        for name in names[:3]:
            if not likely_person_name(name):
                continue
            confidence = 70
            if emails:
                confidence += 15
            if phones:
                confidence += 10
            contacts.append(
                PublicContact(
                    full_name=name,
                    title=title or "Potential leadership/contact role",
                    email=emails[0] if emails else "",
                    phone=phones[0] if phones else "",
                    source_url=source_url,
                    evidence=compact,
                    confidence=min(confidence, 95),
                    recommended_reason=reason_for_title(title),
                )
            )

    for email in extract_emails(text):
        prefix = email.split("@", 1)[0].lower()
        if prefix in GENERIC_EMAIL_PREFIXES:
            title = "Company public inbox"
            confidence = 45
        else:
            title = "Public email contact"
            confidence = 55
        contacts.append(
            PublicContact(
                full_name="",
                title=title,
                email=email,
                phone="",
                source_url=source_url,
                evidence=f"Public email found on source page: {email}",
                confidence=confidence,
                recommended_reason="Useful as a fallback route if no named contact is verified yet.",
            )
        )

    return contacts


def dedupe_contacts(contacts: list[PublicContact]) -> tuple[PublicContact, ...]:
    best: dict[str, PublicContact] = {}
    for contact in contacts:
        key = contact.email.lower() or f"{contact.full_name.lower()}|{contact.title.lower()}|{url_domain(contact.source_url)}"
        if not key.strip("|"):
            continue
        if key not in best or contact.confidence > best[key].confidence:
            best[key] = contact
    return tuple(sorted(best.values(), key=lambda item: (item.confidence, bool(item.full_name), bool(item.email)), reverse=True)[:12])


def clean_linkedin_title(title: str) -> str:
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s+-\s+LinkedIn.*$", "", title, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", title)


def linkedin_role_queries(company: str) -> list[str]:
    roles = [
        "capture manager",
        "proposal manager",
        "business development",
        "contracts manager",
        "program manager",
        "president",
        "CEO",
        "chief technology officer",
    ]
    queries = [f'site:linkedin.com/company "{company}"', f'site:linkedin.com/jobs "{company}" government contract']
    queries.extend(f'site:linkedin.com/in "{company}" "{role}"' for role in roles)
    return queries


def linkedin_contacts_from_results(results: tuple[WebSearchResult, ...]) -> tuple[PublicContact, ...]:
    contacts: list[PublicContact] = []
    for result in results:
        lower_url = result.url.lower()
        if "linkedin.com/in/" not in lower_url:
            continue

        cleaned_title = clean_linkedin_title(result.title)
        title_parts = [part.strip(" -–|") for part in re.split(r"\s[-–|]\s", cleaned_title) if part.strip(" -–|")]
        name = title_parts[0] if title_parts and likely_person_name(title_parts[0]) else ""
        role_text = " ".join(title_parts[1:3]) or result.snippet
        role = title_from_line(role_text) or (title_parts[1] if len(title_parts) > 1 else "LinkedIn profile signal")
        evidence = " ".join(part for part in [cleaned_title, result.snippet] if part).strip()
        if not name and not any(keyword in evidence.lower() for keyword in CONTACT_TITLE_KEYWORDS):
            continue

        confidence = 68 if name and role != "LinkedIn profile signal" else 52
        contacts.append(
            PublicContact(
                full_name=name,
                title=role,
                email="",
                phone="",
                source_url=result.url,
                evidence=evidence[:300],
                confidence=confidence,
                recommended_reason=reason_for_title(role),
            )
        )
    return dedupe_contacts(contacts)


def linkedin_contacts_from_page(html: str, source_url: str) -> tuple[tuple[PublicContact, ...], tuple[WebSearchResult, ...]]:
    soup = BeautifulSoup(html, "html.parser")
    contacts: list[PublicContact] = []
    signals: list[WebSearchResult] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        if "linkedin.com/in/" not in href.lower():
            continue
        profile_url = urljoin(source_url, href)
        if profile_url in seen:
            continue
        seen.add(profile_url)

        anchor_text = anchor.get_text(" ", strip=True)
        parent_text = anchor.parent.get_text(" ", strip=True) if anchor.parent else anchor_text
        evidence_text = re.sub(r"\s+", " ", parent_text or anchor_text or profile_url).strip()
        names = re.findall(r"\b([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){1,3})\b", evidence_text)
        name = ""
        if likely_person_name(anchor_text):
            name = anchor_text
        else:
            for candidate in names:
                if likely_person_name(candidate):
                    name = candidate
                    break

        role = title_from_line(evidence_text) or "LinkedIn profile linked from public company page"
        confidence = 74 if name and role != "LinkedIn profile linked from public company page" else 58
        contacts.append(
            PublicContact(
                full_name=name,
                title=role,
                email="",
                phone="",
                source_url=profile_url,
                evidence=f"LinkedIn profile link found on {source_url}: {evidence_text[:220]}",
                confidence=confidence,
                recommended_reason=reason_for_title(role),
            )
        )
        signals.append(
            WebSearchResult(
                title=anchor_text or name or "LinkedIn profile link",
                url=profile_url,
                snippet=evidence_text[:240],
                query=f"LinkedIn link found on {source_url}",
            )
        )

    return dedupe_contacts(contacts), tuple(signals)


def linkedin_signals_dataframe(intel: CompanyIntel) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Signal": "Profile" if "linkedin.com/in/" in signal.url.lower() else "Company/Jobs",
                "Title": signal.title,
                "Snippet": signal.snippet,
                "LinkedIn URL": signal.url,
                "Search query": signal.query,
            }
            for signal in intel.linkedin_signals
        ]
    )


def signal_source(url: str) -> str:
    domain = url_domain(url)
    if "linkedin.com" in domain:
        return "LinkedIn public signal"
    if "youtube.com" in domain or "youtu.be" in domain:
        return "YouTube/interview"
    if "spotify.com" in domain or "apple.com" in domain or "podcasts" in domain:
        return "Podcast"
    if any(source in domain for source in ["prnewswire", "globenewswire", "businesswire", "newswire"]):
        return "Press release wire"
    if any(source in domain for source in ["defense.gov", "army.mil", "navy.mil", "af.mil", "spaceforce.mil", "sam.gov", "usaspending.gov"]):
        return "Government source"
    return domain or "Public web"


def classify_signal(title: str, snippet: str, url: str) -> str:
    text = " ".join([title, snippet, url]).lower()
    if "linkedin.com" in text and any(term in text for term in ["posts", "feed/update", "activity"]):
        return "LinkedIn announcement"
    if "linkedin.com/jobs" in text or any(term in text for term in ["hiring", "job", "careers", "recruiting"]):
        return "Hiring or growth"
    if any(term in text for term in ["podcast", "interview", "conversation with", "appeared on", "episode"]):
        return "Podcast or interview"
    if any(term in text for term in ["press release", "announces", "announced", "announcement", "launches", "unveils"]):
        return "Announcement"
    if any(term in text for term in ["partnership", "partners with", "teaming", "alliance", "collaboration"]):
        return "Partnership"
    if any(term in text for term in ["webinar", "conference", "event", "speaking", "panel"]):
        return "Event or webinar"
    if any(term in text for term in ["award", "contract", "task order", "idiq", "bpa"]):
        return "Award or contract"
    if any(term in text for term in ["ceo", "president", "chief", "appointed", "joins as"]):
        return "Leadership"
    return "Account intel"


def recency_hint(text: str) -> str:
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December"
    month_match = re.search(rf"\b(?:{months})\.?\s+\d{{1,2}},?\s+20\d{{2}}\b", text, flags=re.IGNORECASE)
    if month_match:
        return month_match.group(0)
    year_match = re.search(r"\b20(?:2[4-9]|3[0-9])\b", text)
    if year_match:
        return year_match.group(0)
    if any(term in text.lower() for term in ["today", "yesterday", "this week", "this month", "recent", "new"]):
        return "Recent wording"
    return "Verify date"


def call_angle_for_signal(signal_type: str, title: str, snippet: str, company: str) -> str:
    text = " ".join([title, snippet]).lower()
    if signal_type == "Podcast or interview":
        return f"Reference the interview and ask how {company} is turning that public thought leadership into repeatable capture/proposal execution."
    if signal_type == "LinkedIn announcement":
        return "Use the public LinkedIn update as the personal opener, then connect it to pipeline, proposal, or contract execution priorities."
    if signal_type == "Hiring or growth":
        return "Ask whether growth is creating proposal volume, capture handoff, or contract-documentation strain."
    if signal_type == "Partnership":
        return "Ask how they coordinate partners, subcontractors, and reusable evidence across pursuits and delivery."
    if signal_type == "Event or webinar":
        return "Reference the event topic and ask whether the team has a repeatable workflow for turning market insight into qualified pursuits."
    if signal_type == "Leadership":
        return "Use the leadership change as a reason to ask about growth process, proposal operations, and visibility into federal pipeline."
    if signal_type == "Announcement":
        return "Reference the announcement and ask what follow-on opportunities or proposal workload it creates."
    if signal_type == "Award or contract" and "contract" in text:
        return "Tie the public contract signal to kickoff, past-performance reuse, modifications, option years, and follow-on capture."
    return "Use this as a relevant business trigger before mentioning the newly reported contract."


def account_signal_queries(company: str, award_id: str) -> list[str]:
    return [
        f'site:linkedin.com/posts "{company}"',
        f'site:linkedin.com/feed/update "{company}"',
        f'site:linkedin.com/company "{company}" "posts"',
        f'"{company}" "podcast"',
        f'"{company}" "interview"',
        f'"{company}" "CEO" "podcast"',
        f'"{company}" "press release"',
        f'"{company}" announcement',
        f'"{company}" partnership',
        f'"{company}" webinar OR conference',
        f'"{company}" hiring government contracts',
        f'"{company}" "{award_id}"',
    ]


def signal_from_search_result(result: WebSearchResult, company: str) -> AccountSignal:
    signal_type = classify_signal(result.title, result.snippet, result.url)
    return AccountSignal(
        signal_type=signal_type,
        title=result.title or result.url,
        url=result.url,
        snippet=result.snippet,
        source=signal_source(result.url),
        recency_hint=recency_hint(" ".join([result.title, result.snippet])),
        call_angle=call_angle_for_signal(signal_type, result.title, result.snippet, company),
        search_query=result.query,
    )


def account_signals_from_page_text(company: str, page_text: str, source_url: str) -> list[AccountSignal]:
    signals: list[AccountSignal] = []
    lines = [re.sub(r"\s+", " ", line).strip() for line in page_text.splitlines()]
    keywords = [
        "announces",
        "announced",
        "press release",
        "podcast",
        "interview",
        "webinar",
        "conference",
        "partnership",
        "hiring",
        "award",
        "contract",
        "appointed",
    ]
    for line in lines:
        if len(line) < 45 or len(line) > 260:
            continue
        lower = line.lower()
        if not any(keyword in lower for keyword in keywords):
            continue
        signal_type = classify_signal(line, "", source_url)
        signals.append(
            AccountSignal(
                signal_type=signal_type,
                title=line[:110],
                url=source_url,
                snippet=line,
                source=signal_source(source_url),
                recency_hint=recency_hint(line),
                call_angle=call_angle_for_signal(signal_type, line, "", company),
                search_query=f"Signal extracted from {source_url}",
            )
        )
        if len(signals) >= 4:
            break
    return signals


def dedupe_account_signals(signals: list[AccountSignal]) -> tuple[AccountSignal, ...]:
    priority = {
        "LinkedIn announcement": 95,
        "Podcast or interview": 92,
        "Announcement": 88,
        "Partnership": 82,
        "Leadership": 78,
        "Hiring or growth": 74,
        "Event or webinar": 72,
        "Award or contract": 70,
        "Account intel": 50,
    }
    best: dict[str, AccountSignal] = {}
    for signal in signals:
        key = signal.url.lower() or signal.title.lower()
        if key not in best or priority.get(signal.signal_type, 0) > priority.get(best[key].signal_type, 0):
            best[key] = signal
    return tuple(
        sorted(
            best.values(),
            key=lambda item: (priority.get(item.signal_type, 0), item.recency_hint != "Verify date"),
            reverse=True,
        )[:12]
    )


def industry_category(naics_description: str, psc_description: str, description: str, psc_code: str = "") -> str:
    text = " ".join([naics_description, psc_description, description, psc_code]).lower()
    if any(term in text for term in ["cyber", "software", "cloud", "data", "network", "telecom", "information technology", "computer", "digital"]):
        return "IT, cyber, and digital services"
    if any(term in text for term in ["construction", "architect", "engineering", "facilities", "maintenance", "repair", "utilities"]):
        return "Construction, engineering, and facilities"
    if any(term in text for term in ["aircraft", "missile", "weapon", "defense", "aerospace", "satellite", "ship", "tactical"]):
        return "Defense, aerospace, and mission systems"
    if any(term in text for term in ["medical", "health", "pharma", "laboratory", "clinical", "hospital", "biolog"]):
        return "Healthcare, life sciences, and labs"
    if any(term in text for term in ["logistics", "transport", "warehouse", "supply", "freight", "material", "equipment"]):
        return "Logistics, supply chain, and products"
    if any(term in text for term in ["research", "development", "scientific", "analysis", "professional", "management", "consulting"]):
        return "Professional, research, and advisory services"
    if any(term in text for term in ["security", "guard", "protective", "investigation"]):
        return "Security and protective services"
    return "General government contractor"


def pain_point_from_signal(signal: AccountSignal, industry: str) -> PainPoint | None:
    text = " ".join([signal.signal_type, signal.title, signal.snippet]).lower()
    if any(term in text for term in ["hiring", "recruiting", "jobs", "talent", "staffing"]):
        return PainPoint(
            industry,
            "Staffing or delivery capacity pressure",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "High",
            "Use GovDash to standardize capture/proposal handoffs and reduce ramp time for new staff or distributed teams.",
            "How are you keeping capture, proposal, and delivery process consistent as the team grows or shifts resources?",
        )
    if any(term in text for term in ["partnership", "teaming", "subcontract", "alliance", "collaboration"]):
        return PainPoint(
            industry,
            "Partner and subcontractor coordination complexity",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "Medium",
            "Position GovDash as the shared workspace for partner evidence, requirements, assignments, and reusable proposal material.",
            "When partners or subs are involved, where do requirements, evidence, and proposal inputs usually get tracked?",
        )
    if any(term in text for term in ["webinar", "conference", "event", "speaking", "panel", "thought leadership"]):
        return PainPoint(
            industry,
            "Market knowledge is not automatically converted into capture action",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "Medium",
            "Use GovDash to turn market signals into qualified opportunities, capture notes, proposal outlines, and reusable evidence.",
            "How does your team turn event or market insight into actual capture plans and proposal assets?",
        )
    if any(term in text for term in ["podcast", "interview", "conversation", "episode"]):
        return PainPoint(
            industry,
            "Executive priorities may not be translated into repeatable operating process",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "Medium",
            "Connect the public executive narrative to repeatable capture/proposal workflows inside GovDash.",
            "I heard the public discussion around this priority; how is that showing up in your pursuit and proposal process?",
        )
    if any(term in text for term in ["cyber", "cmmc", "security", "compliance", "audit", "certification", "authorization"]):
        return PainPoint(
            industry,
            "Compliance, security, or audit evidence burden",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "High",
            "Show GovDash organizing compliance evidence, owner assignments, proposal requirements, and reusable security language.",
            "Where do compliance evidence, security narratives, and proposal requirements live today?",
        )
    if any(term in text for term in ["modernization", "digital", "cloud", "data", "ai", "automation", "software"]):
        return PainPoint(
            industry,
            "Modernization work creates fast-changing requirements and evidence needs",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "High",
            "Show GovDash as a way to keep requirements, technical narratives, and proof points synchronized across pursuits.",
            "How do capture and delivery teams keep technical proof points reusable as requirements evolve?",
        )
    if any(term in text for term in ["award", "contract", "task order", "idiq", "bpa", "option"]):
        return PainPoint(
            industry,
            "Multiple awards or vehicles can create follow-on capture and contract-evidence sprawl",
            "Company/public signal",
            signal.title,
            signal.url,
            signal.source,
            signal.snippet,
            "Medium",
            "Use GovDash to connect award records, past performance, option-year evidence, and follow-on opportunity research.",
            "How are you reusing this work as past performance and evidence for similar opportunities?",
        )
    return None


def pain_points_from_page_text(company: str, industry: str, page_text: str, source_url: str) -> list[PainPoint]:
    pain_keywords = [
        "challenge",
        "challenges",
        "compliance",
        "audit",
        "cmmc",
        "cybersecurity",
        "staffing",
        "hiring",
        "supply chain",
        "quality",
        "modernization",
        "transition",
        "implementation",
        "delivery",
        "subcontractor",
        "partner",
        "proposal",
        "capture",
        "contract management",
        "risk",
    ]
    points: list[PainPoint] = []
    for line in [re.sub(r"\s+", " ", item).strip() for item in page_text.splitlines()]:
        if len(line) < 55 or len(line) > 280:
            continue
        lower = line.lower()
        if not any(keyword in lower for keyword in pain_keywords):
            continue
        pseudo_signal = AccountSignal(
            "Account intel",
            line[:120],
            source_url,
            line,
            signal_source(source_url),
            recency_hint(line),
            call_angle_for_signal("Account intel", line, "", company),
            f"Pain evidence extracted from {source_url}",
        )
        point = pain_point_from_signal(pseudo_signal, industry)
        if point is None:
            point = PainPoint(
                industry,
                "Operational complexity surfaced in public company content",
                "Company/public page",
                line[:120],
                source_url,
                signal_source(source_url),
                line,
                "Medium",
                "Use GovDash to organize requirements, ownership, source evidence, proposal inputs, and delivery proof in one account workspace.",
                "Where is this workflow managed today, and what still depends on spreadsheets, shared drives, or ad hoc notes?",
            )
        points.append(point)
        if len(points) >= 5:
            break
    return points


def industry_benchmark_pain_points(industry: str, company: str) -> list[PainPoint]:
    benchmarks = {
        "IT, cyber, and digital services": [
            (
                "Security/compliance evidence and technical narrative reuse",
                "CMMC cybersecurity compliance government contractors proposal evidence",
                "How much time does the team spend recreating security, compliance, or technical evidence for each pursuit?",
            ),
            (
                "Fast-changing cloud/data/AI requirements across capture and delivery",
                "federal IT modernization contractor proposal requirements cloud data AI challenges",
                "How do capture and delivery teams keep technical win themes and proof points current?",
            ),
        ],
        "Construction, engineering, and facilities": [
            (
                "Field documentation, subcontractor coordination, and modification evidence",
                "federal construction contractor subcontractor documentation modifications compliance challenges",
                "How are field notes, subcontractor inputs, modifications, and option-year evidence organized?",
            ),
            (
                "Past-performance proof scattered across projects",
                "federal construction past performance documentation proposal challenges",
                "How quickly can the team turn project proof into proposal-ready past performance?",
            ),
        ],
        "Defense, aerospace, and mission systems": [
            (
                "Complex technical requirements and mission evidence reuse",
                "defense contractor technical proposal requirements past performance evidence challenges",
                "Where do technical proof points, mission outcomes, and compliance evidence live today?",
            ),
            (
                "Partner/team coordination on complex pursuits",
                "defense contractor teaming subcontractor proposal coordination challenges",
                "How do primes, subs, and internal teams coordinate proposal inputs and delivery evidence?",
            ),
        ],
        "Healthcare, life sciences, and labs": [
            (
                "Regulated documentation and audit-ready evidence",
                "federal healthcare contractor compliance documentation audit evidence challenges",
                "How are regulated requirements and audit evidence tracked across proposal and delivery?",
            ),
            (
                "Specialized staffing and continuity pressure",
                "healthcare government contractor staffing continuity contract delivery challenges",
                "How does the team preserve continuity when specialized staff or sites change?",
            ),
        ],
        "Logistics, supply chain, and products": [
            (
                "Supply chain, delivery proof, and modification tracking",
                "government contractor supply chain delivery documentation modification challenges",
                "How are delivery proof, supplier issues, and contract modifications captured for follow-on work?",
            ),
            (
                "Price, availability, and compliance pressure",
                "federal contractor supply availability pricing compliance proposal challenges",
                "How does the team keep price/availability assumptions and compliance evidence reusable?",
            ),
        ],
        "Professional, research, and advisory services": [
            (
                "Knowledge capture and reusable proposal content",
                "professional services government contractor proposal knowledge management capture challenges",
                "How does the team reuse expertise, resumes, case studies, and win themes across proposals?",
            ),
            (
                "Capture handoff and volume management",
                "federal consulting contractor capture proposal operations challenges",
                "Where do capture notes become proposal outlines, matrices, and review tasks?",
            ),
        ],
    }
    rows = benchmarks.get(industry, benchmarks["Professional, research, and advisory services"])
    return [
        PainPoint(
            industry,
            pain,
            "Industry benchmark to verify",
            f"Research this public industry pattern for {company}",
            search_url(query),
            "Live public-source search",
            "No company-specific pain evidence was found in the quick scan. Use this as a researched hypothesis and verify on the call.",
            "Medium",
            "Use GovDash to centralize capture intelligence, proposal artifacts, compliance evidence, and contract proof around the account.",
            question,
        )
        for pain, query, question in rows
    ]


def dedupe_pain_points(points: list[PainPoint]) -> tuple[PainPoint, ...]:
    severity_rank = {"High": 3, "Medium": 2, "Low": 1}
    evidence_rank = {"Company/public signal": 4, "Company/public page": 3, "Industry benchmark to verify": 1}
    best: dict[str, PainPoint] = {}
    for point in points:
        key = point.pain_point.lower()
        if key not in best:
            best[key] = point
            continue
        current = best[key]
        if (evidence_rank.get(point.evidence_level, 0), severity_rank.get(point.severity, 0)) > (
            evidence_rank.get(current.evidence_level, 0),
            severity_rank.get(current.severity, 0),
        ):
            best[key] = point
    return tuple(
        sorted(
            best.values(),
            key=lambda point: (evidence_rank.get(point.evidence_level, 0), severity_rank.get(point.severity, 0)),
            reverse=True,
        )[:8]
    )


def pain_points_dataframe(intel: CompanyIntel) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Industry": point.industry,
                "Confidence label": confidence_label_for_pain(point),
                "Pain point": point.pain_point,
                "Evidence level": point.evidence_level,
                "Severity": point.severity,
                "Source": point.source,
                "Source URL": point.source_url,
                "Evidence": point.snippet,
                "GovDash angle": point.govdash_angle,
                "Discovery question": point.recommended_question,
            }
            for point in getattr(intel, "pain_points", tuple())
        ]
    )


def account_signals_dataframe(intel: CompanyIntel) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Type": signal.signal_type,
                "Confidence label": confidence_label_for_signal(signal),
                "Title": signal.title,
                "Source": signal.source,
                "Recency": signal.recency_hint,
                "Call angle": signal.call_angle,
                "URL": signal.url,
                "Snippet": signal.snippet,
            }
            for signal in getattr(intel, "account_signals", tuple())
        ]
    )


def sam_opportunities_dataframe(opportunities: tuple[SamOpportunity, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Match": sam_match_score(opportunity),
                "Confidence label": "Official source",
                "Title": opportunity.title,
                "Notice type": opportunity.notice_type,
                "Posted": opportunity.posted_date,
                "Solicitation": opportunity.solicitation_number,
                "Award number": opportunity.award_number,
                "Award amount": opportunity.award_amount,
                "Awardee": opportunity.awardee_name,
                "Awardee UEI": opportunity.awardee_uei,
                "Set-aside": opportunity.set_aside,
                "NAICS": opportunity.naics_code,
                "PSC": opportunity.classification_code,
                "Organization": opportunity.organization_path or " / ".join(
                    part for part in [opportunity.department, opportunity.subtier, opportunity.office] if part
                ),
                "Place of performance": opportunity.place_of_performance,
                "Government POC": opportunity.point_of_contact,
                "POC email": opportunity.poc_email,
                "POC phone": opportunity.poc_phone,
                "SAM.gov link": opportunity.ui_link,
                "Description URL": opportunity.description_url,
                "Match reason": opportunity.match_reason,
            }
            for opportunity in opportunities
        ]
    )


def fallback_call_intel_links(company: str) -> pd.DataFrame:
    rows = []
    for label, query in [
        ("LinkedIn posts", f'site:linkedin.com/posts "{company}"'),
        ("LinkedIn company updates", f'site:linkedin.com/company "{company}" posts'),
        ("Podcast interviews", f'"{company}" podcast interview'),
        ("Executive interviews", f'"{company}" CEO interview podcast'),
        ("Press releases", f'"{company}" press release announcement'),
        ("Events/webinars", f'"{company}" webinar conference government'),
        ("Hiring/growth", f'"{company}" hiring government contracts'),
    ]:
        rows.append({"Intel path": label, "Search URL": search_url(query)})
    return pd.DataFrame(rows)


def contact_matches_role(contact: PublicContact, role: str) -> bool:
    haystack = " ".join([contact.title, contact.evidence, contact.recommended_reason]).lower()
    role_text = role.lower()
    role_aliases = {
        "president/ceo or govcon practice lead": ["president", "chief executive", "ceo", "founder", "growth", "business development"],
        "vp/director of business development": ["business development", "growth", "vice president", "vp", "director"],
        "capture manager": ["capture"],
        "proposal manager": ["proposal"],
        "contracts manager": ["contract", "contracts", "contracting"],
        "cto/vp engineering or technical program lead": ["technology", "technical", "engineering", "cto", "program"],
        "program operations lead": ["program", "operations"],
    }
    aliases = role_aliases.get(role_text, [part for part in re.split(r"[/ ]+", role_text) if len(part) > 3])
    return any(alias in haystack for alias in aliases)


def contact_source_age(contact: PublicContact) -> str:
    evidence = " ".join([contact.evidence, contact.source_url])
    if "verified contact saved in crm" in evidence.lower():
        match = re.search(r"\d{4}-\d{2}-\d{2}", evidence)
        if match:
            parsed_dt = parse_source_datetime(match.group(0))
            if parsed_dt:
                age_days = (date.today() - parsed_dt.date()).days
                if age_days <= 180:
                    return "Fresh"
                if age_days <= 730:
                    return "Aging"
                return "Old"
        return "Fresh"
    parsed = recency_hint(evidence)
    if parsed == "Verify date":
        return "Date not visible"
    parsed_dt = parse_source_datetime(parsed)
    if parsed_dt:
        age_days = (date.today() - parsed_dt.date()).days
        if age_days <= 180:
            return "Fresh"
        if age_days <= 730:
            return "Aging"
        return "Old"
    if parsed in {"Recent wording"}:
        return "Likely recent"
    return parsed


def quality_for_contact(contact: PublicContact, target_role: str = "") -> ContactQuality:
    score = 0
    reasons: list[str] = []
    is_verified = "verified contact saved in crm" in contact.evidence.lower()

    if is_verified:
        score += 35
        reasons.append("Saved verified contact")

    if contact.full_name:
        score += 25
        reasons.append("Named person found")
    else:
        reasons.append("No named person")

    if target_role and contact_matches_role(contact, target_role):
        score += 25
        relevance = "Role match"
        reasons.append("Matches target role")
    elif any(keyword in " ".join([contact.title, contact.evidence]).lower() for keyword in CONTACT_TITLE_KEYWORDS):
        score += 15
        relevance = "Relevant govcon/persona signal"
        reasons.append("Relevant persona terms found")
    else:
        relevance = "Weak role signal"
        reasons.append("Weak role match")

    if contact.email:
        score += 15
        reasons.append("Business email found")
    if contact.phone:
        score += 10
        reasons.append("Business phone found")

    if "linkedin.com/in/" in contact.source_url.lower():
        score += 15
        reasons.append("LinkedIn profile signal")
    elif contact.source_url:
        score += 10
        reasons.append("Public source URL")

    freshness = contact_source_age(contact)
    if freshness in {"Fresh", "Likely recent"}:
        score += 10
        reasons.append("Recent source signal")
    elif freshness == "Date not visible":
        reasons.append("Source date not visible")
    elif freshness == "Old":
        score -= 10
        reasons.append("Old source signal")

    score = max(0, min(score, 100))
    if is_verified and score >= 75 and contact.full_name:
        status = "Verified"
        next_step = "Use in cadence; re-check role if the verified date is old."
    elif score >= 75 and contact.full_name:
        status = "Ready to verify"
        next_step = "Open source link, confirm current role, then add to cadence."
    elif score >= 50:
        status = "Verify first"
        next_step = "Use as a research lead; confirm current role/contact path before outreach."
    else:
        status = "Not ready"
        next_step = "Use the role-based search link or enrichment vendor before sequencing."

    return ContactQuality(status, score, relevance, freshness, next_step, tuple(reasons[:5]))


def contact_quality_summary(account: Account, intel: CompanyIntel | None = None) -> dict[str, object]:
    verified_contacts = load_verified_contacts(account.company)
    if (not isinstance(intel, CompanyIntel) or not intel.contacts) and not verified_contacts:
        return {
            "status": "No scanned contacts",
            "ready": 0,
            "verify": 0,
            "not_ready": len(contact_targets(account)),
            "best_score": 0,
            "message": "Run the public/contact scan before using the contact list.",
        }

    rows = people_to_contact_dataframe(account, intel)
    ready = int(rows["Contact status"].isin(["Verified", "Ready to verify"]).sum()) if "Contact status" in rows else 0
    verify = int((rows["Contact status"] == "Verify first").sum()) if "Contact status" in rows else 0
    not_ready = int((rows["Contact status"] == "Not ready").sum()) if "Contact status" in rows else 0
    best_score = int(pd.to_numeric(rows.get("Contact score", pd.Series([0])), errors="coerce").fillna(0).max()) if not rows.empty else 0
    if ready >= 2:
        status = "Good list"
        message = "There are at least two named, relevant contacts ready for manual verification."
    elif ready == 1 or verify >= 2:
        status = "Usable with verification"
        message = "Use the best contact, but verify roles before sequencing."
    else:
        status = "Needs more research"
        message = "The scan did not find enough current, relevant named people. Use enrichment/manual LinkedIn research before outreach."
    return {
        "status": status,
        "ready": ready,
        "verify": verify,
        "not_ready": not_ready,
        "best_score": best_score,
        "message": message,
    }


def source_audit_status(evidence_type: str, evidence_level: str, recency: str) -> str:
    combined = f"{evidence_type} {evidence_level} {recency}".lower()
    if "do not sequence" in combined or "blocked" in combined:
        return "Do not use"
    if "ready to sequence" in combined or "source-backed" in combined or "company-specific" in combined:
        return "Strong"
    if "stale" in combined or "verify date" in combined or "date not visible" in combined:
        return "Verify"
    if "industry benchmark" in combined or "hypothesis" in combined:
        return "Hypothesis"
    return "Review"


def confidence_label(evidence_type: str = "", evidence_level: str = "", source: str = "", recency: str = "") -> str:
    combined = f"{evidence_type} {evidence_level} {source} {recency}".lower()
    if any(term in combined for term in ["usa spending", "usaspending", "sam.gov", "official", "source page scanned"]):
        return "Official source"
    if any(term in combined for term in ["verified current role", "ready to sequence", "a - current", "saved verified contact"]):
        return "Verified source"
    if any(term in combined for term in ["hunter", "apollo", "zoominfo", "people data labs", "clearbit", "clay", "vendor enrichment"]):
        return "Vendor enrichment"
    if any(term in combined for term in ["public web", "linkedin", "company-specific", "company signal", "source-backed"]):
        return "Public source"
    if any(term in combined for term in ["old", "stale", "recheck", "date not visible", "verify date"]):
        return "Stale or needs recheck"
    if any(term in combined for term in ["industry benchmark", "hypothesis", "not found", "research needed", "no source"]):
        return "Hypothesis"
    return "Needs verification"


def confidence_label_for_contact(contact: PublicContact, quality: ContactQuality | None = None) -> str:
    evidence = " ".join([contact.evidence, contact.source_url, contact.recommended_reason])
    quality = quality or quality_for_contact(contact)
    if "verified contact saved in crm" in evidence.lower():
        return "Verified source"
    return confidence_label("Public contact", quality.status, "LinkedIn signal" if "linkedin.com" in contact.source_url.lower() else "Public web", quality.freshness)


def confidence_label_for_verified_contact(contact: VerifiedContact) -> str:
    gate = verified_contact_gate(contact)
    return confidence_label("Verified contact", f"{contact.verification_status} {gate['evidence_grade']}", contact.source_type, gate["gate"])


def confidence_label_for_pain(point: PainPoint) -> str:
    return confidence_label("Pain point", point.evidence_level, point.source, recency_hint(" ".join([point.evidence_title, point.snippet, point.source_url])))


def confidence_label_for_signal(signal: AccountSignal) -> str:
    return confidence_label(signal.signal_type, "Company signal", signal.source, signal.recency_hint)


def source_audit_dataframe(account: Account, intel: CompanyIntel | None = None) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    scanned_at = getattr(intel, "scanned_at", "") if isinstance(intel, CompanyIntel) else ""

    for contact in load_verified_contacts(account.company):
        gate = verified_contact_gate(contact)
        rows.append(
            {
                "Evidence type": "Verified contact",
                "Confidence label": confidence_label_for_verified_contact(contact),
                "Item": contact.full_name,
                "Evidence level": gate["evidence_grade"],
                "Recency / gate": gate["gate"],
                "Captured / verified": contact.verified_at,
                "Source": contact.source_type,
                "Source URL": contact.linkedin_url or contact.source_url,
                "Evidence snippet": short_text(contact.notes or contact.verification_status),
                "Audit status": source_audit_status("Verified contact", gate["evidence_grade"], gate["gate"]),
                "SDR action": gate["action"],
            }
        )

    if isinstance(intel, CompanyIntel):
        for contact in intel.contacts:
            quality = quality_for_contact(contact)
            rows.append(
                {
                    "Evidence type": "Public contact",
                    "Confidence label": confidence_label_for_contact(contact, quality),
                    "Item": contact.full_name or contact.title or "Unnamed contact",
                    "Evidence level": quality.relevance,
                    "Recency / gate": quality.freshness,
                    "Captured / verified": scanned_at,
                    "Source": "LinkedIn signal" if "linkedin.com" in contact.source_url.lower() else "Public web",
                    "Source URL": contact.source_url,
                    "Evidence snippet": short_text(contact.evidence),
                    "Audit status": source_audit_status("Public contact", quality.status, quality.freshness),
                    "SDR action": quality.next_step,
                }
            )

        for point in getattr(intel, "pain_points", tuple()):
            rows.append(
                {
                    "Evidence type": "Pain point",
                    "Confidence label": confidence_label_for_pain(point),
                    "Item": point.pain_point,
                    "Evidence level": point.evidence_level,
                    "Recency / gate": recency_hint(" ".join([point.evidence_title, point.snippet, point.source_url])),
                    "Captured / verified": scanned_at,
                    "Source": point.source,
                    "Source URL": point.source_url,
                    "Evidence snippet": short_text(point.snippet),
                    "Audit status": source_audit_status("Pain point", point.evidence_level, point.snippet),
                    "SDR action": f"Verify on call: {point.recommended_question}",
                }
            )

        for signal in getattr(intel, "account_signals", tuple()):
            rows.append(
                {
                    "Evidence type": signal.signal_type,
                    "Confidence label": confidence_label_for_signal(signal),
                    "Item": signal.title,
                    "Evidence level": "Company signal",
                    "Recency / gate": signal.recency_hint,
                    "Captured / verified": scanned_at,
                    "Source": signal.source,
                    "Source URL": signal.url,
                    "Evidence snippet": short_text(signal.snippet),
                    "Audit status": source_audit_status(signal.signal_type, "Company signal", signal.recency_hint),
                    "SDR action": signal.call_angle,
                }
            )

        for url in getattr(intel, "scanned_urls", tuple()):
            rows.append(
                {
                    "Evidence type": "Scanned source",
                    "Confidence label": confidence_label("Scanned source", "Source page scanned", signal_source(url), "Captured"),
                    "Item": url_domain(url) or url,
                    "Evidence level": "Source page scanned",
                    "Recency / gate": "Captured",
                    "Captured / verified": scanned_at,
                    "Source": signal_source(url),
                    "Source URL": url,
                    "Evidence snippet": "Page was scanned for company intel, contacts, and pain evidence.",
                    "Audit status": "Review",
                    "SDR action": "Use only source-backed rows from this page; do not infer unsupported claims.",
                }
            )

    return pd.DataFrame(rows)


def save_source_audit_snapshot(company: str, audit_df: pd.DataFrame, reviewer: str = "", review_note: str = "") -> int:
    if audit_df.empty:
        return 0
    created_at = datetime.now().isoformat(timespec="seconds")
    payloads = []
    for _, row in audit_df.iterrows():
        payloads.append(
            {
                "company": company,
                "evidence_type": clean_import_value(row.get("Evidence type", "")),
                "item": clean_import_value(row.get("Item", "")),
                "evidence_level": clean_import_value(row.get("Evidence level", "")),
                "recency_gate": clean_import_value(row.get("Recency / gate", "")),
                "captured_verified": clean_import_value(row.get("Captured / verified", "")),
                "source": clean_import_value(row.get("Source", "")),
                "source_url": clean_import_value(row.get("Source URL", "")),
                "evidence_snippet": clean_import_value(row.get("Evidence snippet", "")),
                "audit_status": clean_import_value(row.get("Audit status", "")),
                "sdr_action": clean_import_value(row.get("SDR action", "")),
                "reviewer": reviewer.strip(),
                "review_note": review_note.strip(),
                "created_at": created_at,
            }
        )

    if supabase_enabled():
        try:
            for payload in payloads:
                supabase_insert("source_audit_records", payload)
            return len(payloads)
        except requests.RequestException as exc:
            storage_warning(f"Supabase source-audit save failed, using SQLite fallback: {exc}")

    with db_connect() as connection:
        connection.executemany(
            """
            INSERT INTO source_audit_records (
                company, evidence_type, item, evidence_level, recency_gate,
                captured_verified, source, source_url, evidence_snippet, audit_status,
                sdr_action, reviewer, review_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    payload["company"],
                    payload["evidence_type"],
                    payload["item"],
                    payload["evidence_level"],
                    payload["recency_gate"],
                    payload["captured_verified"],
                    payload["source"],
                    payload["source_url"],
                    payload["evidence_snippet"],
                    payload["audit_status"],
                    payload["sdr_action"],
                    payload["reviewer"],
                    payload["review_note"],
                    payload["created_at"],
                )
                for payload in payloads
            ],
        )
    return len(payloads)


def load_source_audit_records(company: str, limit: int = 100) -> tuple[SourceAuditRecord, ...]:
    if supabase_enabled():
        try:
            rows = supabase_select(
                "source_audit_records",
                {
                    "select": "*",
                    "company": f"eq.{company}",
                    "order": "created_at.desc,id.desc",
                    "limit": str(limit),
                },
            )
            return tuple(
                SourceAuditRecord(
                    id=int(row.get("id") or 0),
                    company=str(row.get("company") or ""),
                    evidence_type=str(row.get("evidence_type") or ""),
                    item=str(row.get("item") or ""),
                    evidence_level=str(row.get("evidence_level") or ""),
                    recency_gate=str(row.get("recency_gate") or ""),
                    captured_verified=str(row.get("captured_verified") or ""),
                    source=str(row.get("source") or ""),
                    source_url=str(row.get("source_url") or ""),
                    evidence_snippet=str(row.get("evidence_snippet") or ""),
                    audit_status=str(row.get("audit_status") or ""),
                    sdr_action=str(row.get("sdr_action") or ""),
                    reviewer=str(row.get("reviewer") or ""),
                    review_note=str(row.get("review_note") or ""),
                    created_at=str(row.get("created_at") or ""),
                )
                for row in rows
            )
        except requests.RequestException as exc:
            storage_warning(f"Supabase source-audit read failed, using SQLite fallback: {exc}")

    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM source_audit_records
            WHERE company = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (company, limit),
        ).fetchall()
    return tuple(
        SourceAuditRecord(
            id=int(row["id"]),
            company=str(row["company"] or ""),
            evidence_type=str(row["evidence_type"] or ""),
            item=str(row["item"] or ""),
            evidence_level=str(row["evidence_level"] or ""),
            recency_gate=str(row["recency_gate"] or ""),
            captured_verified=str(row["captured_verified"] or ""),
            source=str(row["source"] or ""),
            source_url=str(row["source_url"] or ""),
            evidence_snippet=str(row["evidence_snippet"] or ""),
            audit_status=str(row["audit_status"] or ""),
            sdr_action=str(row["sdr_action"] or ""),
            reviewer=str(row["reviewer"] or ""),
            review_note=str(row["review_note"] or ""),
            created_at=str(row["created_at"] or ""),
        )
        for row in rows
    )


def source_audit_history_dataframe(company: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Saved at": record.created_at,
                "Reviewer": record.reviewer,
                "Review note": record.review_note,
                "Evidence type": record.evidence_type,
                "Item": record.item,
                "Evidence level": record.evidence_level,
                "Recency / gate": record.recency_gate,
                "Source": record.source,
                "Source URL": record.source_url,
                "Audit status": record.audit_status,
                "SDR action": record.sdr_action,
                "Evidence snippet": record.evidence_snippet,
            }
            for record in load_source_audit_records(company)
        ]
    )


def account_fit_assessment(account: Account, intel: CompanyIntel | None = None) -> dict[str, object]:
    contact_summary = contact_quality_summary(account, intel)
    verified_count = len(load_verified_contacts(account.company))
    activities = load_crm_activities(account.company, limit=20)
    open_tasks = [activity for activity in activities if not activity.completed]
    signals = getattr(intel, "account_signals", tuple()) if isinstance(intel, CompanyIntel) else tuple()
    pain_points = getattr(intel, "pain_points", tuple()) if isinstance(intel, CompanyIntel) else tuple()

    score = account.priority_score
    reasons = score_breakdown(account)
    blockers: list[str] = []

    best_contact_score = int(contact_summary.get("best_score", 0))
    if verified_count >= 2:
        score += 12
        reasons.append("Two or more saved verified contacts make this account ready for live outreach.")
    elif verified_count == 1:
        score += 8
        reasons.append("One saved verified contact gives SDRs a real person to sequence.")
    elif best_contact_score >= 75:
        score += 6
        reasons.append("Public scan found at least one named contact worth manual verification.")
    else:
        score -= 10
        blockers.append("No verified contact is saved yet.")

    if signals:
        score += min(8, 3 + len(signals))
        reasons.append("Public call-intel signals create a more relevant reason to call than the award alone.")
    else:
        blockers.append("Automatic Public Intel has not found recent announcements, interviews, podcasts, hiring, or LinkedIn-style public signals yet.")

    if pain_points:
        score += 5
        reasons.append("Pain points are backed by public evidence or industry benchmark research prompts.")
    else:
        blockers.append("Pain points are still generic until Public Intel finds company or industry evidence.")

    if open_tasks:
        score += 3
        reasons.append("There is already a next CRM task queued for this account.")

    score = max(0, min(score, 100))
    if score >= 88 and verified_count:
        tier = "Work today"
        next_move = "Use the saved contact, open the Call Prep tab, and execute the next cadence step."
    elif score >= 78:
        tier = "High priority"
        next_move = "Verify one named contact, then send a tailored first-touch using the call prep brief."
    elif score >= 65:
        tier = "Research first"
        next_move = "Use the automatic research output, then create the full pursuit package when contact evidence is strong enough."
    else:
        tier = "Nurture"
        next_move = "Keep in nurture unless the agency, NAICS, or award value is strategically important."

    if not blockers:
        blockers.append("No major blocker. Re-check role/current contact status before outreach.")

    return {
        "score": score,
        "tier": tier,
        "reasons": reasons[:6],
        "blockers": blockers[:4],
        "next_move": next_move,
        "contact_gate": contact_summary.get("status", ""),
        "verified_contacts": verified_count,
        "best_contact_score": best_contact_score,
        "open_tasks": len(open_tasks),
        "signal_count": len(signals),
        "pain_count": len(pain_points),
    }


def account_action_queue_dataframe(accounts: list[Account]) -> pd.DataFrame:
    rows = []
    for account in accounts:
        intel = st.session_state.get(public_intel_key(account.company))
        if not isinstance(intel, CompanyIntel):
            intel = None
        assessment = account_fit_assessment(account, intel)
        rows.append(
            {
                "_sort_score": assessment["score"],
                "_sort_value": account.total_amount,
                "Action score": assessment["score"],
                "Priority": assessment["tier"],
                "Company": account.company,
                "Base tier": account.tier,
                "Award value": money(account.total_amount),
                "Latest award": account.latest_award_date,
                "Verified contacts": assessment["verified_contacts"],
                "Contact gate": assessment["contact_gate"],
                "Call signals": assessment["signal_count"],
                "Pain signals": assessment["pain_count"],
                "Open tasks": assessment["open_tasks"],
                "Why priority": " ".join(str(reason) for reason in assessment["reasons"][:3]),
                "Blockers": " ".join(str(blocker) for blocker in assessment["blockers"]),
                "Next move": assessment["next_move"],
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["_sort_score", "_sort_value"], ascending=[False, False]).drop(columns=["_sort_score", "_sort_value"])


def account_duplicate_score(left: Account, right: Account, domain_by_company: dict[str, str] | None = None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    left_uei = set(account_uei_values(left))
    right_uei = set(account_uei_values(right))
    shared_uei = sorted(left_uei & right_uei)
    if shared_uei:
        score += 70
        reasons.append(f"Shared UEI: {', '.join(shared_uei[:2])}")

    domain_by_company = domain_by_company or {}
    left_domain = domain_by_company.get(left.company, "")
    right_domain = domain_by_company.get(right.company, "")
    if left_domain and right_domain and left_domain == right_domain:
        score += 60
        reasons.append(f"Shared domain: {left_domain}")

    name_similarity = company_name_similarity(left.company, right.company)
    if name_similarity >= 92:
        score += 35
        reasons.append(f"Very similar names ({name_similarity})")
    elif name_similarity >= 82:
        score += 22
        reasons.append(f"Similar names ({name_similarity})")
    elif normalize_company_name(left.company) in normalize_company_name(right.company) or normalize_company_name(right.company) in normalize_company_name(left.company):
        score += 18
        reasons.append("Parent/subsidiary style name overlap")

    shared_states = sorted(set(account_state_values(left)) & set(account_state_values(right)))
    if shared_states:
        score += 8
        reasons.append(f"Shared state: {', '.join(shared_states[:2])}")

    shared_addresses = sorted(set(account_address_values(left)) & set(account_address_values(right)))
    if shared_addresses:
        score += 12
        reasons.append("Shared address")

    return min(score, 100), reasons


def account_duplicate_risk_dataframe(accounts: list[Account], threshold: int = 45) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    domain_by_company = {account.company: account_known_domain(account, include_verified_contacts=False) for account in accounts}
    for left_index, left in enumerate(accounts):
        for right in accounts[left_index + 1 :]:
            score, reasons = account_duplicate_score(left, right, domain_by_company)
            if score < threshold:
                continue
            keep = left if (left.total_amount, left.award_count, left.latest_award_date) >= (right.total_amount, right.award_count, right.latest_award_date) else right
            duplicate = right if keep is left else left
            rows.append(
                {
                    "_score": score,
                    "Confidence": score,
                    "Keep account": keep.company,
                    "Possible duplicate": duplicate.company,
                    "Match reason": "; ".join(reasons),
                    "Keep UEI(s)": ", ".join(account_uei_values(keep)),
                    "Duplicate UEI(s)": ", ".join(account_uei_values(duplicate)),
                    "Keep domain": domain_by_company.get(keep.company, ""),
                    "Duplicate domain": domain_by_company.get(duplicate.company, ""),
                    "Keep value": money(keep.total_amount),
                    "Duplicate value": money(duplicate.total_amount),
                    "Recommended action": "Review before HubSpot sync; merge CRM activity under the keep account if this is the same entity.",
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("_score", ascending=False).drop(columns=["_score"])


def best_contact_summary(account: Account, intel: CompanyIntel | None = None) -> dict[str, str]:
    people = people_to_contact_dataframe(account, intel)
    if people.empty:
        target = contact_targets(account)[0]
        return {
            "name": target.title,
            "title": target.title,
            "email": "",
            "phone": "",
            "source": search_url(target.search_query),
            "reason": target.why,
            "status": "Research needed",
        }
    row = people.iloc[0].to_dict()
    return {
        "name": str(row.get("Best known person", "")) or str(row.get("Target role", "")),
        "title": str(row.get("Target role", "")),
        "email": str(row.get("Email", "")),
        "phone": str(row.get("Phone", "")),
        "source": str(row.get("Source / search URL", "")),
        "reason": str(row.get("Why this contact", "")),
        "status": str(row.get("Contact status", "")),
    }


def sam_context_lines(opportunities: tuple[SamOpportunity, ...]) -> list[str]:
    lines = []
    for opportunity in opportunities[:4]:
        parts = [
            opportunity.notice_type or "SAM.gov notice",
            opportunity.title,
            f"solicitation {opportunity.solicitation_number}" if opportunity.solicitation_number else "",
            f"set-aside {opportunity.set_aside}" if opportunity.set_aside else "",
            f"place {opportunity.place_of_performance}" if opportunity.place_of_performance else "",
            f"government POC {opportunity.point_of_contact}" if opportunity.point_of_contact else "",
        ]
        lines.append(" | ".join(part for part in parts if part))
    return lines or ["Automatic SAM.gov enrichment has not added notice, set-aside, contracting office, place-of-performance, or government POC context yet."]


def call_prep_sections(
    account: Account,
    intel: CompanyIntel | None = None,
    sam_opportunities: tuple[SamOpportunity, ...] = tuple(),
) -> dict[str, list[str] | str]:
    primary = account.primary
    agency = primary.funding_sub_agency or primary.awarding_sub_agency or primary.awarding_agency
    contact = best_contact_summary(account, intel)
    assessment = account_fit_assessment(account, intel)
    signals = list(getattr(intel, "account_signals", tuple())) if isinstance(intel, CompanyIntel) else []
    pains = list(getattr(intel, "pain_points", tuple())) if isinstance(intel, CompanyIntel) else []
    top_sam = sam_opportunities[0] if sam_opportunities else None
    if not pains:
        pains = industry_benchmark_pain_points(
            industry_category(primary.naics_description, primary.psc_description, primary.description, primary.psc_code),
            account.company,
        )[:3]

    if isinstance(intel, CompanyIntel):
        company_summary = intel.what_they_do
        why_won = intel.why_they_may_have_won
    else:
        company_summary = (
            f"{account.company} recently won work tied to {primary.naics_description or primary.psc_description or 'federal contracting'}. "
            "Automatic Public Intel should replace this with source-backed company research when public sources are available."
        )
        why_won = (
            f"The award record points to fit around {primary.contract_focus}. USAspending does not expose evaluation rationale, "
            "so treat this as a hypothesis to validate."
        )
    if top_sam:
        sam_won_context = (
            f"SAM.gov context: {top_sam.notice_type or 'notice'} posted {top_sam.posted_date or 'date unknown'}"
            f"{' with set-aside ' + top_sam.set_aside if top_sam.set_aside else ''}"
            f"{' through ' + (top_sam.organization_path or top_sam.office) if (top_sam.organization_path or top_sam.office) else ''}."
        )
    else:
        sam_won_context = "Automatic SAM.gov enrichment has not added notice-level procurement context yet."

    return {
        "headline": f"{account.company} | {assessment['tier']} | action score {assessment['score']}/100",
        "account_summary": company_summary,
        "what_they_won": (
            f"{primary.award_id} for {money(primary.amount)} with {agency or 'the buying agency'}. "
            f"{primary.description or 'No public description was included in the award result.'} {sam_won_context}"
        ),
        "why_they_may_have_won": why_won,
        "why_now": why_now_triggers(primary)[:4],
        "best_contact": (
            f"{contact['name']} | {contact['title']} | {contact['status']}. "
            f"{contact['reason']}"
        ),
        "contact_path": [
            f"Email: {contact['email'] or 'not verified yet'}",
            f"Phone: {contact['phone'] or 'not verified yet'}",
            f"Source/search: {contact['source'] or 'use Contact Finder search links'}",
        ],
        "call_intel": [
            f"{signal.signal_type}: {signal.call_angle}"
            for signal in signals[:4]
        ]
        or ["No public call-intel signals are saved yet. Automatic Public Intel did not find a recent call trigger."],
        "sam_gov_context": sam_context_lines(sam_opportunities),
        "pain_points": [
            f"{point.pain_point} - ask: {point.recommended_question}"
            for point in pains[:4]
        ],
        "talk_track": (
            f"Congrats on {primary.award_id}. I saw the work with {agency or 'the agency'} and wanted to share a quick way "
            f"{account.company} could turn this win into reusable capture, proposal, and contract evidence inside GovDash."
        ),
        "discovery_questions": discovery_questions(primary),
        "objections": [
            "We already have tools: ask where award kickoff, proposal reuse, compliance matrices, and delivery proof live today.",
            "Timing is bad: anchor to the award kickoff, option-year, or follow-on pursuit window.",
            "This is just a contract-management problem: pivot to how award evidence becomes reusable proposal and capture material.",
            "Not my role: ask who owns capture/proposal operations, contracts, or program evidence for this award.",
        ],
        "demo_angle": list(demo_asset_pack(primary, intel).values())[:4],
        "next_move": str(assessment["next_move"]),
    }


def call_prep_markdown(
    account: Account,
    intel: CompanyIntel | None = None,
    sam_opportunities: tuple[SamOpportunity, ...] = tuple(),
) -> str:
    sections = call_prep_sections(account, intel, sam_opportunities)
    lines = [f"# Call Prep: {sections['headline']}", ""]
    for label in ["account_summary", "what_they_won", "why_they_may_have_won", "best_contact", "talk_track", "next_move"]:
        title = label.replace("_", " ").title()
        lines.extend([f"## {title}", str(sections[label]), ""])
    for label in ["why_now", "contact_path", "call_intel", "sam_gov_context", "pain_points", "discovery_questions", "objections", "demo_angle"]:
        title = label.replace("_", " ").title()
        lines.append(f"## {title}")
        for item in sections[label]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def account_brief_sections(
    account: Account,
    intel: CompanyIntel | None = None,
    sam_opportunities: tuple[SamOpportunity, ...] = tuple(),
) -> dict[str, object]:
    primary = account.primary
    call_sections = call_prep_sections(account, intel, sam_opportunities)
    assessment = account_fit_assessment(account, intel)
    crm = load_crm_record(account.company)
    verified_contacts = load_verified_contacts(account.company)
    email_ready = email_ready_contacts(verified_contacts)
    domain, domain_source = suggested_hubspot_domain(account, intel, verified_contacts)
    signals = list(getattr(intel, "account_signals", tuple())) if isinstance(intel, CompanyIntel) else []
    pains = list(getattr(intel, "pain_points", tuple())) if isinstance(intel, CompanyIntel) else []
    website = intel.website if isinstance(intel, CompanyIntel) else ""
    sources = list(getattr(intel, "sources", tuple())) if isinstance(intel, CompanyIntel) else []
    scanned_at = intel.scanned_at if isinstance(intel, CompanyIntel) else ""
    trust_gaps: list[str] = []
    if not isinstance(intel, CompanyIntel):
        trust_gaps.append("Automatic Public Intel has not replaced award-only hypotheses with source-backed company research yet.")
    if not email_ready:
        trust_gaps.append("Save or enrich at least one contact with a business email before launching cadence.")
    if not domain:
        trust_gaps.append("Confirm the company domain before relying on HubSpot duplicate matching.")
    if not signals:
        trust_gaps.append("Add recent announcements, interviews, podcasts, hiring, or LinkedIn-style public signals for a more relevant opener.")
    if not pains or all(point.evidence_level == "Industry benchmark to verify" for point in pains):
        trust_gaps.append("Validate the pain hypotheses with company-specific evidence before using them as claims.")
    if not trust_gaps:
        trust_gaps.append("No major brief blocker. Re-check contact title and consent/compliance before outreach.")

    return {
        "headline": call_sections["headline"],
        "executive_summary": (
            f"{account.company} is a {assessment['tier']} account with {account.award_count} recent award(s), "
            f"{money(account.total_amount)} in visible award value, and an action score of {assessment['score']}/100. "
            f"The strongest trigger is {why_now_triggers(primary)[0]}"
        ),
        "company": {
            "website": website or "Not found yet",
            "domain": domain or "Not found yet",
            "domain_source": domain_source,
            "what_they_do": call_sections["account_summary"],
            "scanned_at": scanned_at or "Public Intel not run yet",
            "sources": sources[:6],
        },
        "award": {
            "award_id": primary.award_id,
            "value": money(primary.amount),
            "agency": primary.funding_sub_agency or primary.awarding_sub_agency or primary.awarding_agency or "Unknown agency",
            "latest_award": account.latest_award_date or "Unknown",
            "period": f"{primary.start_date or 'unknown'} to {primary.end_date or 'unknown'}",
            "description": primary.description or "No public description was included in the award result.",
            "why_they_may_have_won": call_sections["why_they_may_have_won"],
        },
        "best_contact": best_contact_summary(account, intel),
        "verified_contacts": verified_contacts,
        "contact_counts": {
            "saved": len(verified_contacts),
            "email_ready": len(email_ready),
            "public_candidates": len(getattr(intel, "contacts", tuple())) if isinstance(intel, CompanyIntel) else 0,
        },
        "pain_points": [
            f"{point.pain_point} | Evidence: {point.evidence_level} | Ask: {point.recommended_question}"
            for point in pains[:5]
        ]
        or list(call_sections["pain_points"]),
        "call_intel": list(call_sections["call_intel"]),
        "talk_track": call_sections["talk_track"],
        "discovery_questions": list(call_sections["discovery_questions"]),
        "govdash_demo": list(demo_asset_pack(primary, intel).items()),
        "cadence": [
            f"{day}: {action} - {detail}"
            for day, action, detail in DEFAULT_CADENCE
        ],
        "crm": {
            "status": str(crm.get("status", "New")),
            "owner": str(crm.get("owner", "")),
            "persona": str(crm.get("persona", suggested_personas(primary)[0])),
            "cadence_stage": str(crm.get("cadence_stage", DEFAULT_CADENCE[0][0])),
            "next_action": str(crm.get("next_action", "Email")),
            "next_step": str(crm.get("next_step", "")),
            "notes": str(crm.get("notes", "")),
        },
        "assessment": assessment,
        "trust_gaps": trust_gaps[:6],
        "next_move": str(assessment["next_move"]),
        "sam_context": sam_context_lines(sam_opportunities),
    }


def account_brief_markdown(
    account: Account,
    intel: CompanyIntel | None = None,
    sam_opportunities: tuple[SamOpportunity, ...] = tuple(),
) -> str:
    brief = account_brief_sections(account, intel, sam_opportunities)
    company = brief["company"]
    award = brief["award"]
    contact = brief["best_contact"]
    crm = brief["crm"]
    assessment = brief["assessment"]
    contact_counts = brief["contact_counts"]
    lines = [
        f"# Account Research Brief: {account.company}",
        "",
        f"## Executive Summary\n{brief['executive_summary']}",
        "",
        "## Company Research",
        f"- Website: {company['website']}",
        f"- Domain: {company['domain']} ({company['domain_source']})",
        f"- Scanned at: {company['scanned_at']}",
        f"- What they do: {company['what_they_do']}",
        "",
        "## Contract Trigger",
        f"- Award: {award['award_id']}",
        f"- Value: {award['value']}",
        f"- Agency: {award['agency']}",
        f"- Latest award date: {award['latest_award']}",
        f"- Period: {award['period']}",
        f"- Description: {award['description']}",
        f"- Why they may have won: {award['why_they_may_have_won']}",
        "",
        "## Best Contact",
        f"- Name: {contact['name']}",
        f"- Role: {contact['title']}",
        f"- Status: {contact['status']}",
        f"- Email: {contact['email'] or 'Not verified yet'}",
        f"- Phone: {contact['phone'] or 'Not verified yet'}",
        f"- Source: {contact['source']}",
        f"- Why: {contact['reason']}",
        f"- Saved contacts: {contact_counts['saved']}; email-ready: {contact_counts['email_ready']}; public candidates: {contact_counts['public_candidates']}",
        "",
        "## Pain Points To Validate",
    ]
    for item in brief["pain_points"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Call Intel"])
    for item in brief["call_intel"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Talk Track", str(brief["talk_track"]), "", "## Discovery Questions"])
    for item in brief["discovery_questions"]:
        lines.append(f"- {item}")
    lines.extend(["", "## GovDash Demo"])
    for label, body in brief["govdash_demo"]:
        lines.append(f"- {label}: {body}")
    lines.extend(["", "## Cadence"])
    for item in brief["cadence"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## CRM State",
            f"- Status: {crm['status']}",
            f"- Owner: {crm['owner'] or 'Unassigned'}",
            f"- Persona: {crm['persona']}",
            f"- Cadence stage: {crm['cadence_stage']}",
            f"- Next action: {crm['next_action']} on {crm['next_step'] or 'not scheduled'}",
            f"- Notes: {crm['notes'] or 'None'}",
            "",
            "## Readiness",
            f"- Score: {assessment['score']}/100",
            f"- Priority: {assessment['tier']}",
            f"- Next move: {brief['next_move']}",
            "",
            "## Trust Gaps",
        ]
    )
    for item in brief["trust_gaps"]:
        lines.append(f"- {item}")
    if company["sources"]:
        lines.extend(["", "## Sources"])
        for source in company["sources"]:
            lines.append(f"- {source}")
    lines.extend(["", "## SAM.gov Context"])
    for item in brief["sam_context"]:
        lines.append(f"- {item}")
    return "\n".join(str(line) for line in lines).strip() + "\n"


def pdf_safe_text(value: object) -> str:
    text = str(value or "")
    text = text.replace("\r", "")
    return text.encode("latin-1", "replace").decode("latin-1")


def pdf_escape(value: object) -> str:
    text = pdf_safe_text(value)
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def markdown_to_pdf_lines(markdown: str, title: str) -> list[str]:
    lines = [title, ""]
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\-\s+", "- ", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        wrapped = textwrap.wrap(line, width=92, replace_whitespace=False, drop_whitespace=True)
        lines.extend(wrapped or [""])
    return lines


def text_pdf_bytes(title: str, markdown: str) -> bytes:
    source_lines = markdown_to_pdf_lines(markdown, title)
    page_lines: list[list[str]] = []
    current: list[str] = []
    max_lines = 52
    for line in source_lines:
        if len(current) >= max_lines:
            page_lines.append(current)
            current = []
        current.append(line)
    if current:
        page_lines.append(current)
    if not page_lines:
        page_lines = [[title]]

    objects: list[bytes] = []
    page_object_numbers: list[int] = []
    content_object_numbers: list[int] = []

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for lines in page_lines:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_object_numbers.append(page_number)
        content_object_numbers.append(content_number)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
            ).encode("latin-1")
        )
        stream_lines = ["BT", "/F1 10 Tf", "14 TL", "54 744 Td"]
        for line in lines:
            stream_lines.append(f"({pdf_escape(line)}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", "replace")
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")

    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_numbers)} >>".encode("latin-1")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def brief_pdf_bytes(title: str, markdown: str) -> bytes:
    return text_pdf_bytes(title, markdown)


def pursuit_package_dataframe(result: dict[str, object]) -> pd.DataFrame:
    rows = result.get("steps")
    if not isinstance(rows, list):
        rows = []
    return pd.DataFrame(
        [
            {
                "Step": str(row.get("step", "")) if isinstance(row, dict) else "",
                "Status": str(row.get("status", "")) if isinstance(row, dict) else "",
                "Confidence label": confidence_label(str(row.get("step", "")), str(row.get("status", "")), str(row.get("result", ""))) if isinstance(row, dict) else "",
                "Result": str(row.get("result", "")) if isinstance(row, dict) else "",
                "Next action": str(row.get("next_action", "")) if isinstance(row, dict) else "",
            }
            for row in rows
        ]
    )


def build_full_pursuit_package(account: Account, sync_to_hubspot: bool = True) -> dict[str, object]:
    steps: list[dict[str, str]] = []
    started_at = datetime.now().isoformat(timespec="seconds")

    def add_step(step: str, status: str, result: str, next_action: str = "") -> None:
        steps.append(
            {
                "step": step,
                "status": status,
                "result": result,
                "next_action": next_action,
            }
        )

    intel: CompanyIntel | None = None
    sam_opportunities: tuple[SamOpportunity, ...] = tuple()
    domain = ""
    domain_source = "not found yet"
    duplicate_matches: list[dict[str, str]] = []
    company_id = ""
    company_message = "HubSpot sync not requested."
    synced_count = 0
    skipped_count = 0
    imported_count = 0
    sync_errors: list[str] = []
    enrichment_messages: list[str] = []

    try:
        intel = enrich_account(account)
        st.session_state[public_intel_key(account.company)] = intel
        add_step(
            "Public Intel",
            "Complete",
            f"Scanned {len(intel.scanned_urls)} page(s), found {len(intel.contacts)} public contact candidate(s), {len(intel.pain_points)} pain signal(s), and {len(intel.account_signals)} call signal(s).",
            "Use the source audit trail before sequencing.",
        )
    except Exception as exc:
        add_step("Public Intel", "Needs review", f"Public scan failed: {exc}", "Broaden the filters or create the package again later.")

    if sam_enabled():
        try:
            sam_opportunities, sam_message = fetch_sam_opportunities(account)
            st.session_state[sam_intel_key(account.company)] = sam_opportunities
            st.session_state[sam_message_key(account.company)] = sam_message
            add_step("SAM.gov", "Complete", sam_message, "Use SAM notice context for procurement details, not as contractor contact data.")
        except Exception as exc:
            add_step("SAM.gov", "Needs review", f"SAM.gov enrichment failed: {exc}", "Try SAM.gov enrichment again from Public Intel.")
    else:
        add_step("SAM.gov", "Skipped", "SAM_API_KEY is not configured.", "Add SAM_API_KEY when notice-level context is needed.")

    verified_contacts = load_verified_contacts(account.company)
    domain, domain_source, discovered_website = resolve_hubspot_domain(account, intel, verified_contacts, allow_public_search=True)
    domain_result = domain or "No domain found"
    if discovered_website:
        domain_result += f" via {discovered_website}"
    add_step(
        "Domain",
        "Complete" if domain else "Needs review",
        f"{domain_result}. Source: {domain_source}.",
        "Confirm official domain before heavy HubSpot dedupe work." if not domain else "Use this domain for duplicate checks and enrichment.",
    )

    contacts_to_sync, enrichment_messages, imported_count = auto_import_hunter_contacts_for_hubspot(account, domain, verified_contacts)
    st.session_state[f"hunter_message_{account.company}"] = " ".join(enrichment_messages)
    if imported_count:
        add_step(
            "Hunter",
            "Complete",
            f"Auto-imported {imported_count} email-ready contact(s). {' '.join(enrichment_messages[:2])}",
            "Review imported contacts before broad sequencing.",
        )
    elif email_ready_contacts(contacts_to_sync):
        add_step(
            "Hunter",
            "Ready",
            f"{len(email_ready_contacts(contacts_to_sync))} saved email-ready contact(s) already available.",
            "Use saved contacts for HubSpot sync and cadence.",
        )
    else:
        add_step(
            "Hunter",
            "Needs review",
            " ".join(enrichment_messages) or "No email-ready contacts found.",
            "Use Create Full Pursuit Package again after a company domain or email-ready contact is available.",
        )

    if hubspot_enabled():
        try:
            duplicate_matches = hubspot_company_matches(account.company, domain, limit=5)
            st.session_state[f"hubspot_duplicate_matches_{account.company}"] = duplicate_matches
            add_step(
                "HubSpot duplicate check",
                "Complete",
                summarize_hubspot_matches(duplicate_matches),
                "Review fuzzy matches before adding activity under a new company." if duplicate_matches else "No duplicate action needed from this run.",
            )
        except requests.RequestException as exc:
            add_step("HubSpot duplicate check", "Needs review", f"Duplicate check failed: {exc}", "Check HubSpot token/scopes and try again.")
    else:
        add_step("HubSpot duplicate check", "Skipped", "HUBSPOT_ACCESS_TOKEN is not configured.", "Add HubSpot token to sync companies and contacts.")

    if sync_to_hubspot and hubspot_enabled():
        company_id, company_message = hubspot_upsert_company(account, domain)
        if company_id:
            st.session_state[f"hubspot_company_id_{account.company}"] = company_id
            synced_count, skipped_count, sync_errors = hubspot_sync_verified_contacts(contacts_to_sync, company_id)
            add_step(
                "HubSpot sync",
                "Complete" if not sync_errors else "Partial",
                f"{company_message} Company ID: {company_id}. Synced {synced_count} contact(s); skipped {skipped_count}.",
                "Open HubSpot and confirm company/contact associations." if synced_count else "Add or enrich at least one email-ready contact.",
            )
        else:
            sync_errors.append(company_message)
            add_step("HubSpot sync", "Blocked", company_message, "Review duplicate warning or HubSpot API permissions.")
    elif sync_to_hubspot:
        add_step("HubSpot sync", "Skipped", "HubSpot is not configured.", "Add HUBSPOT_ACCESS_TOKEN to enable one-click company/contact sync.")
    else:
        add_step("HubSpot sync", "Skipped", "HubSpot sync option was turned off for this package run.", "Use Contact Finder sync when ready.")

    best_contact = best_contact_summary(account, intel)
    cadence_contact = str(best_contact.get("name") or "")
    if cadence_contact.lower() in {"research needed", "vp/director of business development", "capture manager", "proposal manager"}:
        email_contacts = email_ready_contacts(contacts_to_sync)
        cadence_contact = email_contacts[0].full_name if email_contacts and email_contacts[0].full_name else cadence_contact
    cadence_preview = build_cadence_activities(account, cadence_contact, date.today())
    add_step(
        "Cadence prep",
        "Complete",
        f"Prepared {len(cadence_preview)} recommended follow-up step(s) for {cadence_contact or 'the selected account'}.",
        "Launch the cadence from CRM Cadence after contact verification passes.",
    )

    brief_markdown = account_brief_markdown(account, intel, sam_opportunities)
    add_step(
        "Account Brief",
        "Complete",
        "Generated SDR-ready account research brief.",
        "Download the brief or use the Account Brief tab for live review.",
    )

    sync_result = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "company": account.company,
        "company_id": company_id,
        "company_message": company_message,
        "domain": domain,
        "domain_source": domain_source,
        "domain_action": "Use this domain for future duplicate checks." if domain else "Confirm the official company domain.",
        "duplicate_summary": summarize_hubspot_matches(duplicate_matches),
        "duplicate_action": "Review likely duplicate company records before heavy sequencing." if duplicate_matches else "No duplicate review needed from this run.",
        "contacts_before_count": len(verified_contacts),
        "email_ready_before": len(email_ready_contacts(verified_contacts)),
        "contacts_after_count": len(contacts_to_sync),
        "imported_count": imported_count,
        "synced_count": synced_count,
        "skipped_count": skipped_count,
        "enrichment_messages": enrichment_messages,
        "errors": sync_errors,
        "contact_reason": f"Ended with {len(contacts_to_sync)} saved contact(s), {len(email_ready_contacts(contacts_to_sync))} email-ready.",
        "contact_action": "Open HubSpot and start the cadence for synced contacts." if synced_count else "Save or enrich at least one contact with a business email, then sync again.",
        "company_action": "Use this company record for contacts, notes, calls, and cadence tasks." if company_id else "Review company sync before adding activities.",
    }
    st.session_state[hubspot_sync_result_key(account.company)] = sync_result

    result = {
        "company": account.company,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "steps": steps,
        "intel": intel,
        "sam_opportunities": sam_opportunities,
        "domain": domain,
        "domain_source": domain_source,
        "duplicate_matches": duplicate_matches,
        "contacts_to_sync": contacts_to_sync,
        "cadence_preview": cadence_preview,
        "brief_markdown": brief_markdown,
        "hubspot_sync_result": sync_result,
        "ready_score": int(account_fit_assessment(account, intel)["score"]),
        "next_move": str(account_fit_assessment(account, intel)["next_move"]),
    }
    st.session_state[pursuit_package_key(account.company)] = result
    return result


def render_pursuit_package_result(result: dict[str, object]) -> None:
    st.markdown("### Full Pursuit Package Results")
    st.caption(f"Started: {result.get('started_at', '')} | Completed: {result.get('completed_at', '')}")
    package_metrics = st.columns(4)
    package_metrics[0].metric("Readiness score", int(result.get("ready_score") or 0))
    package_metrics[1].metric("Domain", str(result.get("domain") or "Not found"))
    contacts = result.get("contacts_to_sync")
    package_metrics[2].metric("Saved contacts", len(contacts) if isinstance(contacts, tuple) else 0)
    cadence = result.get("cadence_preview")
    package_metrics[3].metric("Cadence steps", len(cadence) if isinstance(cadence, tuple) else 0)
    dataframe_with_links(pursuit_package_dataframe(result), width="stretch", hide_index=True)
    st.markdown(f'<div class="score-card"><b>Next move</b>{html_escape(str(result.get("next_move", "")))}</div>', unsafe_allow_html=True)
    if isinstance(cadence, tuple) and cadence:
        with st.expander("Cadence preview", expanded=False):
            dataframe_with_links(cadence_preview_dataframe(cadence), width="stretch", hide_index=True)
    brief_markdown = str(result.get("brief_markdown") or "")
    if brief_markdown:
        st.download_button(
            "Download generated pursuit package brief",
            data=brief_markdown,
            file_name=f"{str(result.get('company', 'account')).lower().replace(' ', '-')}-full-pursuit-package.md",
            mime="text/markdown",
            key=f"download_pursuit_package_{result.get('company', 'account')}",
        )
        st.download_button(
            "Download generated pursuit package PDF",
            data=brief_pdf_bytes(f"{result.get('company', 'Account')} Full Pursuit Package", brief_markdown),
            file_name=f"{str(result.get('company', 'account')).lower().replace(' ', '-')}-full-pursuit-package.pdf",
            mime="application/pdf",
            key=f"download_pursuit_package_pdf_{result.get('company', 'account')}",
        )


def product_gap_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Gap": "Verified contact enrichment",
                "Why it matters": "Hunter is connected for professional emails, but some govcon accounts need direct dials, mobile phones, and richer org charts.",
                "Recommended update": "Add Apollo, ZoomInfo, People Data Labs, Clay, or CRM-approved enrichment as optional second-source providers.",
                "Priority": "Medium",
            },
            {
                "Gap": "Contact recency confirmation",
                "Why it matters": "The app now gates verified contacts by verification date/status, but public snippets still may not prove a person is currently in role.",
                "Recommended update": "Connect a second-source enrichment provider with explicit profile last-seen dates and direct-dial freshness.",
                "Priority": "Medium",
            },
            {
                "Gap": "SAM.gov entity and full-history detail",
                "Why it matters": "The app now pulls SAM.gov award-notice candidates, but deeper entity records and full notice-version history would improve matching.",
                "Recommended update": "Add SAM.gov entity-management lookup, notice history downloads, and persistent source evidence tables.",
                "Priority": "Medium",
            },
            {
                "Gap": "Production CRM sync",
                "Why it matters": "Local SQLite works for the prototype, but multi-user SDR teams need shared durable records and ownership.",
                "Recommended update": "Add Supabase/Airtable/Postgres or HubSpot/Salesforce sync for shared accounts, activities, dedupe, and reporting.",
                "Priority": "High",
            },
            {
                "Gap": "Email/call activity automation",
                "Why it matters": "The app can launch a 14-day HubSpot task cadence and create HubSpot tasks, notes, and calls, but it does not send emails or place calls.",
                "Recommended update": "Connect Gmail/Outlook or a sequencing tool with opt-out handling, consent/compliance controls, and reply/call tracking.",
                "Priority": "Medium",
            },
            {
                "Gap": "Source audit trail",
                "Why it matters": "The app now shows source URLs, scan timestamps, evidence snippets, and SDR actions, but does not yet store named reviewer ownership for every evidence row.",
                "Recommended update": "Add reviewer/owner fields and persist source-audit rows in Supabase for team-level compliance reporting.",
                "Priority": "Low",
            },
            {
                "Gap": "Account dedupe and subsidiaries",
                "Why it matters": "The app now flags likely duplicates by UEI, domain, normalized-name similarity, state, and address, but it does not perform automated merge writes.",
                "Recommended update": "Add explicit parent-child account records, manual merge workflow, and HubSpot merge/de-dupe review queues.",
                "Priority": "Low",
            },
        ]
    )


def best_contact_for_target(target: ContactTarget, contacts: tuple[PublicContact, ...]) -> PublicContact | None:
    matches = [contact for contact in contacts if contact_matches_role(contact, target.title)]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item.confidence, bool(item.full_name), bool(item.email), bool(item.phone)), reverse=True)[0]


def people_to_contact_dataframe(account: Account, intel: CompanyIntel | None = None) -> pd.DataFrame:
    verified_contacts = tuple(verified_contact_to_public(contact) for contact in load_verified_contacts(account.company))
    public_contacts = intel.contacts if isinstance(intel, CompanyIntel) else tuple()
    contacts = (*verified_contacts, *public_contacts)
    rows = []
    for target in contact_targets(account):
        best = best_contact_for_target(target, contacts)
        quality = quality_for_contact(best, target.title) if best else ContactQuality(
            "Not ready",
            0,
            "No contact found",
            "No source",
            "Use the role-based search link or run a verified enrichment source before sequencing.",
            ("No named public contact found for this role",),
        )
        search = search_url(target.search_query)
        rows.append(
            {
                "Rank": target.rank,
                "Confidence label": confidence_label_for_contact(best, quality) if best else "Needs verification",
                "Target role": target.title,
                "Best known person": best.full_name if best and best.full_name else "Research needed",
                "Likely title": best.title if best else target.title,
                "Email": best.email if best else "",
                "Phone": best.phone if best else "",
                "Confidence": best.confidence if best else 0,
                "Contact status": quality.status,
                "Contact score": quality.score,
                "Sequence gate": "Ready to sequence" if quality.status == "Verified" and quality.freshness in {"Fresh", "Likely recent"} else quality.status,
                "Role relevance": quality.relevance,
                "Source freshness": quality.freshness,
                "Verification next step": quality.next_step,
                "Source type": "LinkedIn signal" if best and "linkedin.com" in best.source_url.lower() else ("Public web" if best else "Manual research"),
                "Source / search URL": best.source_url if best else search,
                "Evidence snippet": short_text(best.evidence if best else target.why),
                "Why this person": best.recommended_reason if best else target.why,
                "Message angle": target.message_angle,
            }
        )
    return pd.DataFrame(rows)


def summarize_company_work(company: str, award_description: str, naics_description: str, psc_description: str, page_texts: list[str]) -> str:
    company_terms = []
    for text in page_texts:
        for line in text.splitlines():
            lower = line.lower()
            if len(line) < 40 or len(line) > 260:
                continue
            if any(term in lower for term in ["provides", "specializes", "delivers", "services include", "solutions", "capabilities"]):
                company_terms.append(re.sub(r"\s+", " ", line))
            if len(company_terms) >= 2:
                break
        if len(company_terms) >= 2:
            break

    if company_terms:
        return " ".join(company_terms)

    contract_category = naics_description or psc_description or award_description or "the awarded federal work"
    return f"Public award data indicates {company} is performing work tied to {contract_category}."


def summarize_why_won(prospect: Prospect) -> str:
    category = prospect.naics_description or prospect.psc_description or "the stated contract scope"
    agency = prospect.funding_sub_agency or prospect.awarding_sub_agency or prospect.awarding_agency or "the buying agency"
    description = prospect.description or "the listed requirement"
    return (
        f"USAspending does not publish the evaluation rationale, so this is a reasoned SDR hypothesis: "
        f"{agency} awarded {prospect.award_id} for {description}. The NAICS/PSC context points to {category}, "
        f"which suggests the company had relevant capability, eligibility, pricing, past performance, or incumbent/partner fit for that scope."
    )


def public_intel_key(company: str) -> str:
    return f"public_intel_{company}"


def sam_intel_key(company: str) -> str:
    return f"sam_intel_{company}"


def sam_message_key(company: str) -> str:
    return f"sam_message_{company}"


def pursuit_package_key(company: str) -> str:
    return f"pursuit_package_{company}"


def scan_budget_available(started_at: float) -> bool:
    return monotonic() - started_at < SCAN_BUDGET_SECONDS


@st.cache_data(ttl=86400, show_spinner=False)
def build_public_intel(
    company: str,
    award_id: str,
    award_amount: float,
    agency: str,
    description: str,
    naics_description: str,
    psc_description: str,
    psc_code: str,
) -> CompanyIntel:
    scan_started_at = monotonic()
    industry = industry_category(naics_description, psc_description, description, psc_code)
    queries = [
        f'"{company}" official website',
        f'"{company}" leadership',
        f'"{company}" "business development"',
        f'"{company}" "contracts manager"',
        f'"{company}" "{award_id}" contract award',
        f'"{company}" compliance challenge',
        f'"{company}" hiring government contract',
        f'"{company}" proposal capture',
    ]

    candidate_urls: list[str] = []
    for query in queries:
        if not scan_budget_available(scan_started_at):
            break
        for url in search_public_web(query, max_results=3):
            if url not in candidate_urls:
                candidate_urls.append(url)
        if len(candidate_urls) >= 8:
            break

    website = ""
    for url in candidate_urls:
        domain = url_domain(url)
        if not any(blocked in domain for blocked in ["usaspending.gov", "sam.gov", "govinfo.gov", "defense.gov", "prnewswire.com"]):
            website = domain_root(url)
            break

    scan_urls = list(candidate_urls[:6])
    if website and website not in scan_urls:
        scan_urls.insert(0, website)

    scanned_urls: list[str] = []
    source_urls: list[str] = []
    page_texts: list[str] = []
    contacts: list[PublicContact] = []
    page_linkedin_contacts: list[PublicContact] = []
    linkedin_signals: list[WebSearchResult] = []
    account_signals: list[AccountSignal] = []
    pain_points: list[PainPoint] = []

    index = 0
    while index < len(scan_urls) and len(scanned_urls) < 6 and scan_budget_available(scan_started_at):
        url = scan_urls[index]
        index += 1
        if url in scanned_urls:
            continue
        html, resolved_url = fetch_public_page(url)
        final_url = resolved_url or url
        if not html:
            continue
        scanned_urls.append(final_url)
        source_urls.append(final_url)
        page_text = clean_text_from_html(html)
        page_texts.append(page_text[:20_000])
        contacts.extend(extract_contacts_from_text(page_text[:35_000], final_url))
        account_signals.extend(account_signals_from_page_text(company, page_text[:35_000], final_url))
        pain_points.extend(pain_points_from_page_text(company, industry, page_text[:35_000], final_url))
        page_contacts, page_signals = linkedin_contacts_from_page(html, final_url)
        page_linkedin_contacts.extend(page_contacts)
        for signal in page_signals:
            if signal.url not in [item.url for item in linkedin_signals]:
                linkedin_signals.append(signal)

        if website and url_domain(final_url) == url_domain(website):
            for extra_url in source_links_from_html(html, final_url, limit=3):
                if extra_url not in scan_urls and len(scan_urls) < 10:
                    scan_urls.append(extra_url)

    for query in linkedin_role_queries(company)[:7]:
        if not scan_budget_available(scan_started_at):
            break
        for result in search_linkedin_web(query, max_results=2):
            if result.url not in [signal.url for signal in linkedin_signals]:
                linkedin_signals.append(result)
        if len(linkedin_signals) >= 10:
            break

    for query in account_signal_queries(company, award_id)[:8]:
        if not scan_budget_available(scan_started_at):
            break
        for result in search_web_results(query, max_results=2, fetchable_only=False):
            account_signal = signal_from_search_result(result, company)
            if account_signal.url not in [signal.url for signal in account_signals]:
                account_signals.append(account_signal)
            if "linkedin.com" in result.url.lower() and result.url not in [signal.url for signal in linkedin_signals]:
                linkedin_signals.append(result)
        if len(account_signals) >= 12:
            break

    for signal in account_signals:
        point = pain_point_from_signal(signal, industry)
        if point is not None:
            pain_points.append(point)

    if len([point for point in pain_points if point.evidence_level != "Industry benchmark to verify"]) < 2:
        pain_points.extend(industry_benchmark_pain_points(industry, company))

    linkedin_contacts = dedupe_contacts([*page_linkedin_contacts, *linkedin_contacts_from_results(tuple(linkedin_signals))])
    all_contacts = dedupe_contacts([*contacts, *linkedin_contacts])

    primary_like = Prospect(
        award_id=award_id,
        company=company,
        uei="",
        amount=award_amount,
        base_obligation_date="",
        last_modified_date="",
        start_date="",
        end_date="",
        awarding_agency=agency,
        awarding_sub_agency="",
        funding_agency=agency,
        funding_sub_agency="",
        description=description,
        naics_code="",
        naics_description=naics_description,
        psc_code=psc_code,
        psc_description=psc_description,
        address="",
        city="",
        state="",
        country="",
    )

    return CompanyIntel(
        company=company,
        website=website,
        what_they_do=summarize_company_work(company, description, naics_description, psc_description, page_texts),
        why_they_may_have_won=summarize_why_won(primary_like),
        contacts=all_contacts,
        linkedin_contacts=linkedin_contacts,
        linkedin_signals=tuple(linkedin_signals[:10]),
        account_signals=dedupe_account_signals(account_signals),
        pain_points=dedupe_pain_points(pain_points),
        sources=tuple(source_urls[:12]),
        scanned_urls=tuple(scanned_urls[:12]),
        scanned_at=datetime.now().isoformat(timespec="seconds"),
    )


def enrich_account(account: Account) -> CompanyIntel:
    primary = account.primary
    agency = primary.funding_sub_agency or primary.awarding_sub_agency or primary.awarding_agency
    return build_public_intel(
        account.company,
        primary.award_id,
        primary.amount,
        agency,
        primary.description,
        primary.naics_description,
        primary.psc_description,
        primary.psc_code,
    )


def auto_public_intel(account: Account) -> CompanyIntel | None:
    key = public_intel_key(account.company)
    intel = st.session_state.get(key)
    if isinstance(intel, CompanyIntel):
        return intel
    with st.spinner(f"Automatically scanning public sources for {account.company}..."):
        try:
            intel = enrich_account(account)
        except Exception as exc:
            st.warning(f"Automatic public intel scan could not complete: {exc}")
            return None
    st.session_state[key] = intel
    return intel


def auto_sam_context(account: Account) -> tuple[SamOpportunity, ...]:
    sam_key = sam_intel_key(account.company)
    sam_msg_key = sam_message_key(account.company)
    sam_opportunities = st.session_state.get(sam_key, tuple())
    if isinstance(sam_opportunities, tuple) and sam_opportunities:
        return sam_opportunities
    if not sam_enabled():
        st.session_state[sam_msg_key] = "SAM_API_KEY is not configured."
        return tuple()
    with st.spinner(f"Automatically pulling SAM.gov context for {account.company}..."):
        try:
            sam_opportunities, sam_message = fetch_sam_opportunities(account)
        except Exception as exc:
            st.session_state[sam_msg_key] = f"SAM.gov enrichment failed: {exc}"
            st.warning(str(st.session_state[sam_msg_key]))
            return tuple()
    st.session_state[sam_key] = sam_opportunities
    st.session_state[sam_msg_key] = sam_message
    return sam_opportunities


def auto_hunter_contacts(
    account: Account,
    intel: CompanyIntel | None,
    limit: int = 25,
) -> tuple[HunterContact, ...]:
    hunter_key = f"hunter_contacts_{account.company}"
    hunter_message_key = f"hunter_message_{account.company}"
    hunter_contacts = st.session_state.get(hunter_key, tuple())
    if isinstance(hunter_contacts, tuple) and hunter_contacts:
        return hunter_contacts
    if not hunter_enabled():
        st.session_state[hunter_message_key] = "HUNTER_API_KEY is not configured."
        return tuple()
    verified_contacts = load_verified_contacts(account.company)
    domain, _, _ = resolve_hubspot_domain(account, intel, verified_contacts, allow_public_search=False)
    if not domain and isinstance(intel, CompanyIntel) and intel.website:
        domain = clean_company_domain(intel.website)
    with st.spinner(f"Automatically checking Hunter contacts for {account.company}..."):
        hunter_contacts, hunter_message = fetch_hunter_contacts(account.company, domain, limit)
    st.session_state[hunter_key] = hunter_contacts
    st.session_state[hunter_message_key] = hunter_message
    return hunter_contacts


def public_contacts_dataframe(intel: CompanyIntel) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Name": contact.full_name or "Not named",
                "Confidence label": confidence_label_for_contact(contact),
                "Title/Role": contact.title,
                "Email": contact.email,
                "Phone": contact.phone,
                "Confidence": contact.confidence,
                "Quality status": quality_for_contact(contact).status,
                "Quality score": quality_for_contact(contact).score,
                "Source freshness": quality_for_contact(contact).freshness,
                "Relevance": quality_for_contact(contact).relevance,
                "Next verification step": quality_for_contact(contact).next_step,
                "Source type": "LinkedIn signal" if "linkedin.com" in contact.source_url.lower() else "Public web",
                "Why contact": contact.recommended_reason,
                "Source": contact.source_url,
                "Evidence": contact.evidence,
            }
            for contact in intel.contacts
        ]
    )


def public_contacts_quality_dataframe(intel: CompanyIntel) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Name": contact.full_name or "Not named",
                "Confidence label": confidence_label_for_contact(contact, quality_for_contact(contact)),
                "Title/Role": contact.title,
                "Quality status": quality_for_contact(contact).status,
                "Quality score": quality_for_contact(contact).score,
                "Relevance": quality_for_contact(contact).relevance,
                "Freshness": quality_for_contact(contact).freshness,
                "Reasons": "; ".join(quality_for_contact(contact).reasons),
                "Next step": quality_for_contact(contact).next_step,
                "Source": contact.source_url,
            }
            for contact in intel.contacts
        ]
    )


def suggested_personas(prospect: Prospect) -> list[str]:
    text = " ".join([prospect.description, prospect.naics_description, prospect.psc_description]).lower()
    personas = ["VP/Director of Business Development", "Capture Manager", "Proposal Manager"]
    if any(term in text for term in ["cyber", "software", "network", "telecom", "cloud", "data"]):
        personas.append("Chief Technology Officer")
    if any(term in text for term in ["facilities", "maintenance", "construction", "operation"]):
        personas.append("Program Operations Lead")
    personas.append("Contracts Manager")
    return personas


def contact_targets(account: Account) -> list[ContactTarget]:
    prospect = account.primary
    text = " ".join([prospect.description, prospect.naics_description, prospect.psc_description]).lower()
    targets = [
        ContactTarget(
            1,
            "VP/Director of Business Development",
            "Owns growth pipeline and cares about turning a new award into follow-on pursuits.",
            "Lead with repeatable capture process and using the win as stronger past performance.",
            f'site:linkedin.com/in "{account.company}" "business development" government',
        ),
        ContactTarget(
            2,
            "Capture Manager",
            "Closest day-to-day owner for recompetes, follow-on opportunities, and opportunity research.",
            "Lead with faster opportunity qualification, capture workspace setup, and pursuit artifacts.",
            f'site:linkedin.com/in "{account.company}" "capture manager"',
        ),
        ContactTarget(
            3,
            "Proposal Manager",
            "Feels the pain when capture intelligence, compliance matrices, and draft content are scattered.",
            "Lead with proposal speed, compliance matrix generation, and reusable boilerplate.",
            f'site:linkedin.com/in "{account.company}" "proposal manager"',
        ),
        ContactTarget(
            4,
            "Contracts Manager",
            "Responsible for award records, modifications, option years, and audit-ready evidence.",
            "Lead with contract record organization, option-year readiness, and delivery proof.",
            f'site:linkedin.com/in "{account.company}" "contracts manager"',
        ),
    ]

    if account.award_count >= 3 or account.total_amount >= 5_000_000:
        targets.insert(
            0,
            ContactTarget(
                1,
                "President/CEO or GovCon Practice Lead",
                "A high-value or repeat-award account may justify executive-level growth and process conversation.",
                "Lead with scaling federal growth without adding proposal and contract-management drag.",
                f'site:linkedin.com/in "{account.company}" president OR CEO government',
            ),
        )

    if any(term in text for term in ["cyber", "software", "network", "telecom", "cloud", "data", "satellite"]):
        targets.append(
            ContactTarget(
                5,
                "CTO/VP Engineering or Technical Program Lead",
                "Technical awards often require security evidence, delivery documentation, and technical-volume reuse.",
                "Lead with technical evidence reuse, security/compliance documentation, and faster technical proposals.",
                f'site:linkedin.com/in "{account.company}" CTO OR \"technical program\"',
            )
        )

    if any(term in text for term in ["facilities", "construction", "maintenance", "operation", "support services"]):
        targets.append(
            ContactTarget(
                5,
                "Program Operations Lead",
                "Operations-heavy awards create kickoff, staffing, subcontractor, and delivery documentation pressure.",
                "Lead with kickoff tasking, delivery notes, subcontractor coordination, and option-year evidence.",
                f'site:linkedin.com/in "{account.company}" \"program manager\" operations',
            )
        )

    ranked = []
    seen: set[str] = set()
    for target in targets:
        if target.title not in seen:
            ranked.append(ContactTarget(len(ranked) + 1, target.title, target.why, target.message_angle, target.search_query))
            seen.add(target.title)
    return ranked[:6]


def why_now_triggers(prospect: Prospect) -> list[str]:
    triggers = []
    start = parse_iso_date(prospect.start_date)
    end = parse_iso_date(prospect.end_date)
    today = date.today()
    description = prospect.description.lower()
    if start and start <= today <= start + timedelta(days=45):
        triggers.append("Award kickoff is happening now, so documentation and owner assignment are urgent.")
    elif start and today < start:
        triggers.append("The period of performance has not started yet, which makes this a clean pre-kickoff outreach window.")
    if end and end <= today + timedelta(days=365):
        triggers.append("The end date is within a year, so option-year or recompete readiness may matter soon.")
    if any(term in description for term in ["option", "idiq", "task order", "blanket", "bpa"]):
        triggers.append("The award language suggests repeat ordering or option work, which fits a follow-on capture workflow.")
    if prospect.amount >= 1_000_000:
        triggers.append("The obligation is large enough to justify executive attention and repeatable contract-management process.")
    if not triggers:
        triggers.append("The award is newly reported, creating a timely reason to congratulate and discuss follow-on growth.")
    return triggers


def score_breakdown(account: Account) -> list[str]:
    primary = account.primary
    reasons = [
        f"{primary.govdash_fit_score}/99 award fit on the strongest contract.",
        f"{account.award_count} recent award{'s' if account.award_count != 1 else ''} totaling {money(account.total_amount)}.",
    ]
    if primary.naics_code.startswith(("541", "517", "561")):
        reasons.append("NAICS category maps well to capture, proposal, IT, telecom, or support-services workflows.")
    if len(account.agencies) >= 2:
        reasons.append("Multiple buying organizations suggest expansion potential beyond one program.")
    if primary.end_date:
        reasons.append("Known performance dates support option-year, recompete, and contract evidence planning.")
    return reasons


def next_best_action(account: Account) -> str:
    if account.tier == "Tier 1":
        return "Research two named contacts today and send a personalized first-touch email."
    if account.tier == "Tier 2":
        return "Validate company domain and one capture/proposal contact before adding to sequence."
    return "Save for nurture unless the agency, NAICS, or keyword is strategically important."


def discovery_questions(prospect: Prospect) -> list[str]:
    agency = prospect.funding_sub_agency or prospect.awarding_sub_agency or prospect.awarding_agency
    return [
        f"How are you organizing kickoff requirements and owner assignments for {prospect.award_id}?",
        f"Which parts of this {agency or 'agency'} win can become reusable past performance for future pursuits?",
        "Where do proposal, capture, contracts, and delivery teams lose the most time today?",
        "How do you prepare evidence for modifications, option years, and follow-on opportunities?",
        "What would make a GovDash demo useful enough for your capture or proposal team to evaluate?",
    ]


def demo_steps(prospect: Prospect) -> list[tuple[str, str]]:
    return [
        (
            "1. Import the award",
            f"Start with award {prospect.award_id}, agency, period of performance, NAICS/PSC, and obligation value so the team has one record of truth.",
        ),
        (
            "2. Build the kickoff workspace",
            f"GovDash can organize the contract narrative around {prospect.contract_focus}, with reusable compliance notes and owner assignments.",
        ),
        (
            "3. Find the next pursuit",
            f"Use the incumbent win as past performance for similar {prospect.naics_code or 'NAICS'} opportunities at {prospect.awarding_agency or 'the buying agency'}.",
        ),
        (
            "4. Draft faster",
            "Show a proposal workflow that turns solicitation requirements into outlines, matrices, review tasks, and reusable boilerplate.",
        ),
        (
            "5. Manage proof",
            "Close with contract-management value: delivery notes, modifications, option-year readiness, and audit-ready source material.",
        ),
    ]


def demo_asset_pack(prospect: Prospect, intel: CompanyIntel | None = None) -> dict[str, str]:
    agency = prospect.funding_sub_agency or prospect.awarding_sub_agency or prospect.awarding_agency
    pain_points = getattr(intel, "pain_points", tuple()) if isinstance(intel, CompanyIntel) else tuple()
    if pain_points:
        top_pain = pain_points[0]
        pain_hypothesis = (
            f"Lead with a researched pain point: {top_pain.pain_point}. Evidence level: {top_pain.evidence_level}. "
            f"Use the source as context, then verify: {top_pain.recommended_question}"
        )
    else:
        pain_hypothesis = (
            f"The team likely needs to organize {prospect.contract_focus} while preserving reusable material for future bids. "
            "Automatic Public Intel should replace this with evidence-backed pain when public sources are available."
        )
    return {
        "Opening scene": (
            f"Start with {prospect.company}'s {money(prospect.amount)} award {prospect.award_id} with {agency}. "
            f"Show the award record, dates, NAICS/PSC, and the contract description as the source of truth."
        ),
        "Pain hypothesis": pain_hypothesis,
        "GovDash workflow": (
            "Create an award workspace, extract requirements, assign owners, generate a compliance matrix, draft reusable sections, "
            "and tag delivery evidence for option years or recompetes."
        ),
        "Proof moment": (
            "Show how one award becomes a reusable capture/proposal asset instead of a one-time PDF, spreadsheet, or shared-drive folder."
        ),
        "Close": (
            "Ask whether their capture/proposal/contracts team would benefit from seeing this same workflow mapped to one live pursuit."
        ),
    }


def outreach_copy(prospect: Prospect) -> str:
    agency = prospect.funding_sub_agency or prospect.awarding_sub_agency or prospect.awarding_agency
    return (
        f"Subject: Congrats on {prospect.award_id}\n\n"
        f"Hi {{first_name}},\n\n"
        f"Saw that {prospect.company} was listed for {money(prospect.amount)} with {agency}. "
        f"The work looks centered on {prospect.contract_focus}.\n\n"
        "GovDash helps govcon teams turn a new award into reusable capture intelligence, proposal-ready past performance, "
        "and organized contract-management evidence for follow-on work.\n\n"
        "Worth a quick look at how this award could become a repeatable pursuit and proposal workspace?\n"
    )


def call_opener(prospect: Prospect) -> str:
    agency = prospect.funding_sub_agency or prospect.awarding_sub_agency or prospect.awarding_agency
    return (
        f"Congrats on {prospect.award_id} with {agency}. I am calling because teams often use a new award like this "
        f"to tighten kickoff documentation, turn the win into reusable past performance, and prepare for follow-on work. "
        "Is capture/proposal operations the right group to speak with?"
    )


def sequence_steps(prospect: Prospect) -> list[tuple[str, str]]:
    return [
        ("Day 1 email", "Congratulate them on the award, reference agency/value, and ask whether GovDash is worth a quick look."),
        ("Day 2 LinkedIn", "Connect with a capture, proposal, contracts, or BD leader using the award as context."),
        ("Day 4 call", "Use the call opener and ask who owns proposal operations or contract evidence."),
        ("Day 7 follow-up", "Send a short demo premise around the award kickoff and future pursuit workflow."),
        ("Day 14 nurture", "Share a relevant GovDash use case: capture workspace, compliance matrix, proposal drafting, or contract management."),
    ]


def group_accounts(prospects: list[Prospect]) -> list[Account]:
    grouped: dict[str, list[Prospect]] = {}
    for prospect in prospects:
        grouped.setdefault(prospect.company, []).append(prospect)
    accounts = [Account(company=company, prospects=tuple(items)) for company, items in grouped.items()]
    return sorted(accounts, key=lambda account: (account.priority_score, account.total_amount), reverse=True)


def to_dataframe(prospects: list[Prospect]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Fit": p.govdash_fit_score,
                "Confidence label": "Official source",
                "Company": p.company,
                "Award": p.award_id,
                "Amount": money(p.amount),
                "Base obligation": p.base_obligation_date,
                "Last source update": p.last_modified_date,
                "Agency": p.awarding_agency,
                "Sub agency": p.awarding_sub_agency,
                "NAICS": f"{p.naics_code} {p.naics_description}".strip(),
                "PSC": f"{p.psc_code} {p.psc_description}".strip(),
                "Location": p.location,
            }
            for p in prospects
        ]
    )


def account_dataframe(accounts: list[Account]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Tier": account.tier,
                "Score": account.priority_score,
                "Confidence label": "Official source",
                "Company": account.company,
                "Normalized name": normalize_company_name(account.company),
                "UEI(s)": ", ".join(account_uei_values(account)),
                "Known domain": account_known_domain(account, include_verified_contacts=False),
                "Awards": account.award_count,
                "Total recent value": money(account.total_amount),
                "Largest award": money(account.largest_award),
                "Latest award": account.latest_award_date,
                "Latest source update": account.latest_source_modified_date,
                "Primary agency": account.primary.funding_sub_agency
                or account.primary.awarding_sub_agency
                or account.primary.awarding_agency,
                "Primary NAICS": f"{account.primary.naics_code} {account.primary.naics_description}".strip(),
                "Best contact": contact_targets(account)[0].title,
                "Why now": " ".join(why_now_triggers(account.primary)[:2]),
                "Next action": next_best_action(account),
            }
            for account in accounts
        ]
    )


def crm_dataframe(accounts: list[Account]) -> pd.DataFrame:
    rows = []
    for account in accounts:
        key = f"crm_{account.company}"
        crm = st.session_state.get(key, {}) or load_crm_record(account.company)
        intel = st.session_state.get(public_intel_key(account.company))
        best_public_contact = ""
        best_public_email = ""
        best_public_phone = ""
        best_public_source = ""
        best_row: dict[str, object] = {}
        best_df = people_to_contact_dataframe(account, intel if isinstance(intel, CompanyIntel) else None)
        if not best_df.empty:
            best_row = best_df.iloc[0].to_dict() if not best_df.empty else {}
        if isinstance(intel, CompanyIntel) and intel.contacts:
            best = intel.contacts[0]
            best_public_contact = best.full_name or best.title
            best_public_email = best.email
            best_public_phone = best.phone
            best_public_source = best.source_url
        rows.append(
            {
                "Company": account.company,
                "Confidence label": confidence_label("CRM account", str(best_row.get("Contact readiness", "")), str(best_row.get("Best public source", ""))),
                "Tier": account.tier,
                "Score": account.priority_score,
                "Status": crm.get("status", "New"),
                "Owner": crm.get("owner", ""),
                "Cadence stage": crm.get("cadence_stage", DEFAULT_CADENCE[0][0]),
                "Next action": crm.get("next_action", "Email"),
                "Next step date": crm.get("next_step", ""),
                "Emailed": crm.get("emailed", False),
                "Called": crm.get("called", False),
                "Email outcome": crm.get("email_outcome", ""),
                "Call outcome": crm.get("call_outcome", ""),
                "Primary persona": crm.get("persona", suggested_personas(account.primary)[0]),
                "Best contact target": contact_targets(account)[0].title,
                "Why this contact": contact_targets(account)[0].why,
                "Best public contact": best_row.get("Best known person", best_public_contact),
                "Best public email": best_row.get("Email", best_public_email),
                "Best public phone": best_row.get("Phone", best_public_phone),
                "Best public source": best_row.get("Source / search URL", best_public_source),
                "Contact readiness": best_row.get("Contact status", ""),
                "Contact score": best_row.get("Contact score", ""),
                "Contact verification next step": best_row.get("Verification next step", ""),
                "Notes": crm.get("notes", ""),
                "Award": account.primary.award_id,
                "Amount": money(account.primary.amount),
                "Agency": account.primary.funding_sub_agency
                or account.primary.awarding_sub_agency
                or account.primary.awarding_agency,
            }
        )
    return pd.DataFrame(rows)


def account_names(accounts: list[Account]) -> list[str]:
    return [account.company for account in accounts]


def set_active_company(company: str) -> None:
    st.session_state["active_company"] = company
    st.session_state["active_company_picker"] = company


def sync_active_company_picker() -> None:
    st.session_state["active_company"] = st.session_state.get("active_company_picker", "")


def ensure_active_company(accounts: list[Account]) -> Account | None:
    if not accounts:
        st.session_state.pop("active_company", None)
        st.session_state.pop("active_company_picker", None)
        return None

    names = account_names(accounts)
    current = st.session_state.get("active_company")
    if current not in names:
        current = names[0]
        st.session_state["active_company"] = current

    picker_value = st.session_state.get("active_company_picker")
    if picker_value not in names:
        st.session_state["active_company_picker"] = current

    return next(account for account in accounts if account.company == st.session_state["active_company"])


def active_account(accounts: list[Account]) -> Account:
    selected = ensure_active_company(accounts)
    if selected is None:
        raise ValueError("No active account is available.")
    return selected


def dataframe_with_links(data: pd.DataFrame, **kwargs: object) -> None:
    df = data.copy()
    column_config = {}
    for column in df.columns:
        values = df[column].dropna().astype(str)
        has_url = values.str.startswith(("http://", "https://")).any()
        link_named = any(term in column.lower() for term in ["url", "source", "link"])
        if has_url and link_named:
            column_config[column] = st.column_config.LinkColumn(
                column,
                help=f"Open {column} in a new browser tab.",
            )
    st.dataframe(df, column_config=column_config or None, **kwargs)


def render_sales_cockpit(account: Account) -> None:
    intel = st.session_state.get(public_intel_key(account.company))
    intel = intel if isinstance(intel, CompanyIntel) else None
    assessment = account_fit_assessment(account, intel)
    contact_summary = contact_quality_summary(account, intel)
    verified_contacts = load_verified_contacts(account.company)
    domain, domain_source = suggested_hubspot_domain(account, intel, verified_contacts)
    package_ready = isinstance(st.session_state.get(pursuit_package_key(account.company)), dict)
    hubspot_company_id = st.session_state.get(f"hubspot_company_id_{account.company}", "")
    primary = account.primary
    cockpit_state = "Package ready" if package_ready else ("Research ready" if intel else "Needs research")
    confidence = "Public source" if intel else "Official source"
    if contact_summary.get("status") == "Good list":
        confidence = "Verified source"

    st.markdown(
        f"""
        <div class="cockpit-shell">
          <div>
            <div class="cockpit-kicker">{html_escape(cockpit_state)} | {html_escape(str(assessment['tier']))} | {html_escape(confidence)}</div>
            <h2>{html_escape(account.company)}</h2>
            <div class="cockpit-subtitle">{html_escape(primary.award_id)} | {money(primary.amount)} | {html_escape(primary.funding_sub_agency or primary.awarding_sub_agency or primary.awarding_agency)}</div>
            <div class="cockpit-next"><b>Next best action:</b> {html_escape(str(assessment['next_move']))}</div>
          </div>
          <div class="cockpit-pill-grid">
            <span>Action score {int(assessment['score'])}</span>
            <span>{html_escape(str(contact_summary.get('status', 'No contacts')))}</span>
            <span>{html_escape(domain or 'Domain needed')}</span>
            <span>{'HubSpot linked' if hubspot_company_id else 'HubSpot not linked'}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    cockpit_cols = st.columns(5)
    cockpit_cols[0].metric("Award value", money(account.total_amount))
    cockpit_cols[1].metric("Verified contacts", len(verified_contacts))
    cockpit_cols[2].metric("Email-ready", len(email_ready_contacts(verified_contacts)))
    cockpit_cols[3].metric("Call signals", int(assessment["signal_count"]))
    cockpit_cols[4].metric("Pain signals", int(assessment["pain_count"]))
    st.caption(
        f"Confidence guide: {confidence}. Domain source: {domain_source}. "
        "Use Account Brief for the full package, Contact Finder for people, and CRM Cadence for execution."
    )


def render_source_audit_persistence(company: str, audit_df: pd.DataFrame, key_prefix: str) -> None:
    history_df = source_audit_history_dataframe(company)
    if not history_df.empty:
        with st.expander("Saved audit history"):
            dataframe_with_links(history_df, width="stretch", hide_index=True)
            st.download_button(
                "Download saved audit history CSV",
                data=history_df.to_csv(index=False),
                file_name=f"{company.lower().replace(' ', '-')}-saved-source-audit.csv",
                mime="text/csv",
                key=f"{key_prefix}_audit_history_download",
            )
    else:
        st.caption("Source evidence is generated automatically. Persistent audit snapshots are created from packaged workflows rather than manual save buttons.")


st.set_page_config(
    page_title="Application 0 | GovDash SDR Prospecting",
    page_icon="0",
    layout="wide",
)
init_database()

st.markdown(
    """
    <style>
    :root {
        --ink: #172033;
        --muted: #667085;
        --line: #d9dee8;
        --panel: #ffffff;
        --wash: #f6f8fb;
        --green: #206a5d;
        --blue: #275f9f;
        --amber: #9c6b18;
    }
    .stApp {
        background: #f7f8fa;
        color: var(--ink);
    }
    h1, h2, h3, p {
        letter-spacing: 0;
    }
    .top-band {
        border-bottom: 1px solid var(--line);
        padding: 1rem 0 1.1rem 0;
        margin-bottom: 1rem;
    }
    .top-band h1 {
        font-size: clamp(2rem, 4vw, 3.6rem);
        line-height: 1;
        margin: 0 0 .5rem 0;
    }
    .top-band p {
        color: var(--muted);
        max-width: 980px;
        margin: 0;
        font-size: 1rem;
    }
    .mini-row {
        display: flex;
        gap: .5rem;
        flex-wrap: wrap;
        margin-top: .8rem;
    }
    .mini {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .38rem .65rem;
        color: #344054;
        font-size: .88rem;
    }
    .prospect-card {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .95rem;
        margin-bottom: .75rem;
    }
    .prospect-card h3 {
        margin: 0 0 .35rem 0;
        font-size: 1.05rem;
    }
    .muted {
        color: var(--muted);
        font-size: .92rem;
    }
    .link-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: .45rem;
    }
    .link-grid a {
        display: block;
        border: 1px solid var(--line);
        background: var(--wash);
        color: var(--ink);
        text-decoration: none;
        border-radius: 8px;
        padding: .52rem .6rem;
        font-weight: 650;
        font-size: .88rem;
    }
    .demo-step {
        border-left: 4px solid var(--blue);
        background: #fff;
        padding: .75rem .9rem;
        margin-bottom: .55rem;
    }
    .contact-box {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .8rem;
    }
    .score-card {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .85rem;
        margin-bottom: .6rem;
    }
    .score-card b {
        display: block;
        margin-bottom: .25rem;
    }
    .trigger {
        border-left: 4px solid var(--green);
        background: #fff;
        padding: .65rem .8rem;
        margin-bottom: .45rem;
    }
    .tier-pill {
        display: inline-block;
        border-radius: 999px;
        padding: .25rem .55rem;
        background: #e8f3f0;
        color: #175b50;
        border: 1px solid #b8d8d1;
        font-weight: 700;
        font-size: .82rem;
    }
    .target-card {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .85rem;
        margin-bottom: .65rem;
    }
    .target-card h4 {
        margin: .25rem 0 .35rem 0;
        font-size: 1rem;
    }
    .target-rank {
        display: inline-flex;
        justify-content: center;
        align-items: center;
        width: 1.65rem;
        height: 1.65rem;
        border-radius: 999px;
        color: #fff;
        background: var(--blue);
        font-weight: 700;
        margin-right: .35rem;
    }
    .intel-card {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .9rem;
        margin-bottom: .7rem;
    }
    .intel-card b {
        display: block;
        margin-bottom: .25rem;
    }
    .source-list a {
        display: block;
        margin: 0 0 .35rem 0;
        overflow-wrap: anywhere;
    }
    .cadence-row {
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: .7rem .8rem;
        margin-bottom: .5rem;
    }
    .cockpit-shell {
        display: grid;
        grid-template-columns: minmax(0, 1.35fr) minmax(260px, .65fr);
        gap: 1rem;
        align-items: stretch;
        border: 1px solid var(--line);
        background: #fff;
        border-radius: 8px;
        padding: 1rem;
        margin: .6rem 0 1rem 0;
    }
    .cockpit-shell h2 {
        margin: .12rem 0 .35rem 0;
        font-size: 1.45rem;
        line-height: 1.15;
    }
    .cockpit-kicker {
        color: var(--blue);
        font-size: .78rem;
        font-weight: 800;
        text-transform: uppercase;
    }
    .cockpit-subtitle,
    .cockpit-next {
        color: var(--muted);
        font-size: .94rem;
        margin-top: .25rem;
    }
    .cockpit-pill-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: .45rem;
        align-content: center;
    }
    .cockpit-pill-grid span {
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: .48rem .55rem;
        background: var(--wash);
        font-size: .83rem;
        font-weight: 700;
        color: #344054;
        min-height: 2.25rem;
        display: flex;
        align-items: center;
    }
    @media (max-width: 800px) {
        .cockpit-shell {
            grid-template-columns: 1fr;
        }
        .cockpit-pill-grid {
            grid-template-columns: 1fr;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <section class="top-band">
      <h1>Application 0</h1>
      <p>SDR command center for newly reported government-contract winners. Pull live public award data, group account activity, prioritize outreach, and turn each win into a GovDash demo angle.</p>
      <div class="mini-row">
        <span class="mini">USAspending public API</span>
        <span class="mini">Public web intel scan</span>
        <span class="mini">Account-level scoring</span>
        <span class="mini">GovDash demo builder</span>
        <span class="mini">CRM-ready workflow</span>
      </div>
    </section>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Storage")
    storage_ready, storage_message = supabase_ping()
    if storage_ready:
        st.success("Using Supabase")
    elif supabase_enabled():
        st.warning("Supabase fallback")
    else:
        st.info("Using local SQLite")
    st.caption(storage_message)
    if st.session_state.get("storage_warning"):
        st.warning(str(st.session_state["storage_warning"]))

    st.header("SAM.gov")
    if sam_enabled():
        st.success("SAM API key configured")
    else:
        st.info("SAM API key missing")
    st.caption("SAM.gov enrichment runs automatically for the active account.")

    st.header("Hunter")
    if hunter_enabled():
        st.success("Hunter API key configured")
    else:
        st.info("Hunter API key missing")
    st.caption("Hunter enrichment runs automatically in Contact Finder.")

    st.header("HubSpot")
    hubspot_ready, hubspot_message = hubspot_ping()
    if hubspot_ready:
        st.success("HubSpot connected")
    elif hubspot_enabled():
        st.warning("HubSpot token issue")
    else:
        st.info("HubSpot token missing")
    st.caption(hubspot_message)

    st.header("Lead Filters")
    lookback_days = st.slider("Days back", 7, 90, DEFAULT_LOOKBACK_DAYS)
    result_limit = st.slider("Max awards", 10, 200, 50, step=10)
    min_amount = st.number_input("Minimum award amount", min_value=0, value=100000, step=50000)
    keyword = st.text_input("Keyword", placeholder="cyber, construction, satellite...")
    tier_filter = st.multiselect("Priority tiers", ["Tier 1", "Tier 2", "Tier 3"], default=["Tier 1", "Tier 2", "Tier 3"])
    active_only = st.checkbox("Active or starting soon only")
    st.caption("Data refreshes automatically when filters change. USAspending is the award source; SAM.gov context auto-loads when the API key is configured.")

end_date = date.today()
start_date = end_date - timedelta(days=lookback_days)

try:
    with st.spinner("Checking USAspending source freshness..."):
        freshness = check_usaspending_freshness(start_date, end_date, int(min_amount), keyword.strip())
    with st.spinner("Pulling recent public award data after freshness check..."):
        rows, api_messages = fetch_recent_awards(start_date, end_date, result_limit, int(min_amount), keyword.strip())
    prospects = [parse_prospect(row) for row in rows]
    st.session_state["last_refresh"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
except Exception as exc:
    st.error(f"Could not load USAspending data: {exc}")
    st.stop()

prospects = sorted(prospects, key=lambda item: (item.govdash_fit_score, item.amount), reverse=True)
if active_only:
    prospects = [prospect for prospect in prospects if prospect.urgency in {"Active now", "Starts soon"}]
accounts = [account for account in group_accounts(prospects) if account.tier in tier_filter]

metrics = st.columns(4)
metrics[0].metric("Accounts", len(accounts))
metrics[1].metric("Date range", f"{start_date:%b %d} - {end_date:%b %d}")
metrics[2].metric("Pipeline value", money(sum(account.total_amount for account in accounts)))
metrics[3].metric("Top account score", max((account.priority_score for account in accounts), default=0))

freshness_icon = {"Current": "OK", "Aging": "Check", "Stale": "Stale", "No matching data": "No data", "Unknown freshness": "Unknown"}.get(freshness.status, freshness.status)
st.caption(
    f"Freshness gate: {freshness_icon} | Checked {freshness.checked_at} | "
    f"Latest source update: {freshness.latest_modified_date or 'none'} | "
    f"Latest award date: {freshness.latest_award_date or 'none'} | "
    f"Full pull refresh: {st.session_state.get('last_refresh', 'not yet loaded')} | Cached for 30 minutes unless filters change."
)

if freshness.status == "Current":
    st.success(f"Source freshness check passed. {freshness.message}")
elif freshness.status in {"Aging", "Unknown freshness"}:
    st.warning(f"Source freshness needs review. {freshness.message}")
elif freshness.status == "Stale":
    st.error(f"Source freshness warning. {freshness.message}")
else:
    st.info(freshness.message)

with st.expander("Source Freshness Details"):
    freshness_cols = st.columns(4)
    freshness_cols[0].metric("Freshness", freshness.status)
    freshness_cols[1].metric("Lag days", freshness.lag_days if freshness.lag_days is not None else "N/A")
    freshness_cols[2].metric("Latest modified", freshness.latest_modified_date or "N/A")
    freshness_cols[3].metric("Latest award", freshness.latest_award_date or "N/A")
    st.write(
        {
            "sample_award": freshness.award_id,
            "sample_recipient": freshness.recipient,
            "sample_amount": money(freshness.amount),
            "checked_before_full_pull": freshness.checked_at,
            "filters": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "minimum_award_amount": int(min_amount),
                "keyword": keyword.strip(),
            },
        }
    )

selected_global_account = ensure_active_company(accounts)
if selected_global_account:
    selector_cols = st.columns([0.68, 0.32])
    with selector_cols[0]:
        st.selectbox(
            "Active company",
            account_names(accounts),
            key="active_company_picker",
            on_change=sync_active_company_picker,
            help="This selected company drives Public Intel, Contact Finder, CRM Cadence, Demo Builder, Outreach, and Account Brief.",
        )
    selected_global_account = active_account(accounts)
    with selector_cols[1]:
        st.metric("Selected company score", selected_global_account.priority_score, selected_global_account.tier)
    render_sales_cockpit(selected_global_account)

if api_messages or freshness.api_messages:
    with st.expander("API notes"):
        for message in freshness.api_messages:
            st.write(message)
        for message in api_messages:
            st.write(message)

tabs = st.tabs(["Account Radar", "Public Intel", "Contact Finder", "Call Prep", "CRM Cadence", "Demo Builder", "Outreach Sequence", "Account Brief", "Data Notes"])

with tabs[0]:
    st.subheader("Account Radar")
    if accounts:
        st.caption("Use the Active company selector above; every tab auto-loads for that selected account.")
        dataframe_with_links(account_dataframe(accounts), width="stretch", hide_index=True)

        st.markdown("### SDR Action Queue")
        st.caption("This dynamic score combines award fit, contact readiness, verified contacts, public call intel, pain evidence, and open CRM tasks.")
        action_queue = account_action_queue_dataframe(accounts)
        dataframe_with_links(action_queue, width="stretch", hide_index=True)

        st.markdown("### Account Dedupe & Parent/Subsidiary Risk")
        duplicate_risk = account_duplicate_risk_dataframe(accounts)
        if duplicate_risk.empty:
            st.success("No high-confidence duplicate account risks found in the current pull.")
        else:
            st.caption("Review these before syncing to HubSpot. Matches use UEI, domain, normalized-name similarity, state, and address overlap.")
            dataframe_with_links(duplicate_risk, width="stretch", hide_index=True)
            st.download_button(
                "Download duplicate-risk CSV",
                data=duplicate_risk.to_csv(index=False),
                file_name="application-0-account-duplicate-risk.csv",
                mime="text/csv",
                key="account_duplicate_risk_download",
            )

        export_cols = st.columns(3)
        export_cols[0].download_button(
            "Download account radar CSV",
            data=account_dataframe(accounts).to_csv(index=False),
            file_name="application-0-account-radar.csv",
            mime="text/csv",
            key="account_radar_download",
        )
        export_cols[1].download_button(
            "Download award-level CSV",
            data=to_dataframe(prospects).to_csv(index=False),
            file_name="application-0-awards.csv",
            mime="text/csv",
            key="award_level_download",
        )
        export_cols[2].download_button(
            "Download CRM CSV",
            data=crm_dataframe(accounts).to_csv(index=False),
            file_name="application-0-crm-export.csv",
            mime="text/csv",
            key="crm_export_download",
        )
        st.download_button(
            "Download SDR action queue CSV",
            data=action_queue.to_csv(index=False),
            file_name="application-0-sdr-action-queue.csv",
            mime="text/csv",
            key="sdr_action_queue_download",
        )

        st.markdown("### Top Account Briefs")
        for account in accounts[:5]:
            primary = account.primary
            best_target = contact_targets(account)[0]
            st.markdown(
                f"""
                <div class="prospect-card">
                  <span class="tier-pill">{account.tier} | {account.priority_score}</span>
                  <h3>{account.company}</h3>
                  <div class="muted">{account.award_count} recent award(s) | {money(account.total_amount)} total | latest {account.latest_award_date or "unknown"}</div>
                  <div class="muted"><b>Primary trigger:</b> {why_now_triggers(primary)[0]}</div>
                  <div class="muted"><b>Best contact:</b> {best_target.title} - {best_target.why}</div>
                  <div class="muted"><b>Next action:</b> {next_best_action(account)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        st.info("No accounts matched these filters. Try a longer date range, lower amount, broader keyword, or more priority tiers.")

with tabs[1]:
    if not accounts:
        st.info("No accounts to enrich. Adjust filters on the left to load recent award winners.")
    else:
        selected_intel_account = active_account(accounts)
        selected_intel = selected_intel_account.primary
        st.caption(f"Using active company: {selected_intel_account.company}")

        st.markdown(
            f"""
            <div class="prospect-card">
              <span class="tier-pill">{selected_intel_account.tier} | score {selected_intel_account.priority_score}</span>
              <h3>{html_escape(selected_intel_account.company)}</h3>
              <div class="muted"><b>What they won:</b> {html_escape(selected_intel.award_id)} | {money(selected_intel.amount)} | {html_escape(selected_intel.funding_sub_agency or selected_intel.awarding_sub_agency or selected_intel.awarding_agency)}</div>
              <div class="muted">{html_escape(selected_intel.description)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.caption(
            "Public scan searches open web results, public company pages, and public LinkedIn search-result signals. It does not bypass LinkedIn login, paywalls, robots restrictions, or invent missing emails and phone numbers."
        )

        st.markdown("### SAM.gov Intelligence")
        st.caption("Pulls official SAM.gov award notices for procurement context. Government POCs are context for the notice, not SDR targets at the awardee company.")
        sam_key = sam_intel_key(selected_intel_account.company)
        sam_msg_key = sam_message_key(selected_intel_account.company)
        sam_opportunities = auto_sam_context(selected_intel_account)
        if not isinstance(sam_opportunities, tuple):
            sam_opportunities = tuple()
        if not sam_enabled():
            st.warning("Add SAM_API_KEY to Streamlit secrets to enable SAM.gov enrichment.")
        else:
            st.caption(st.session_state.get(sam_msg_key, "SAM.gov auto-enrichment is ready for this active account."))
        if sam_opportunities:
            dataframe_with_links(sam_opportunities_dataframe(sam_opportunities), width="stretch", hide_index=True)
            st.download_button(
                "Download SAM.gov context CSV",
                data=sam_opportunities_dataframe(sam_opportunities).to_csv(index=False),
                file_name=f"{selected_intel_account.company.lower().replace(' ', '-')}-sam-gov-context.csv",
                mime="text/csv",
                key=f"sam_context_download_{selected_intel_account.company}",
            )
        elif st.session_state.get(sam_msg_key):
            st.info(str(st.session_state[sam_msg_key]))

        existing_intel = auto_public_intel(selected_intel_account)

        if isinstance(existing_intel, CompanyIntel):
            pain_points = getattr(existing_intel, "pain_points", tuple())
            st.markdown("### Evidence-Based Pain Points")
            st.caption("Company evidence is shown when public sources mention a real signal. Industry benchmark rows are hypotheses to verify, not claims about the company.")
            if pain_points:
                dataframe_with_links(pain_points_dataframe(existing_intel), width="stretch", hide_index=True)
                st.download_button(
                    "Download pain points CSV",
                    data=pain_points_dataframe(existing_intel).to_csv(index=False),
                    file_name=f"{selected_intel_account.company.lower().replace(' ', '-')}-pain-points.csv",
                    mime="text/csv",
                    key=f"pain_points_download_{selected_intel_account.company}",
                )
            else:
                st.info("No pain-point evidence was found in the quick public scan. Try role-specific searches in Contact Finder or broaden the account filters.")

            signals = getattr(existing_intel, "account_signals", tuple())
            if signals:
                st.markdown("### Call Intel Beyond The Award")
                st.caption("Use these public signals to make the first call relevant before you pivot into GovDash.")
                dataframe_with_links(account_signals_dataframe(existing_intel), width="stretch", hide_index=True)
                top_signal = signals[0]
                st.markdown(
                    f"""
                    <div class="intel-card">
                      <b>Best call opener angle</b>
                      {html_escape(top_signal.call_angle)}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.download_button(
                    "Download call intel CSV",
                    data=account_signals_dataframe(existing_intel).to_csv(index=False),
                    file_name=f"{selected_intel_account.company.lower().replace(' ', '-')}-call-intel.csv",
                    mime="text/csv",
                    key=f"call_intel_download_{selected_intel_account.company}",
                )
            else:
                st.markdown("### Call Intel Beyond The Award")
                st.caption("No announcement/interview signals were found in the quick scan. Use these live research links to manually check public sources.")
                dataframe_with_links(fallback_call_intel_links(selected_intel_account.company), width="stretch", hide_index=True)

            intel_cols = st.columns([0.55, 0.45])
            with intel_cols[0]:
                st.markdown("### Company Intel")
                if existing_intel.website:
                    st.link_button("Open likely company website", existing_intel.website)
                st.markdown(
                    f"""
                    <div class="intel-card">
                      <b>What the company appears to do</b>
                      {html_escape(existing_intel.what_they_do)}
                    </div>
                    <div class="intel-card">
                      <b>Why they may have won</b>
                      {html_escape(existing_intel.why_they_may_have_won)}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                st.markdown("### Source Pages Scanned")
                if existing_intel.sources:
                    st.markdown(
                        '<div class="source-list">'
                        + "".join(
                            f'<a href="{html_escape(url)}" target="_blank" rel="noopener noreferrer">{html_escape(url)}</a>'
                            for url in existing_intel.sources
                        )
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("No public pages could be fetched. Use the manual search links in Contact Finder.")

            with intel_cols[1]:
                st.markdown("### Public Contacts Found")
                if existing_intel.contacts:
                    summary = contact_quality_summary(selected_intel_account, existing_intel)
                    st.markdown("### Contact Readiness Gate")
                    gate_cols = st.columns(4)
                    gate_cols[0].metric("Gate", str(summary["status"]))
                    gate_cols[1].metric("Ready", int(summary["ready"]))
                    gate_cols[2].metric("Verify", int(summary["verify"]))
                    gate_cols[3].metric("Best score", int(summary["best_score"]))
                    if summary["status"] == "Good list":
                        st.success(str(summary["message"]))
                    elif summary["status"] == "Usable with verification":
                        st.warning(str(summary["message"]))
                    else:
                        st.error(str(summary["message"]))
                    dataframe_with_links(public_contacts_quality_dataframe(existing_intel), width="stretch", hide_index=True)
                    dataframe_with_links(public_contacts_dataframe(existing_intel), width="stretch", hide_index=True)
                    st.download_button(
                        "Download public intel CSV",
                        data=public_contacts_dataframe(existing_intel).to_csv(index=False),
                        file_name=f"{selected_intel_account.company.lower().replace(' ', '-')}-public-intel.csv",
                        mime="text/csv",
                        key=f"public_intel_download_{selected_intel_account.company}",
                    )
                else:
                    st.warning("No named public contacts or public emails were found in the pages scanned.")

                st.markdown("### LinkedIn Intelligence")
                linkedin_signals = getattr(existing_intel, "linkedin_signals", tuple())
                if linkedin_signals:
                    dataframe_with_links(linkedin_signals_dataframe(existing_intel), width="stretch", hide_index=True)
                else:
                    st.caption("No public LinkedIn search signals were found yet. Run the scan again to refresh LinkedIn profile, company, and job-result signals.")

                audit_df = source_audit_dataframe(selected_intel_account, existing_intel)
                if not audit_df.empty:
                    st.markdown("### Source Audit Trail")
                    st.caption("Use this to defend where each contact, pain point, and call opener came from before an SDR sequences the account.")
                    dataframe_with_links(audit_df, width="stretch", hide_index=True)
                    st.download_button(
                        "Download source audit CSV",
                        data=audit_df.to_csv(index=False),
                        file_name=f"{selected_intel_account.company.lower().replace(' ', '-')}-source-audit.csv",
                        mime="text/csv",
                        key=f"public_source_audit_download_{selected_intel_account.company}",
                    )
                    render_source_audit_persistence(selected_intel_account.company, audit_df, f"public_{selected_intel_account.company}")

                st.markdown("### What SDR Should Verify")
                for item in [
                    "Confirm the person still works at the company.",
                    "Confirm the email or phone is business contact information from the source page.",
                    "Open LinkedIn source links manually to verify current title before outreach.",
                    "Confirm the person owns BD, capture, proposal, contracts, or program execution.",
                    "Do not add personal or residential data to outreach notes.",
                ]:
                    st.markdown(f'<div class="cadence-row">{html_escape(item)}</div>', unsafe_allow_html=True)
        else:
            st.info("Automatic public-source scan did not return company intel yet. Try a broader filter or create the full package.")


with tabs[2]:
    if not accounts:
        st.info("No accounts to research. Adjust filters on the left to load recent award winners.")
    else:
        selected_contact_account = active_account(accounts)
        primary = selected_contact_account.primary
        st.caption(f"Using active company: {selected_contact_account.company}")

        st.markdown(
            f"""
            <div class="prospect-card">
              <span class="tier-pill">{selected_contact_account.tier} | score {selected_contact_account.priority_score}</span>
              <h3>{selected_contact_account.company}</h3>
              <div class="muted">{selected_contact_account.award_count} recent award(s) | {money(selected_contact_account.total_amount)} total | primary award {primary.award_id}</div>
              <div class="muted"><b>Why contact now:</b> {why_now_triggers(primary)[0]}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("### Best People To Contact")
        st.caption("The app ranks the roles most likely to care about GovDash. Public research loads automatically for the active account.")
        current_contact_intel = auto_public_intel(selected_contact_account)
        dataframe_with_links(people_to_contact_dataframe(selected_contact_account, current_contact_intel), width="stretch", hide_index=True)

        jump_links = " ".join(
            f'<a class="mini" href="#{anchor_slug(target.title)}">{html_escape(target.title)}</a>'
            for target in contact_targets(selected_contact_account)
        )
        st.markdown(f'<div class="mini-row">{jump_links}</div>', unsafe_allow_html=True)

        for target in contact_targets(selected_contact_account):
            target_anchor = anchor_slug(target.title)
            st.markdown(
                f"""
                <div class="target-card" id="{target_anchor}">
                  <span class="target-rank">{target.rank}</span><h4>{html_escape(target.title)}</h4>
                  <div class="muted"><b>Why this person:</b> {html_escape(target.why)}</div>
                  <div class="muted"><b>Message angle:</b> {html_escape(target.message_angle)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            link_cols = st.columns(3)
            link_cols[0].link_button("LinkedIn search", search_url(target.search_query))
            link_cols[1].link_button("Company site search", search_url(f'"{selected_contact_account.company}" "{target.title}"'))
            link_cols[2].link_button("Email pattern search", search_url(f'"{selected_contact_account.company}" email {target.title}'))

        st.markdown("### Actual People To Contact")
        st.markdown("### Verified Contact Storage")
        st.caption("Use this for Apollo/ZoomInfo/Hunter/Clay exports or manually verified LinkedIn/business contacts. These records persist in the app database and outrank public web guesses.")
        verified_df = verified_contacts_dataframe(selected_contact_account.company)
        if not verified_df.empty:
            st.markdown("#### Verified Contact Recency Gate")
            recency_cols = st.columns(4)
            recency_cols[0].metric("Ready", int((verified_df["Sequence gate"] == "Ready to sequence").sum()))
            recency_cols[1].metric("Verify", int(verified_df["Sequence gate"].isin(["Verify before sequence", "Verify missing fields", "Recheck before sequence"]).sum()))
            recency_cols[2].metric("Blocked", int((verified_df["Sequence gate"] == "Do not sequence").sum()))
            recency_cols[3].metric("Source-backed", int(verified_df["Evidence grade"].astype(str).str.startswith("A").sum()))
            dataframe_with_links(verified_df, width="stretch", hide_index=True)
            st.caption("Verified contacts are maintained by enrichment/package workflows. Manual delete is hidden in automatic mode.")
        else:
            st.info("No verified contacts saved for this account yet.")

        st.markdown("### HubSpot Sync")
        st.caption(
            "HubSpot matching status is shown here automatically. Company/contact writes happen from the Create Full Pursuit Package button in Account Brief."
        )
        hubspot_intel = current_contact_intel
        verified_contacts_for_sync = load_verified_contacts(selected_contact_account.company)
        sync_result_key = hubspot_sync_result_key(selected_contact_account.company)
        hubspot_domain, hubspot_domain_source, _ = resolve_hubspot_domain(
            selected_contact_account,
            hubspot_intel if isinstance(hubspot_intel, CompanyIntel) else None,
            verified_contacts_for_sync,
        )
        hubspot_domain_key = f"hubspot_domain_{selected_contact_account.company}"
        if hubspot_domain_key not in st.session_state:
            st.session_state[hubspot_domain_key] = hubspot_domain
        elif hubspot_domain and not st.session_state.get(hubspot_domain_key):
            st.session_state[hubspot_domain_key] = hubspot_domain
        st.caption(
            f"Auto domain: {hubspot_domain or 'will search on click'}"
            f" | Source: {hubspot_domain_source if hubspot_domain else 'public website search'}"
            " | Duplicate check: automatic"
        )
        hs_matches = st.session_state.get(f"hubspot_duplicate_matches_{selected_contact_account.company}", [])
        if not hs_matches and hubspot_enabled():
            try:
                hs_matches = hubspot_company_matches(selected_contact_account.company, hubspot_domain, limit=5)
                st.session_state[f"hubspot_duplicate_matches_{selected_contact_account.company}"] = hs_matches
            except requests.RequestException as exc:
                st.warning(f"Automatic HubSpot duplicate check could not complete: {exc}")
        if isinstance(hs_matches, list) and hs_matches:
            st.caption("Latest automatic HubSpot duplicate-check results. Exact matches update; likely fuzzy duplicates are blocked for review.")
            dataframe_with_links(hubspot_company_matches_dataframe(hs_matches), width="stretch", hide_index=True)
        sync_result = st.session_state.get(sync_result_key)
        if isinstance(sync_result, dict):
            render_hubspot_sync_result(sync_result)
        hubspot_company_id = st.session_state.get(f"hubspot_company_id_{selected_contact_account.company}", "")
        st.caption(f"Company ID: {hubspot_company_id or 'not synced yet'}")
        if not hubspot_enabled():
            st.warning("Add HUBSPOT_ACCESS_TOKEN to Streamlit secrets to enable HubSpot sync.")
        st.caption("To write companies, contacts, and cadence context into HubSpot, use Create Full Pursuit Package in Account Brief.")

        st.markdown("### Hunter Contact Enrichment")
        st.caption("Uses Hunter Domain Search automatically to find professional email addresses for the company domain/name. Contacts are written to CRM only when the full package workflow imports/syncs them.")
        hunter_key = f"hunter_contacts_{selected_contact_account.company}"
        hunter_message_key = f"hunter_message_{selected_contact_account.company}"
        hunter_contacts = auto_hunter_contacts(
            selected_contact_account,
            current_contact_intel if isinstance(current_contact_intel, CompanyIntel) else None,
        )
        if not isinstance(hunter_contacts, tuple):
            hunter_contacts = tuple()
        if not hunter_enabled():
            st.warning("Add HUNTER_API_KEY to Streamlit secrets to enable Hunter enrichment.")
        else:
            st.caption(st.session_state.get(hunter_message_key, "Hunter auto-enrichment is ready for this active account."))
        if hunter_contacts:
            dataframe_with_links(hunter_contacts_dataframe(hunter_contacts), width="stretch", hide_index=True)
            st.download_button(
                "Download Hunter contacts CSV",
                data=hunter_contacts_dataframe(hunter_contacts).to_csv(index=False),
                file_name=f"{selected_contact_account.company.lower().replace(' ', '-')}-hunter-contacts.csv",
                mime="text/csv",
                key=f"hunter_contacts_download_{selected_contact_account.company}",
            )
        elif st.session_state.get(hunter_message_key):
            st.info(str(st.session_state[hunter_message_key]))

        st.caption("Manual contact entry/import is hidden in automatic mode. Use Create Full Pursuit Package to import and sync email-ready contacts.")

        contact_intel = current_contact_intel

        if isinstance(contact_intel, CompanyIntel):
            summary = contact_quality_summary(selected_contact_account, contact_intel)
            st.markdown("### Contact Readiness Gate")
            gate_cols = st.columns(5)
            gate_cols[0].metric("Status", str(summary["status"]))
            gate_cols[1].metric("Ready", int(summary["ready"]))
            gate_cols[2].metric("Verify", int(summary["verify"]))
            gate_cols[3].metric("Not ready", int(summary["not_ready"]))
            gate_cols[4].metric("Best score", int(summary["best_score"]))
            if summary["status"] == "Good list":
                st.success(str(summary["message"]))
            elif summary["status"] == "Usable with verification":
                st.warning(str(summary["message"]))
            else:
                st.error(str(summary["message"]))

            audit_df = source_audit_dataframe(selected_contact_account, contact_intel)
            if not audit_df.empty:
                st.markdown("### Source Audit Trail")
                st.caption("Every usable contact, pain point, and call-intel signal should have a source, capture time, evidence snippet, and SDR action.")
                dataframe_with_links(audit_df, width="stretch", hide_index=True)
                st.download_button(
                    "Download source audit CSV",
                    data=audit_df.to_csv(index=False),
                    file_name=f"{selected_contact_account.company.lower().replace(' ', '-')}-source-audit.csv",
                    mime="text/csv",
                    key=f"contact_source_audit_download_{selected_contact_account.company}",
                )
                render_source_audit_persistence(selected_contact_account.company, audit_df, f"contact_{selected_contact_account.company}")

        if isinstance(contact_intel, CompanyIntel) and contact_intel.contacts:
            st.caption("Includes public web contacts plus LinkedIn profile-result signals. LinkedIn rows need manual verification before outreach.")
            dataframe_with_links(public_contacts_dataframe(contact_intel), width="stretch", hide_index=True)
            st.markdown("### Updated People To Contact")
            dataframe_with_links(people_to_contact_dataframe(selected_contact_account, contact_intel), width="stretch", hide_index=True)
            st.download_button(
                "Download updated people CSV",
                data=people_to_contact_dataframe(selected_contact_account, contact_intel).to_csv(index=False),
                file_name=f"{selected_contact_account.company.lower().replace(' ', '-')}-people-to-contact.csv",
                mime="text/csv",
                key=f"updated_people_download_{selected_contact_account.company}",
            )
            linkedin_contacts = getattr(contact_intel, "linkedin_contacts", tuple())
            if linkedin_contacts:
                st.markdown("### LinkedIn People Signals")
                dataframe_with_links(
                    pd.DataFrame(
                        [
                            {
                                "Name": contact.full_name or "Needs manual verification",
                                "Likely role": contact.title,
                                "Why contact": contact.recommended_reason,
                                "LinkedIn URL": contact.source_url,
                                "Evidence": contact.evidence,
                            }
                            for contact in linkedin_contacts
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )
        elif isinstance(contact_intel, CompanyIntel):
            st.info("The scan did not find a verified named contact. Use the role-based LinkedIn and company search links above and keep the account in Researching.")
        else:
            st.caption("Public contact scanning runs automatically for the active account when public sources are available.")

        saved_audit_history = source_audit_history_dataframe(selected_contact_account.company)
        if not saved_audit_history.empty:
            st.markdown("### Saved Source Audit History")
            dataframe_with_links(saved_audit_history, width="stretch", hide_index=True)
            st.download_button(
                "Download saved source audit history CSV",
                data=saved_audit_history.to_csv(index=False),
                file_name=f"{selected_contact_account.company.lower().replace(' ', '-')}-saved-source-audit.csv",
                mime="text/csv",
                key=f"contact_saved_audit_download_{selected_contact_account.company}",
            )

        st.markdown("### Contact Verification Checklist")
        checklist_cols = st.columns(4)
        checklist_cols[0].checkbox("Name verified", key=f"{selected_contact_account.company}_name_verified")
        checklist_cols[1].checkbox("Current role verified", key=f"{selected_contact_account.company}_role_verified")
        checklist_cols[2].checkbox("Company domain verified", key=f"{selected_contact_account.company}_domain_verified")
        checklist_cols[3].checkbox("Safe to sequence", key=f"{selected_contact_account.company}_safe_sequence")

        st.download_button(
            "Download contact targets CSV",
            data=pd.DataFrame(
                [
                    {
                        "Company": selected_contact_account.company,
                        "Rank": target.rank,
                        "Target title": target.title,
                        "Why": target.why,
                        "Message angle": target.message_angle,
                        "Search URL": search_url(target.search_query),
                    }
                    for target in contact_targets(selected_contact_account)
                ]
            ).to_csv(index=False),
            file_name=f"{selected_contact_account.company.lower().replace(' ', '-')}-contact-targets.csv",
            mime="text/csv",
            key=f"contact_targets_download_{selected_contact_account.company}",
        )

with tabs[3]:
    if not accounts:
        st.info("No accounts to prep. Adjust filters on the left to load recent award winners.")
    else:
        selected_prep_account = active_account(accounts)
        prep_intel = auto_public_intel(selected_prep_account)
        prep_sam = auto_sam_context(selected_prep_account)
        if not isinstance(prep_sam, tuple):
            prep_sam = tuple()
        sections = call_prep_sections(selected_prep_account, prep_intel, prep_sam)
        assessment = account_fit_assessment(selected_prep_account, prep_intel)

        st.caption(f"Using active company: {selected_prep_account.company}")
        st.markdown(
            f"""
            <div class="prospect-card">
              <span class="tier-pill">{html_escape(str(sections['headline']))}</span>
              <h3>SDR Call Prep Brief</h3>
              <div class="muted">{html_escape(str(sections['what_they_won']))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        prep_metrics = st.columns(5)
        prep_metrics[0].metric("Action score", int(assessment["score"]))
        prep_metrics[1].metric("Priority", str(assessment["tier"]))
        prep_metrics[2].metric("Verified contacts", int(assessment["verified_contacts"]))
        prep_metrics[3].metric("Call signals", int(assessment["signal_count"]))
        prep_metrics[4].metric("Pain signals", int(assessment["pain_count"]))

        if prep_intel is None:
            st.warning("Automatic public intel did not complete; this brief may contain award-based hypotheses.")

        prep_cols = st.columns([0.55, 0.45])
        with prep_cols[0]:
            st.markdown("### Account Context")
            for label in ["account_summary", "why_they_may_have_won", "best_contact", "talk_track", "next_move"]:
                title = label.replace("_", " ").title()
                st.markdown(
                    f"""
                    <div class="score-card">
                      <b>{html_escape(title)}</b>
                      {html_escape(str(sections[label]))}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            st.markdown("### Why Now")
            for item in sections["why_now"]:
                st.markdown(f'<div class="trigger">{html_escape(str(item))}</div>', unsafe_allow_html=True)

            st.markdown("### Contact Path")
            for item in sections["contact_path"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

        with prep_cols[1]:
            st.markdown("### Pain Points To Validate")
            for item in sections["pain_points"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

            st.markdown("### Call Intel")
            for item in sections["call_intel"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

            st.markdown("### SAM.gov Context")
            for item in sections["sam_gov_context"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

            st.markdown("### Likely Objections")
            for item in sections["objections"]:
                st.markdown(f'<div class="score-card">{html_escape(str(item))}</div>', unsafe_allow_html=True)

        st.markdown("### Discovery Questions")
        question_cols = st.columns(2)
        for index, question in enumerate(sections["discovery_questions"]):
            with question_cols[index % 2]:
                st.markdown(f'<div class="score-card">{html_escape(str(question))}</div>', unsafe_allow_html=True)

        st.markdown("### GovDash Demo Angle")
        for item in sections["demo_angle"]:
            st.markdown(f'<div class="demo-step">{html_escape(str(item))}</div>', unsafe_allow_html=True)

        st.download_button(
            "Download call prep brief",
            data=call_prep_markdown(selected_prep_account, prep_intel, prep_sam),
            file_name=f"{selected_prep_account.company.lower().replace(' ', '-')}-call-prep.md",
            mime="text/markdown",
            key=f"call_prep_download_{selected_prep_account.company}",
        )
        st.download_button(
            "Download call prep PDF",
            data=brief_pdf_bytes(
                f"{selected_prep_account.company} Call Prep Brief",
                call_prep_markdown(selected_prep_account, prep_intel, prep_sam),
            ),
            file_name=f"{selected_prep_account.company.lower().replace(' ', '-')}-call-prep.pdf",
            mime="application/pdf",
            key=f"call_prep_pdf_download_{selected_prep_account.company}",
        )


with tabs[4]:
    if not accounts:
        st.info("No accounts to work. Adjust filters on the left to load recent award winners.")
    else:
        selected_account = active_account(accounts)
        selected = selected_account.primary
        st.caption(f"Using active company: {selected_account.company}")

        overview_cols = st.columns([0.58, 0.42])
        with overview_cols[0]:
            st.markdown(
                f"""
                <div class="prospect-card">
                  <span class="tier-pill">{selected_account.tier} | score {selected_account.priority_score}</span>
                  <h3>{selected_account.company}</h3>
                  <div class="muted">{selected.urgency} | {money(selected.amount)} primary award | {selected.awarding_agency}</div>
                  <div class="muted">{selected.description}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.markdown("### Why Now")
            for trigger in why_now_triggers(selected):
                st.markdown(f'<div class="trigger">{trigger}</div>', unsafe_allow_html=True)

            st.markdown("### Score Reasons")
            for reason in score_breakdown(selected_account):
                st.markdown(f'<div class="score-card"><b>Signal</b>{reason}</div>', unsafe_allow_html=True)

        with overview_cols[1]:
            st.markdown("### CRM Fields")
            crm_key = f"crm_{selected_account.company}"
            current = st.session_state.get(crm_key, {}) or load_crm_record(selected_account.company)
            status_default = current.get("status", "New")
            status_index = DEFAULT_STATUSES.index(status_default) if status_default in DEFAULT_STATUSES else 0
            status = st.selectbox("Status", DEFAULT_STATUSES, index=status_index)
            owner = st.text_input("Owner", value=current.get("owner", ""))
            personas = suggested_personas(selected)
            persona_default = current.get("persona", personas[0])
            persona_index = personas.index(persona_default) if persona_default in personas else 0
            persona = st.selectbox(
                "Primary persona",
                personas,
                index=persona_index,
            )
            cadence_labels = [step[0] for step in DEFAULT_CADENCE]
            current_cadence = current.get("cadence_stage", cadence_labels[0])
            cadence_index = cadence_labels.index(current_cadence) if current_cadence in cadence_labels else 0
            cadence_stage = st.selectbox("Cadence stage", cadence_labels, index=cadence_index)
            action_options = ["Email", "Call", "LinkedIn", "Research", "Demo follow-up", "Nurture"]
            action_default = current.get("next_action", action_options[0])
            action_index = action_options.index(action_default) if action_default in action_options else 0
            next_action = st.selectbox("Next action", action_options, index=action_index)
            next_step_default = parse_iso_date(current.get("next_step", "")) or date.today() + timedelta(days=2)
            next_step = st.date_input("Next step date", value=next_step_default)
            action_cols = st.columns(2)
            emailed = action_cols[0].checkbox("Emailed", value=bool(current.get("emailed", False)))
            called = action_cols[1].checkbox("Called", value=bool(current.get("called", False)))
            email_outcomes = ["", "Not sent", "Sent", "Opened", "Replied", "Bounced", "Unsubscribed"]
            email_default = current.get("email_outcome", "")
            email_outcome = st.selectbox(
                "Email outcome",
                email_outcomes,
                index=email_outcomes.index(email_default) if email_default in email_outcomes else 0,
            )
            call_outcomes = ["", "Not called", "No answer", "Left voicemail", "Connected", "Bad number", "Referred"]
            call_default = current.get("call_outcome", "")
            call_outcome = st.selectbox(
                "Call outcome",
                call_outcomes,
                index=call_outcomes.index(call_default) if call_default in call_outcomes else 0,
            )
            notes = st.text_area("Notes", value=current.get("notes", ""), placeholder="Contact names, call notes, objection, next action...")
            st.session_state[crm_key] = {
                "status": status,
                "owner": owner,
                "persona": persona,
                "cadence_stage": cadence_stage,
                "next_action": next_action,
                "next_step": next_step.isoformat(),
                "emailed": emailed,
                "called": called,
                "email_outcome": email_outcome,
                "call_outcome": call_outcome,
                "notes": notes,
            }
            save_crm_record(selected_account.company, st.session_state[crm_key])
            st.caption("CRM fields are saved to the local Application 0 database.")

            st.markdown("### Public Contact Research")
            links = public_links(selected)
            st.markdown(
                '<div class="link-grid">'
                + "".join(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>' for label, url in links.items())
                + "</div>",
                unsafe_allow_html=True,
            )

            st.markdown("### Best Contact")
            best_target = contact_targets(selected_account)[0]
            crm_intel = auto_public_intel(selected_account)
            best_contact = best_contact_summary(selected_account, crm_intel if isinstance(crm_intel, CompanyIntel) else None)
            best_contact_body = (
                f"{best_contact['name']}"
                f"{' | ' + best_contact['email'] if best_contact['email'] else ''}"
                f"{' | ' + best_contact['phone'] if best_contact['phone'] else ''}"
                f" | {best_contact['status']}"
            )
            st.markdown(
                f"""
                <div class="target-card">
                  <h4>{html_escape(best_target.title)}</h4>
                  <div class="muted">{html_escape(best_contact_body)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.markdown("### Account Award History")
        dataframe_with_links(to_dataframe(list(selected_account.prospects)), width="stretch", hide_index=True)

        st.markdown("### Activity Timeline & Tasks")
        st.caption("Log calls, emails, LinkedIn touches, research tasks, and demo follow-ups. These activity rows are saved in Application 0 and can be pushed to the HubSpot timeline when scopes allow it.")
        activity_flash_key = f"activity_flash_{selected_account.company}"
        activity_flash = st.session_state.pop(activity_flash_key, "")
        if activity_flash:
            if str(activity_flash).startswith("HubSpot warning:"):
                st.warning(str(activity_flash).replace("HubSpot warning: ", "", 1))
            else:
                st.success(str(activity_flash))

        crm_verified_contacts = load_verified_contacts(selected_account.company)
        activity_hubspot_domain, activity_hubspot_domain_source, _ = resolve_hubspot_domain(
            selected_account,
            crm_intel if isinstance(crm_intel, CompanyIntel) else None,
            crm_verified_contacts,
        )
        activity_domain_key = f"activity_hubspot_domain_{selected_account.company}"
        if activity_domain_key not in st.session_state:
            st.session_state[activity_domain_key] = activity_hubspot_domain
        elif activity_hubspot_domain and not st.session_state.get(activity_domain_key):
            st.session_state[activity_domain_key] = activity_hubspot_domain

        hubspot_activity_cols = st.columns([1])
        hubspot_activity_domain = st.session_state.get(activity_domain_key, activity_hubspot_domain)
        hubspot_activity_cols[0].caption(
            f"HubSpot activity sync is {'ready' if hubspot_enabled() else 'disabled until HUBSPOT_ACCESS_TOKEN is configured'}. "
            f"Auto domain: {hubspot_activity_domain or 'will search on click'}"
            f" | Source: {activity_hubspot_domain_source if activity_hubspot_domain else 'public website search'}."
        )

        verified_names = [contact.full_name for contact in crm_verified_contacts if contact.full_name]
        default_contact = verified_names[0] if verified_names else best_contact_summary(selected_account, crm_intel if isinstance(crm_intel, CompanyIntel) else None)["name"]

        st.markdown("### Launch 14-Day HubSpot Cadence")
        st.caption("Creates the six recommended follow-up activities in Application 0 and, when enabled, creates matching HubSpot tasks associated to the company and selected verified contact.")
        cadence_cols = st.columns([0.34, 0.22, 0.22, 0.22])
        cadence_contact = cadence_cols[0].selectbox(
            "Cadence contact",
            [""] + verified_names,
            key=f"cadence_contact_{selected_account.company}",
            help="Save a verified contact first so the cadence can associate to the right HubSpot contact.",
        )
        cadence_start = cadence_cols[1].date_input(
            "Cadence start",
            value=date.today(),
            key=f"cadence_start_{selected_account.company}",
        )
        sync_cadence_to_hubspot = cadence_cols[2].checkbox(
            "Create HubSpot tasks",
            value=hubspot_enabled(),
            disabled=not hubspot_enabled(),
            key=f"sync_cadence_to_hubspot_{selected_account.company}",
        )
        cadence_activities = build_cadence_activities(selected_account, cadence_contact, cadence_start)
        cadence_cols[3].metric("Tasks", len(cadence_activities))
        verified_contact_by_name = {contact.full_name: contact for contact in crm_verified_contacts if contact.full_name}
        selected_cadence_contact = verified_contact_by_name.get(cadence_contact)
        cadence_gate = verified_contact_gate(selected_cadence_contact)["gate"] if selected_cadence_contact else "No verified contact selected"
        if not verified_names:
            st.warning("Save at least one verified contact before launching a HubSpot cadence.")
        elif selected_cadence_contact:
            if cadence_gate == "Ready to sequence":
                st.success(f"Compliance gate passed for {selected_cadence_contact.full_name}: {cadence_gate}.")
            else:
                st.warning(f"Compliance gate blocked launch for {selected_cadence_contact.full_name}: {cadence_gate}. Update the verified contact before launching cadence.")
        with st.expander("Preview cadence tasks"):
            dataframe_with_links(cadence_preview_dataframe(cadence_activities), width="stretch", hide_index=True)
        st.caption("Cadence tasks are prepared automatically. Use Create Full Pursuit Package to write synced package context; manual cadence launch is hidden in automatic mode.")

        loaded_activities = load_crm_activities(selected_account.company)
        activity_df = pd.DataFrame(
            [
                {
                    "ID": activity.id,
                    "Done": activity.completed,
                    "Due date": activity.due_date,
                    "Type": activity.activity_type,
                    "Contact": activity.contact_name,
                    "Subject": activity.subject,
                    "Outcome": activity.outcome,
                    "Notes": activity.notes,
                    "Created": activity.created_at,
                }
                for activity in loaded_activities
            ]
        )
        if activity_df.empty:
            st.info("No activities logged for this account yet.")
        else:
            dataframe_with_links(activity_df, width="stretch", hide_index=True)
        st.caption("Manual activity logging and activity sync are hidden in automatic mode. Package creation prepares the next workflow and HubSpot context.")

        st.markdown("### Recommended Cadence")
        cadence_cols = st.columns(2)
        for index, (day, action, detail) in enumerate(DEFAULT_CADENCE):
            with cadence_cols[index % 2]:
                st.markdown(
                    f"""
                    <div class="cadence-row">
                      <b>{html_escape(day)} - {html_escape(action)}</b><br>
                      <span class="muted">{html_escape(detail)}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.download_button(
            "Download CRM cadence CSV",
            data=crm_dataframe(accounts).to_csv(index=False),
            file_name="application-0-crm-cadence.csv",
            mime="text/csv",
            key="crm_cadence_download",
        )

with tabs[5]:
    if not accounts:
        st.info("No accounts to demo. Adjust filters on the left to load recent award winners.")
    else:
        selected_demo_account = active_account(accounts)
        st.caption(f"Using active company: {selected_demo_account.company}")
        demo_awards = sorted(selected_demo_account.prospects, key=lambda prospect: prospect.amount, reverse=True)
        selected_award_label = st.selectbox(
            "Select award/use case",
            [
                f"{prospect.award_id} | {money(prospect.amount)} | {prospect.description[:80]}"
                for prospect in demo_awards
            ],
            key=f"demo_award_{selected_demo_account.company}",
        )
        selected_demo = demo_awards[
            [
                f"{prospect.award_id} | {money(prospect.amount)} | {prospect.description[:80]}"
                for prospect in demo_awards
            ].index(selected_award_label)
        ]

        st.markdown(
            f"""
            <div class="prospect-card">
              <span class="tier-pill">{selected_demo_account.tier} | {selected_demo_account.priority_score}</span>
              <h3>Demo premise: {selected_demo.company}</h3>
              <div class="muted">Use their {money(selected_demo.amount)} award as the opening scene, then show how GovDash turns it into capture, proposal, and contract-management motion.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("### Demo Asset Pack")
        demo_intel = auto_public_intel(selected_demo_account)
        pack = demo_asset_pack(selected_demo, demo_intel if isinstance(demo_intel, CompanyIntel) else None)
        for label, body in pack.items():
            st.markdown(
                f"""
                <div class="score-card">
                  <b>{label}</b>
                  {body}
                </div>
                """,
                unsafe_allow_html=True,
            )

        demo_cols = st.columns([0.5, 0.5])
        with demo_cols[0]:
            st.markdown("### Demo Flow")
            for title, body in demo_steps(selected_demo):
                st.markdown(
                    f"""
                    <div class="demo-step">
                      <b>{title}</b><br>
                      <span class="muted">{body}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        with demo_cols[1]:
            st.markdown("### Discovery Questions")
            for question in discovery_questions(selected_demo):
                st.markdown(f'<div class="score-card">{question}</div>', unsafe_allow_html=True)

        st.markdown("### Demo Talk Track")
        st.write(
            f"Lead with: \"You just won {selected_demo.award_id}. In GovDash, we would use that win to centralize the award record, "
            f"extract reusable past-performance language, prepare for modifications and option years, and identify similar opportunities "
            f"at {selected_demo.awarding_agency or 'the buying agency'}.\""
        )

with tabs[6]:
    if not accounts:
        st.info("No accounts to sequence. Adjust filters on the left to load recent award winners.")
    else:
        selected_sequence_account = active_account(accounts)
        selected_sequence = selected_sequence_account.primary
        st.caption(f"Using active company: {selected_sequence_account.company}")
        sequence_intel = auto_public_intel(selected_sequence_account)
        sequence_signals = getattr(sequence_intel, "account_signals", tuple()) if isinstance(sequence_intel, CompanyIntel) else tuple()

        st.markdown("### Relevant Call Intel")
        if sequence_signals:
            for signal in sequence_signals[:3]:
                st.markdown(
                    f"""
                    <div class="cadence-row">
                      <b>{html_escape(signal.signal_type)}: {html_escape(signal.title)}</b><br>
                      <span class="muted">{html_escape(signal.call_angle)}</span><br>
                      <span class="muted">{html_escape(signal.source)} | {html_escape(signal.recency_hint)}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.caption("Automatic public-intel scan did not find announcements, LinkedIn updates, podcasts, interviews, or other call triggers yet.")

        sequence_pains = getattr(sequence_intel, "pain_points", tuple()) if isinstance(sequence_intel, CompanyIntel) else tuple()
        st.markdown("### Pain Points To Validate")
        if sequence_pains:
            for point in sequence_pains[:3]:
                st.markdown(
                    f"""
                    <div class="cadence-row">
                      <b>{html_escape(point.pain_point)}</b><br>
                      <span class="muted">{html_escape(point.evidence_level)} | {html_escape(point.industry)} | {html_escape(point.severity)}</span><br>
                      <span class="muted">{html_escape(point.recommended_question)}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.caption("Automatic public-intel scan did not find evidence-backed or industry-benchmark pain points yet.")

        st.markdown("### First-Touch Email")
        st.code(outreach_copy(selected_sequence), language="text")

        st.markdown("### Call Opener")
        st.code(call_opener(selected_sequence), language="text")

        st.markdown("### 14-Day Sequence")
        for title, body in sequence_steps(selected_sequence):
            st.markdown(
                f"""
                <div class="demo-step">
                  <b>{title}</b><br>
                  <span class="muted">{body}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[7]:
    if not accounts:
        st.info("No accounts to brief. Adjust filters on the left to load recent award winners.")
    else:
        selected_brief_account = active_account(accounts)
        brief_intel = auto_public_intel(selected_brief_account)
        brief_sam = auto_sam_context(selected_brief_account)
        if not isinstance(brief_sam, tuple):
            brief_sam = tuple()
        brief = account_brief_sections(selected_brief_account, brief_intel, brief_sam)
        company_brief = brief["company"]
        award_brief = brief["award"]
        contact_brief = brief["best_contact"]
        contact_counts = brief["contact_counts"]
        crm_brief = brief["crm"]
        assessment_brief = brief["assessment"]

        st.caption(f"Using active company: {selected_brief_account.company}")
        package_key = pursuit_package_key(selected_brief_account.company)
        package_cols = st.columns([0.48, 0.2, 0.32])
        package_cols[0].caption(
            "Create the complete SDR pursuit package from one click: public intel, SAM.gov, Hunter, HubSpot duplicate/sync, brief, and cadence prep."
        )
        package_sync_hubspot = package_cols[1].checkbox(
            "Sync HubSpot",
            value=hubspot_enabled(),
            disabled=not hubspot_enabled(),
            key=f"package_sync_hubspot_{selected_brief_account.company}",
        )
        if package_cols[2].button(
            "Create Full Pursuit Package",
            key=f"create_pursuit_package_{selected_brief_account.company}",
            use_container_width=True,
        ):
            with st.spinner("Creating full pursuit package..."):
                build_full_pursuit_package(selected_brief_account, package_sync_hubspot)
            st.rerun()

        existing_package = st.session_state.get(package_key)
        if isinstance(existing_package, dict):
            render_pursuit_package_result(existing_package)

        st.markdown(
            f"""
            <div class="prospect-card">
              <span class="tier-pill">{html_escape(str(brief['headline']))}</span>
              <h3>{html_escape(selected_brief_account.company)} Account Research Brief</h3>
              <div class="muted">{html_escape(str(brief['executive_summary']))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        brief_metrics = st.columns(6)
        brief_metrics[0].metric("Action score", int(assessment_brief["score"]))
        brief_metrics[1].metric("Priority", str(assessment_brief["tier"]))
        brief_metrics[2].metric("Award value", money(selected_brief_account.total_amount))
        brief_metrics[3].metric("Email-ready contacts", int(contact_counts["email_ready"]))
        brief_metrics[4].metric("Call signals", int(assessment_brief["signal_count"]))
        brief_metrics[5].metric("Pain signals", int(assessment_brief["pain_count"]))

        if brief_intel is None:
            st.warning("Automatic public intel did not complete; this may remain an award-based brief.")

        brief_cols = st.columns([0.52, 0.48])
        with brief_cols[0]:
            st.markdown("### Company & Contract")
            st.markdown(
                f"""
                <div class="score-card">
                  <b>What they do</b>
                  {html_escape(str(company_brief['what_they_do']))}
                </div>
                <div class="score-card">
                  <b>What they won</b>
                  {html_escape(str(award_brief['award_id']))} | {html_escape(str(award_brief['value']))} | {html_escape(str(award_brief['agency']))}<br>
                  <span class="muted">{html_escape(str(award_brief['description']))}</span>
                </div>
                <div class="score-card">
                  <b>Why they may have won</b>
                  {html_escape(str(award_brief['why_they_may_have_won']))}
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.markdown("### Best Contact")
            st.markdown(
                f"""
                <div class="target-card">
                  <h4>{html_escape(str(contact_brief['name']))}</h4>
                  <div class="muted">{html_escape(str(contact_brief['title']))} | {html_escape(str(contact_brief['status']))}</div>
                  <div class="muted">Email: {html_escape(str(contact_brief['email'] or 'not verified yet'))} | Phone: {html_escape(str(contact_brief['phone'] or 'not verified yet'))}</div>
                  <div class="muted">{html_escape(str(contact_brief['reason']))}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if contact_brief.get("source"):
                st.link_button("Open contact source/search", str(contact_brief["source"]))

            st.markdown("### Pain Points To Validate")
            for item in brief["pain_points"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

        with brief_cols[1]:
            st.markdown("### SDR Talk Track")
            st.markdown(f'<div class="intel-card">{html_escape(str(brief["talk_track"]))}</div>', unsafe_allow_html=True)

            st.markdown("### Call Intel")
            for item in brief["call_intel"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

            st.markdown("### GovDash Demo")
            for label, body in brief["govdash_demo"]:
                st.markdown(
                    f"""
                    <div class="demo-step">
                      <b>{html_escape(str(label))}</b><br>
                      <span class="muted">{html_escape(str(body))}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        lower_cols = st.columns([0.34, 0.33, 0.33])
        with lower_cols[0]:
            st.markdown("### Discovery")
            for question in brief["discovery_questions"]:
                st.markdown(f'<div class="score-card">{html_escape(str(question))}</div>', unsafe_allow_html=True)
        with lower_cols[1]:
            st.markdown("### CRM State")
            crm_rows = [
                f"Status: {crm_brief['status']}",
                f"Owner: {crm_brief['owner'] or 'Unassigned'}",
                f"Persona: {crm_brief['persona']}",
                f"Cadence: {crm_brief['cadence_stage']}",
                f"Next: {crm_brief['next_action']} {crm_brief['next_step']}",
            ]
            for row in crm_rows:
                st.markdown(f'<div class="cadence-row">{html_escape(str(row))}</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="score-card"><b>Next move</b>{html_escape(str(brief["next_move"]))}</div>', unsafe_allow_html=True)
        with lower_cols[2]:
            st.markdown("### Trust Gaps")
            for item in brief["trust_gaps"]:
                st.markdown(f'<div class="cadence-row">{html_escape(str(item))}</div>', unsafe_allow_html=True)

        source_urls = company_brief["sources"] if isinstance(company_brief.get("sources"), list) else []
        if source_urls:
            st.markdown("### Source Links")
            st.markdown(
                '<div class="source-list">'
                + "".join(
                    f'<a href="{html_escape(url)}" target="_blank" rel="noopener noreferrer">{html_escape(url)}</a>'
                    for url in source_urls
                )
                + "</div>",
                unsafe_allow_html=True,
            )

        st.download_button(
            "Download account research brief",
            data=account_brief_markdown(selected_brief_account, brief_intel, brief_sam),
            file_name=f"{selected_brief_account.company.lower().replace(' ', '-')}-account-brief.md",
            mime="text/markdown",
            key=f"account_brief_download_{selected_brief_account.company}",
        )
        st.download_button(
            "Download account research PDF",
            data=brief_pdf_bytes(
                f"{selected_brief_account.company} Account Research Brief",
                account_brief_markdown(selected_brief_account, brief_intel, brief_sam),
            ),
            file_name=f"{selected_brief_account.company.lower().replace(' ', '-')}-account-brief.pdf",
            mime="application/pdf",
            key=f"account_brief_pdf_download_{selected_brief_account.company}",
        )


with tabs[8]:
    st.markdown("### Source Strategy")
    st.write(
        "Application 0 uses the USAspending public API because it does not require authorization and exposes recent federal contract-award data. "
        "Before the full pull, the app runs a one-record freshness check sorted by USAspending Last Modified Date using the same filters. "
        "The app retries transient API failures and caches successful lead responses for 30 minutes to keep the live workflow responsive."
    )
    st.markdown("### Freshness Rules")
    st.write(
        "Current means the newest matching USAspending modification is within 7 days. Aging means 8 to 14 days. "
        "Stale means more than 14 days. No matching data means the source responded but the current filters found no records."
    )
    st.markdown("### Contact Quality Guardrails")
    st.write(
        "The app avoids inventing contact details. Public Intel only shows names, emails, phones, and evidence found on public pages the app could fetch. "
        "LinkedIn intelligence uses public search-result signals and source links; it does not scrape protected LinkedIn pages. "
        "Treat those findings as SDR research, verify role and business contact status, and do not store personal or residential information."
    )
    st.markdown("### Contact List Freshness Rules")
    st.write(
        "The Contact Readiness Gate scores each row by whether it has a named person, role relevance, source type, visible recency, and business contact data. "
        "Verified contacts now also show a Sequence Gate, Verified Age, Evidence Grade, and SDR action. "
        "Ready to sequence means the person has a current-role verification, a source, and a usable business contact path. Verify first or Recheck before sequence means the SDR should confirm role and source recency before outreach."
    )
    st.markdown("### Confidence Labels")
    st.write(
        "Major account, contact, pain-point, call-intel, source-audit, HubSpot, Hunter, SAM.gov, and pursuit-package tables now include a Confidence label. "
        "Labels distinguish Official source, Verified source, Vendor enrichment, Public source, Hypothesis, Stale or needs recheck, and Needs verification so SDRs know what can be used directly and what needs review."
    )
    st.markdown("### Sales Cockpit")
    st.write(
        "The selected-account cockpit at the top of the app shows account state, action score, contact readiness, domain status, HubSpot link status, award value, call signals, pain signals, and the next best action. "
        "Use it as the command center, then open Account Brief for the full package, Contact Finder for people, and CRM Cadence for execution."
    )
    st.markdown("### Source Audit Trail")
    st.write(
        "Public Intel and Contact Finder now build an audit table for verified contacts, public contacts, pain points, call-intel signals, and scanned pages. "
        "Each row carries the source URL, captured or verified timestamp, evidence snippet, audit status, and recommended SDR action so reps can defend why a contact or pain point was used."
    )
    st.markdown("### Storage Backend")
    st.write(
        "CRM accounts, verified contacts, and activity history use Supabase when `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are configured and the Supabase schema has been created. "
        "Without those secrets, Application 0 uses the local SQLite file as a fallback so development still works."
    )
    st.markdown("### SAM.gov Enrichment")
    st.write(
        "When `SAM_API_KEY` is configured, Public Intel can pull official SAM.gov award-notice candidates for the active account. "
        "These rows add procurement context such as solicitation number, set-aside, NAICS/PSC, contracting organization, place of performance, and government point of contact. "
        "Government POCs are shown as notice context, not contractor SDR targets."
    )
    st.markdown("### Hunter Enrichment")
    st.write(
        "When `HUNTER_API_KEY` is configured, Contact Finder can run Hunter Domain Search for the active company. "
        "Hunter rows are treated as review-first enrichment leads and can be saved into verified contacts after the SDR checks role fit and source evidence."
    )
    st.markdown("### HubSpot Sync")
    st.write(
        "When `HUBSPOT_ACCESS_TOKEN` is configured, Contact Finder can sync the active company and every verified contact with an email to HubSpot in one click. "
        "No domain typing or separate duplicate-check button is needed: the app auto-detects the company domain from company intel, verified-contact email domains, or public website search. "
        "Create Full Pursuit Package is the explicit write action: it checks HubSpot by domain, exact name, and fuzzy name-token matches so exact matches update and likely duplicates are blocked for review. "
        "If no saved verified contact has an email and Hunter is configured, package creation attempts Hunter enrichment, imports up to five email-ready contacts as review-needed records, and syncs them to HubSpot. "
        "After each sync, Contact Finder shows a Last HubSpot Sync Results panel with the domain decision, duplicate result, company ID, contact counts, skipped contacts, errors, and next action. "
        "CRM Cadence can also create HubSpot tasks, notes, and calls when the private app has the needed activity scopes. "
        "The 14-day cadence launcher creates six dated follow-up activities locally and matching HubSpot tasks in one click. "
        "If HubSpot denies an activity object, Application 0 still saves the row in Supabase/local storage and shows a warning."
    )
    st.markdown("### Account Brief")
    st.write(
        "The Account Brief tab packages the active company into one SDR-ready brief: executive summary, company research, contract trigger, best contact, "
        "pain points to validate, call intel, GovDash demo angle, CRM state, trust gaps, sources, and downloadable Markdown/PDF versions. "
        "Public Intel, SAM.gov enrichment, Hunter enrichment, duplicate checking, and tab content auto-load for the active account; Create Full Pursuit Package is the one explicit action for package generation and CRM/HubSpot writes."
    )
    st.markdown("### Gaps & Recommended Updates")
    dataframe_with_links(product_gap_dataframe(), width="stretch", hide_index=True)
    st.markdown("### What To Add Next")
    st.write(
        "My strongest recommendation: move the local SQLite CRM to a shared production database and connect an approved contact-enrichment or CRM provider. "
        "The app now has account scoring, call prep, verified contacts, and activity logging, but a team workflow needs shared permissions, dedupe, sync, and reporting."
    )
