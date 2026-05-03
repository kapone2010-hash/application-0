from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote_plus

import pandas as pd
import requests
import streamlit as st


USASPENDING_AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
DEFAULT_LOOKBACK_DAYS = 30


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
    response = requests.post(USASPENDING_AWARD_SEARCH_URL, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("results", []), data.get("messages", [])


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


def public_links(prospect: Prospect) -> dict[str, str]:
    company = prospect.company
    return {
        "Company site": search_url(f'"{company}" official website'),
        "Leadership": search_url(f'"{company}" leadership government contracts'),
        "LinkedIn contacts": linkedin_url(company, "capture proposal contracts"),
        "News": search_url(f'"{company}" "{prospect.award_id}" contract award'),
        "USAspending": search_url(f'site:usaspending.gov "{prospect.award_id}"'),
        "SAM.gov": search_url(f'site:sam.gov "{prospect.award_id}" "{company}"'),
    }


def suggested_personas(prospect: Prospect) -> list[str]:
    text = " ".join([prospect.description, prospect.naics_description, prospect.psc_description]).lower()
    personas = ["VP/Director of Business Development", "Capture Manager", "Proposal Manager"]
    if any(term in text for term in ["cyber", "software", "network", "telecom", "cloud", "data"]):
        personas.append("Chief Technology Officer")
    if any(term in text for term in ["facilities", "maintenance", "construction", "operation"]):
        personas.append("Program Operations Lead")
    personas.append("Contracts Manager")
    return personas


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
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <section class="top-band">
      <h1>Application 0</h1>
      <p>SDR prospecting for newly reported government-contract winners. Pull recent public award data, identify likely contacts, and turn each win into a GovDash demo angle.</p>
      <div class="mini-row">
        <span class="mini">USAspending public API</span>
        <span class="mini">Recent contract awards</span>
        <span class="mini">GovDash demo builder</span>
        <span class="mini">SDR outreach workflow</span>
      </div>
    </section>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Lead Filters")
    lookback_days = st.slider("Days back", 7, 90, DEFAULT_LOOKBACK_DAYS)
    result_limit = st.slider("Max awards", 10, 100, 30, step=10)
    min_amount = st.number_input("Minimum award amount", min_value=0, value=100000, step=50000)
    keyword = st.text_input("Keyword", placeholder="cyber, construction, satellite...")
    st.caption("Data comes from USAspending award search. SAM.gov Contract Awards can be added when you provide a SAM.gov public API key.")

end_date = date.today()
start_date = end_date - timedelta(days=lookback_days)

try:
    with st.spinner("Pulling recent public award data..."):
        rows, api_messages = fetch_recent_awards(start_date, end_date, result_limit, int(min_amount), keyword.strip())
    prospects = [parse_prospect(row) for row in rows]
except Exception as exc:
    st.error(f"Could not load USAspending data: {exc}")
    st.stop()

prospects = sorted(prospects, key=lambda item: (item.govdash_fit_score, item.amount), reverse=True)

metrics = st.columns(4)
metrics[0].metric("Prospects", len(prospects))
metrics[1].metric("Date range", f"{start_date:%b %d} - {end_date:%b %d}")
metrics[2].metric("Largest award", money(max((p.amount for p in prospects), default=0)))
metrics[3].metric("Top fit score", max((p.govdash_fit_score for p in prospects), default=0))

if api_messages:
    with st.expander("API notes"):
        for message in api_messages:
            st.write(message)

tabs = st.tabs(["Lead Board", "SDR Workbench", "GovDash Demo", "Data Notes"])

with tabs[0]:
    st.subheader("Recently Reported Winners")
    if prospects:
        st.dataframe(to_dataframe(prospects), width="stretch", hide_index=True)
    else:
        st.info("No awards matched these filters. Try a longer date range, lower amount, or broader keyword.")

    st.download_button(
        "Download lead board CSV",
        data=to_dataframe(prospects).to_csv(index=False),
        file_name="application-0-govdash-prospects.csv",
        mime="text/csv",
        disabled=not prospects,
    )

with tabs[1]:
    if not prospects:
        st.info("No prospects to work. Adjust filters on the left to load recent award winners.")
    else:
        left, right = st.columns([0.38, 0.62])
        with left:
            selected_company = st.selectbox("Select a company", [p.company for p in prospects])

        selected = next(p for p in prospects if p.company == selected_company)

        with right:
            st.markdown(
                f"""
                <div class="prospect-card">
                  <h3>{selected.company}</h3>
                  <div class="muted">{selected.urgency} | {money(selected.amount)} | {selected.awarding_agency}</div>
                  <div class="muted">{selected.description}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        contact_cols = st.columns([0.48, 0.52])
        with contact_cols[0]:
            st.markdown("### Contact Information")
            st.markdown(
                f"""
                <div class="contact-box">
                  <b>Public address</b><br>
                  {selected.address or "Not provided"}<br>
                  {selected.location or "Location not provided"}<br><br>
                  <b>UEI</b><br>{selected.uei or "Not provided"}<br><br>
                  <b>Likely SDR personas</b><br>
                  {", ".join(suggested_personas(selected))}
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption("Public award data usually does not include verified direct emails or phone numbers. Use the links below to confirm people and contact channels before outreach.")

        with contact_cols[1]:
            st.markdown("### Public Research Links")
            links = public_links(selected)
            st.markdown(
                '<div class="link-grid">'
                + "".join(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>' for label, url in links.items())
                + "</div>",
                unsafe_allow_html=True,
            )

        st.markdown("### Outreach Draft")
        st.code(outreach_copy(selected), language="text")

with tabs[2]:
    if not prospects:
        st.info("No prospects to demo. Adjust filters on the left to load recent award winners.")
    else:
        selected_company_demo = st.selectbox(
            "Build demo for",
            [p.company for p in prospects],
            index=0,
            key="demo_company",
        )
        selected_demo = next(p for p in prospects if p.company == selected_company_demo)

        st.markdown(
            f"""
            <div class="prospect-card">
              <h3>Demo premise: {selected_demo.company}</h3>
              <div class="muted">Use their new {money(selected_demo.amount)} award as the opening scene, then show how GovDash turns it into capture, proposal, and contract-management motion.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

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

        st.markdown("### Demo Talk Track")
        st.write(
            f"Lead with: \"You just won {selected_demo.award_id}. In GovDash, we would use that win to centralize the award record, "
            f"extract reusable past-performance language, prepare for modifications and option years, and identify similar opportunities "
            f"at {selected_demo.awarding_agency or 'the buying agency'}.\""
        )

with tabs[3]:
    st.markdown("### Source Strategy")
    st.write(
        "Application 0 uses the USAspending public API because it does not require authorization and exposes recent federal contract-award data. "
        "For deeper SAM.gov award records, add a SAM.gov public API key later and connect the Contract Awards API."
    )
    st.markdown("### Compliance Notes")
    st.write(
        "The app avoids inventing personal contact details. It shows public recipient address data, likely buying personas, and search links that an SDR can use to verify official company pages, LinkedIn profiles, press releases, SAM.gov records, and USAspending pages."
    )
    st.markdown("### Next Data Enrichment Options")
    st.write(
        "Good next integrations: SAM.gov Contract Awards API, company-domain discovery, verified email enrichment, CRM export, and a GovDash-specific demo deck generator."
    )
