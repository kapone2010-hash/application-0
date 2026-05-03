from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from time import sleep
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import pandas as pd
import requests
import streamlit as st


USASPENDING_AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PUBLIC_SEARCH_URL = "https://duckduckgo.com/html/"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_STATUSES = ["New", "Researching", "Contact found", "Emailed", "Meeting booked", "Nurture", "Disqualified"]
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


@dataclass(frozen=True)
class Prospect:
    award_id: str
    company: str
    uei: str
    amount: float
    base_obligation_date: str
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
class CompanyIntel:
    company: str
    website: str
    what_they_do: str
    why_they_may_have_won: str
    contacts: tuple[PublicContact, ...]
    sources: tuple[str, ...]
    scanned_urls: tuple[str, ...]


def build_search_payload(start: date, end: date, limit: int, min_amount: int, keyword: str) -> dict:
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
        "sort": "Base Obligation Date",
        "order": "desc",
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
        "Contracts contact": search_url(f'"{company}" contracts manager email government'),
        "Proposal team": search_url(f'"{company}" proposal manager capture manager'),
        "News": search_url(f'"{company}" "{prospect.award_id}" contract award'),
        "USAspending": search_url(f'site:usaspending.gov "{prospect.award_id}"'),
        "SAM.gov": search_url(f'site:sam.gov "{prospect.award_id}" "{company}"'),
    }


def html_escape(value: object) -> str:
    return escape(str(value or ""), quote=True)


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
def search_public_web(query: str, max_results: int = 5) -> tuple[str, ...]:
    try:
        response = requests.get(PUBLIC_SEARCH_URL, params={"q": query}, headers=REQUEST_HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        return tuple()

    soup = BeautifulSoup(response.text, "html.parser")
    results: list[str] = []
    for anchor in soup.select("a.result__a"):
        url = normalize_search_result_url(anchor.get("href", ""))
        if url and fetchable_public_url(url) and url not in results:
            results.append(url)
        if len(results) >= max_results:
            break
    return tuple(results)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_public_page(url: str) -> tuple[str, str]:
    if not fetchable_public_url(url):
        return "", ""
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=12, allow_redirects=True)
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
    queries = [
        f'"{company}" official website',
        f'"{company}" leadership',
        f'"{company}" "business development"',
        f'"{company}" "capture manager"',
        f'"{company}" "proposal manager"',
        f'"{company}" "contracts manager"',
        f'"{company}" "{award_id}" contract award',
    ]

    candidate_urls: list[str] = []
    for query in queries:
        for url in search_public_web(query, max_results=4):
            if url not in candidate_urls:
                candidate_urls.append(url)
        if len(candidate_urls) >= 12:
            break

    website = ""
    for url in candidate_urls:
        domain = url_domain(url)
        if not any(blocked in domain for blocked in ["usaspending.gov", "sam.gov", "govinfo.gov", "defense.gov", "prnewswire.com"]):
            website = domain_root(url)
            break

    scan_urls = list(candidate_urls[:8])
    if website and website not in scan_urls:
        scan_urls.insert(0, website)

    scanned_urls: list[str] = []
    source_urls: list[str] = []
    page_texts: list[str] = []
    contacts: list[PublicContact] = []

    index = 0
    while index < len(scan_urls) and len(scanned_urls) < 12:
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

        if website and url_domain(final_url) == url_domain(website):
            for extra_url in source_links_from_html(html, final_url, limit=5):
                if extra_url not in scan_urls and len(scan_urls) < 16:
                    scan_urls.append(extra_url)

    primary_like = Prospect(
        award_id=award_id,
        company=company,
        uei="",
        amount=award_amount,
        base_obligation_date="",
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
        contacts=dedupe_contacts(contacts),
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
                "Why contact": contact.recommended_reason,
                "Source": contact.source_url,
                "Evidence": contact.evidence,
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


def demo_asset_pack(prospect: Prospect) -> dict[str, str]:
    agency = prospect.funding_sub_agency or prospect.awarding_sub_agency or prospect.awarding_agency
    return {
        "Opening scene": (
            f"Start with {prospect.company}'s {money(prospect.amount)} award {prospect.award_id} with {agency}. "
            f"Show the award record, dates, NAICS/PSC, and the contract description as the source of truth."
        ),
        "Pain hypothesis": (
            f"The team likely needs to organize {prospect.contract_focus} while preserving reusable material for future bids."
        ),
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
        if isinstance(intel, CompanyIntel) and intel.contacts:
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
                "Best public contact": best_public_contact,
                "Best public email": best_public_email,
                "Best public phone": best_public_phone,
                "Best public source": best_public_source,
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
        st.rerun()
    st.caption("Data comes from USAspending award search. SAM.gov Contract Awards can be added when you provide a SAM.gov public API key.")

end_date = date.today()
start_date = end_date - timedelta(days=lookback_days)

try:
    with st.spinner("Pulling recent public award data..."):
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

st.caption(f"Live source refresh: {st.session_state.get('last_refresh', 'not yet loaded')} | Cached for 30 minutes unless filters change or Refresh live data is clicked.")

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

if api_messages:
    with st.expander("API notes"):
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

        st.dataframe(account_dataframe(accounts), width="stretch", hide_index=True)

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
            "Public scan searches open web results and public company pages. It does not bypass logins, paywalls, robots restrictions, or invent missing emails and phone numbers."
        )

        run_scan = st.button("Run public scan", key=f"run_scan_{selected_intel_account.company}")
        existing_intel = st.session_state.get(public_intel_key(selected_intel_account.company))
        if run_scan:
            with st.spinner("Scanning public sources for company intel and contact evidence..."):
                existing_intel = enrich_account(selected_intel_account)
                st.session_state[public_intel_key(selected_intel_account.company)] = existing_intel

        if isinstance(existing_intel, CompanyIntel):
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
                    st.dataframe(public_contacts_dataframe(existing_intel), width="stretch", hide_index=True)
                    st.download_button(
                        "Download public intel CSV",
                        data=public_contacts_dataframe(existing_intel).to_csv(index=False),
                        file_name=f"{selected_intel_account.company.lower().replace(' ', '-')}-public-intel.csv",
                        mime="text/csv",
                    )
                else:
                    st.warning("No named public contacts or public emails were found in the pages scanned.")

                st.markdown("### What SDR Should Verify")
                for item in [
                    "Confirm the person still works at the company.",
                    "Confirm the email or phone is business contact information from the source page.",
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
        st.caption("The app ranks the roles most likely to care about GovDash. Use the search links to find and verify named people before outreach.")
        for target in contact_targets(selected_contact_account):
            st.markdown(
                f"""
                <div class="target-card">
                  <span class="target-rank">{target.rank}</span><h4>{target.title}</h4>
                  <div class="muted"><b>Why this person:</b> {target.why}</div>
                  <div class="muted"><b>Message angle:</b> {target.message_angle}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            link_cols = st.columns(3)
            link_cols[0].link_button("LinkedIn search", search_url(target.search_query))
            link_cols[1].link_button("Company site search", search_url(f'"{selected_contact_account.company}" "{target.title}"'))
            link_cols[2].link_button("Email pattern search", search_url(f'"{selected_contact_account.company}" email {target.title}'))

        st.markdown("### Actual Public Contacts")
        contact_intel = st.session_state.get(public_intel_key(selected_contact_account.company))
        if st.button("Scan public sources for actual contacts", key=f"contact_scan_{selected_contact_account.company}"):
            with st.spinner("Searching public pages for named contacts, emails, and phone numbers..."):
                contact_intel = enrich_account(selected_contact_account)
                st.session_state[public_intel_key(selected_contact_account.company)] = contact_intel

        if isinstance(contact_intel, CompanyIntel) and contact_intel.contacts:
            st.dataframe(public_contacts_dataframe(contact_intel), width="stretch", hide_index=True)
        elif isinstance(contact_intel, CompanyIntel):
            st.info("The scan did not find a verified named contact. Use the role-based search links above and keep the account in Researching.")
        else:
            st.caption("Run the scan to populate public names, business emails, business phones, and source evidence when available.")

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
        st.dataframe(to_dataframe(list(selected_account.prospects)), width="stretch", hide_index=True)

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
        pack = demo_asset_pack(selected_demo)
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
        "The app retries transient API failures and caches successful responses for 30 minutes to keep the live workflow responsive."
    )
    st.markdown("### Contact Quality Guardrails")
    st.write(
        "The app avoids inventing contact details. Public Intel only shows names, emails, phones, and evidence found on public pages the app could fetch. "
        "Treat those findings as SDR research, verify role and business contact status, and do not store personal or residential information."
    )
    st.markdown("### What To Add Next")
    st.write(
        "Best next integrations: SAM.gov Contract Awards API, verified contact enrichment, HubSpot/Salesforce sync, account history beyond the current lookback window, and a GovDash demo deck generator."
    )
