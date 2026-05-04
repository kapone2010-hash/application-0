from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from time import monotonic, sleep
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import pandas as pd
import requests
import streamlit as st


USASPENDING_AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PUBLIC_SEARCH_URL = "https://duckduckgo.com/html/"
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
    if score >= 75 and contact.full_name:
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
    if not isinstance(intel, CompanyIntel) or not intel.contacts:
        return {
            "status": "No scanned contacts",
            "ready": 0,
            "verify": 0,
            "not_ready": len(contact_targets(account)),
            "best_score": 0,
            "message": "Run the public/contact scan before using the contact list.",
        }

    rows = people_to_contact_dataframe(account, intel)
    ready = int((rows["Contact status"] == "Ready to verify").sum()) if "Contact status" in rows else 0
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


def product_gap_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Gap": "Verified contact enrichment",
                "Why it matters": "Public search is inconsistent for direct emails, phones, and current titles.",
                "Recommended update": "Connect Apollo, ZoomInfo, Hunter, People Data Labs, Clearbit, Clay, or a CRM-approved enrichment provider.",
                "Priority": "High",
            },
            {
                "Gap": "Contact recency confirmation",
                "Why it matters": "LinkedIn/search snippets may not show whether a person is still in role.",
                "Recommended update": "Add an enrichment timestamp, profile last-seen date, and a manual verification checkbox before export.",
                "Priority": "High",
            },
            {
                "Gap": "SAM.gov award detail",
                "Why it matters": "USAspending is strong for awards, but SAM.gov can add solicitation/notice context and procurement history.",
                "Recommended update": "Add SAM.gov API key support for notice history, set-aside, place of performance, and solicitation links.",
                "Priority": "High",
            },
            {
                "Gap": "CRM persistence",
                "Why it matters": "Streamlit session state is not a durable CRM database.",
                "Recommended update": "Add Supabase/Airtable/Postgres or HubSpot/Salesforce sync for SDR ownership, activity history, and dedupe.",
                "Priority": "High",
            },
            {
                "Gap": "Email/call activity automation",
                "Why it matters": "The app tracks intent but does not send/log email or calls automatically.",
                "Recommended update": "Integrate HubSpot/Salesforce, Gmail/Outlook, or a sequencing platform with opt-out/compliance controls.",
                "Priority": "Medium",
            },
            {
                "Gap": "Source audit trail",
                "Why it matters": "SDRs need to defend why a contact or pain point was selected.",
                "Recommended update": "Store scan timestamp, source URL, evidence snippet, confidence, and manual verifier name for each row.",
                "Priority": "Medium",
            },
            {
                "Gap": "Account dedupe and subsidiaries",
                "Why it matters": "Government award recipients can appear under subsidiaries, DBAs, UEIs, and parent companies.",
                "Recommended update": "Normalize by UEI/CAGE/domain and add parent-child account mapping.",
                "Priority": "Medium",
            },
        ]
    )


def best_contact_for_target(target: ContactTarget, contacts: tuple[PublicContact, ...]) -> PublicContact | None:
    matches = [contact for contact in contacts if contact_matches_role(contact, target.title)]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item.confidence, bool(item.full_name), bool(item.email), bool(item.phone)), reverse=True)[0]


def people_to_contact_dataframe(account: Account, intel: CompanyIntel | None = None) -> pd.DataFrame:
    contacts = intel.contacts if isinstance(intel, CompanyIntel) else tuple()
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
                "Target role": target.title,
                "Best known person": best.full_name if best and best.full_name else "Research needed",
                "Likely title": best.title if best else target.title,
                "Email": best.email if best else "",
                "Phone": best.phone if best else "",
                "Confidence": best.confidence if best else "",
                "Contact status": quality.status,
                "Contact score": quality.score,
                "Role relevance": quality.relevance,
                "Source freshness": quality.freshness,
                "Verification next step": quality.next_step,
                "Source type": "LinkedIn signal" if best and "linkedin.com" in best.source_url.lower() else ("Public web" if best else "Manual research"),
                "Source / search URL": best.source_url if best else search,
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


def public_contacts_dataframe(intel: CompanyIntel) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Name": contact.full_name or "Not named",
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
            "Run Public Intel first to replace this with evidence-backed pain."
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
                "Company": account.company,
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
        crm = st.session_state.get(key, {})
        intel = st.session_state.get(public_intel_key(account.company))
        best_public_contact = ""
        best_public_email = ""
        best_public_phone = ""
        best_public_source = ""
        best_row: dict[str, object] = {}
        if isinstance(intel, CompanyIntel) and intel.contacts:
            best_df = people_to_contact_dataframe(account, intel)
            best_row = best_df.iloc[0].to_dict() if not best_df.empty else {}
            best = intel.contacts[0]
            best_public_contact = best.full_name or best.title
            best_public_email = best.email
            best_public_phone = best.phone
            best_public_source = best.source_url
        rows.append(
            {
                "Company": account.company,
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
                "Best public contact": best_row.get("Best known person", best_public_contact) if isinstance(intel, CompanyIntel) else best_public_contact,
                "Best public email": best_public_email,
                "Best public phone": best_public_phone,
                "Best public source": best_row.get("Source / search URL", best_public_source) if isinstance(intel, CompanyIntel) else best_public_source,
                "Contact readiness": best_row.get("Contact status", "") if isinstance(intel, CompanyIntel) else "",
                "Contact score": best_row.get("Contact score", "") if isinstance(intel, CompanyIntel) else "",
                "Contact verification next step": best_row.get("Verification next step", "") if isinstance(intel, CompanyIntel) else "",
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


st.set_page_config(
    page_title="Application 0 | GovDash SDR Prospecting",
    page_icon="0",
    layout="wide",
)

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
    st.header("Lead Filters")
    lookback_days = st.slider("Days back", 7, 90, DEFAULT_LOOKBACK_DAYS)
    result_limit = st.slider("Max awards", 10, 200, 50, step=10)
    min_amount = st.number_input("Minimum award amount", min_value=0, value=100000, step=50000)
    keyword = st.text_input("Keyword", placeholder="cyber, construction, satellite...")
    tier_filter = st.multiselect("Priority tiers", ["Tier 1", "Tier 2", "Tier 3"], default=["Tier 1", "Tier 2", "Tier 3"])
    active_only = st.checkbox("Active or starting soon only")
    if st.button("Refresh live data"):
        fetch_recent_awards.clear()
        check_usaspending_freshness.clear()
        st.rerun()
    st.caption("Data comes from USAspending award search. SAM.gov Contract Awards can be added when you provide a SAM.gov public API key.")

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
    f"Full pull refresh: {st.session_state.get('last_refresh', 'not yet loaded')} | Cached for 30 minutes unless filters change or Refresh live data is clicked."
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
            help="This selected company drives Public Intel, Contact Finder, CRM Cadence, Demo Builder, and Outreach.",
        )
    selected_global_account = active_account(accounts)
    with selector_cols[1]:
        st.metric("Selected company score", selected_global_account.priority_score, selected_global_account.tier)

if api_messages or freshness.api_messages:
    with st.expander("API notes"):
        for message in freshness.api_messages:
            st.write(message)
        for message in api_messages:
            st.write(message)

tabs = st.tabs(["Account Radar", "Public Intel", "Contact Finder", "CRM Cadence", "Demo Builder", "Outreach Sequence", "Data Notes"])

with tabs[0]:
    st.subheader("Account Radar")
    if accounts:
        st.markdown("### Company Buttons")
        st.caption("Click a company here to update every tab and field that depends on the selected account.")
        button_cols = st.columns(min(4, len(accounts)))
        for index, account in enumerate(accounts[:12]):
            with button_cols[index % len(button_cols)]:
                is_active = account.company == active_account(accounts).company
                label_prefix = "Selected" if is_active else "Use"
                st.button(
                    f"{label_prefix}: {account.company}",
                    key=f"use_company_{index}_{account.company}",
                    on_click=set_active_company,
                    args=(account.company,),
                    use_container_width=True,
                )

        dataframe_with_links(account_dataframe(accounts), width="stretch", hide_index=True)

        export_cols = st.columns(3)
        export_cols[0].download_button(
            "Download account radar CSV",
            data=account_dataframe(accounts).to_csv(index=False),
            file_name="application-0-account-radar.csv",
            mime="text/csv",
        )
        export_cols[1].download_button(
            "Download award-level CSV",
            data=to_dataframe(prospects).to_csv(index=False),
            file_name="application-0-awards.csv",
            mime="text/csv",
        )
        export_cols[2].download_button(
            "Download CRM CSV",
            data=crm_dataframe(accounts).to_csv(index=False),
            file_name="application-0-crm-export.csv",
            mime="text/csv",
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

        run_scan = st.button("Run public scan", key=f"run_scan_{selected_intel_account.company}")
        existing_intel = st.session_state.get(public_intel_key(selected_intel_account.company))
        if run_scan:
            with st.spinner("Scanning public sources for company intel and contact evidence..."):
                existing_intel = enrich_account(selected_intel_account)
                st.session_state[public_intel_key(selected_intel_account.company)] = existing_intel

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
                    )
                else:
                    st.warning("No named public contacts or public emails were found in the pages scanned.")

                st.markdown("### LinkedIn Intelligence")
                linkedin_signals = getattr(existing_intel, "linkedin_signals", tuple())
                if linkedin_signals:
                    dataframe_with_links(linkedin_signals_dataframe(existing_intel), width="stretch", hide_index=True)
                else:
                    st.caption("No public LinkedIn search signals were found yet. Run the scan again to refresh LinkedIn profile, company, and job-result signals.")

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
            st.info("Click Run public scan to pull public-source company intel, source pages, and available business contact evidence for this account.")


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
        st.caption("The app ranks the roles most likely to care about GovDash. Public scan results update the table below; otherwise each row gives the right LinkedIn search path.")
        current_contact_intel = st.session_state.get(public_intel_key(selected_contact_account.company))
        if not isinstance(current_contact_intel, CompanyIntel):
            current_contact_intel = None
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
        contact_intel = st.session_state.get(public_intel_key(selected_contact_account.company))
        if st.button("Scan public sources for actual contacts", key=f"contact_scan_{selected_contact_account.company}"):
            with st.spinner("Searching public pages for named contacts, emails, and phone numbers..."):
                contact_intel = enrich_account(selected_contact_account)
                st.session_state[public_intel_key(selected_contact_account.company)] = contact_intel

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
            st.caption("Run the scan to populate public names, LinkedIn profile signals, business emails, business phones, and source evidence when available.")

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
        )

with tabs[3]:
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
            current = st.session_state.get(crm_key, {})
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
            crm_intel = st.session_state.get(public_intel_key(selected_account.company))
            if isinstance(crm_intel, CompanyIntel) and crm_intel.contacts:
                best_public = crm_intel.contacts[0]
                best_contact_body = (
                    f"{best_public.full_name or best_public.title}"
                    f"{' | ' + best_public.email if best_public.email else ''}"
                    f"{' | ' + best_public.phone if best_public.phone else ''}"
                )
            else:
                best_contact_body = f"{best_target.title} - {best_target.why}"
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
        )

with tabs[4]:
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
        demo_intel = st.session_state.get(public_intel_key(selected_demo_account.company))
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

with tabs[5]:
    if not accounts:
        st.info("No accounts to sequence. Adjust filters on the left to load recent award winners.")
    else:
        selected_sequence_account = active_account(accounts)
        selected_sequence = selected_sequence_account.primary
        st.caption(f"Using active company: {selected_sequence_account.company}")
        sequence_intel = st.session_state.get(public_intel_key(selected_sequence_account.company))
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
            st.caption("Run Public Intel scan first to populate announcements, LinkedIn updates, podcasts, interviews, and other call triggers.")

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
            st.caption("Run Public Intel scan first to populate evidence-backed or industry-benchmark pain points.")

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

with tabs[6]:
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
        "Ready means the SDR can verify and add to cadence. Verify first means it is a research lead. Not ready means use manual LinkedIn research or a verified enrichment provider before outreach."
    )
    st.markdown("### Gaps & Recommended Updates")
    dataframe_with_links(product_gap_dataframe(), width="stretch", hide_index=True)
    st.markdown("### What To Add Next")
    st.write(
        "My strongest recommendation: add verified contact enrichment plus a real CRM database next. The app is now strong for research and SDR prep, but a production SDR workflow needs durable account/contact records, dedupe, verified emails/phones, and activity sync."
    )
