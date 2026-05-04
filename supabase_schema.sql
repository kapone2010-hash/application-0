-- Application 0 Supabase schema
-- Run this in Supabase SQL Editor before adding SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.

create table if not exists public.crm_accounts (
    company text primary key,
    status text default 'New',
    owner text default '',
    persona text default '',
    cadence_stage text default '',
    next_action text default '',
    next_step text default '',
    emailed boolean default false,
    called boolean default false,
    email_outcome text default '',
    call_outcome text default '',
    notes text default '',
    updated_at timestamptz default now()
);

create table if not exists public.verified_contacts (
    id bigint generated always as identity primary key,
    company text not null,
    full_name text not null,
    title text default '',
    email text default '',
    phone text default '',
    linkedin_url text default '',
    source_url text default '',
    source_type text default '',
    verification_status text default '',
    verified_at timestamptz default now(),
    notes text default '',
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_verified_contacts_company
    on public.verified_contacts(company);

create index if not exists idx_verified_contacts_company_name
    on public.verified_contacts(company, full_name);

create table if not exists public.crm_activities (
    id bigint generated always as identity primary key,
    company text not null,
    activity_type text not null,
    contact_name text default '',
    subject text default '',
    outcome text default '',
    notes text default '',
    due_date date,
    completed boolean default false,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_crm_activities_company
    on public.crm_activities(company);

create index if not exists idx_crm_activities_company_due
    on public.crm_activities(company, completed, due_date);

alter table public.crm_accounts enable row level security;
alter table public.verified_contacts enable row level security;
alter table public.crm_activities enable row level security;

-- No anon/authenticated policies are created on purpose.
-- The Streamlit server uses SUPABASE_SERVICE_ROLE_KEY from secrets.
-- Do not expose the service role key in browser code.
