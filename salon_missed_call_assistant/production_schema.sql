CREATE TABLE salons (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE staff_users (
    id BIGSERIAL PRIMARY KEY,
    salon_id BIGINT NOT NULL REFERENCES salons(id),
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT DEFAULT '',
    role TEXT NOT NULL,
    auth_provider_user_id TEXT DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE clients (
    id BIGSERIAL PRIMARY KEY,
    salon_id BIGINT NOT NULL REFERENCES salons(id),
    name TEXT NOT NULL,
    phone_e164 TEXT NOT NULL,
    email TEXT DEFAULT '',
    consent_status TEXT NOT NULL DEFAULT 'Unknown',
    consent_source TEXT DEFAULT '',
    consent_updated_at TIMESTAMPTZ,
    opt_out_at TIMESTAMPTZ,
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (salon_id, phone_e164)
);

CREATE TABLE consent_events (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES clients(id),
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE services (
    id BIGSERIAL PRIMARY KEY,
    salon_id BIGINT NOT NULL REFERENCES salons(id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    base_price NUMERIC(10,2) NOT NULL,
    deposit_required BOOLEAN NOT NULL DEFAULT false,
    deposit_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
    cancellation_window_hours INTEGER NOT NULL DEFAULT 24,
    requires_consultation BOOLEAN NOT NULL DEFAULT false,
    prep_notes TEXT DEFAULT '',
    price_notes TEXT DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE stylists (
    id BIGSERIAL PRIMARY KEY,
    salon_id BIGINT NOT NULL REFERENCES salons(id),
    name TEXT NOT NULL,
    specialties TEXT NOT NULL,
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE conversations (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES clients(id),
    status TEXT NOT NULL,
    last_intent TEXT DEFAULT '',
    last_message TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id),
    sender TEXT NOT NULL,
    body TEXT NOT NULL,
    channel TEXT NOT NULL,
    provider_message_id TEXT DEFAULT '',
    delivery_status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE appointments (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES clients(id),
    service_id BIGINT NOT NULL REFERENCES services(id),
    stylist_id BIGINT NOT NULL REFERENCES stylists(id),
    appointment_date DATE NOT NULL,
    appointment_time TEXT NOT NULL,
    status TEXT NOT NULL,
    client_request TEXT DEFAULT '',
    deposit_status TEXT NOT NULL DEFAULT 'Not required',
    deposit_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
    payment_link TEXT DEFAULT '',
    calendar_sync_status TEXT NOT NULL DEFAULT 'Not synced',
    calendar_event_ref TEXT DEFAULT '',
    cancellation_deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_events (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phone TEXT NOT NULL,
    client_name TEXT DEFAULT '',
    payload JSONB NOT NULL,
    signature_status TEXT NOT NULL,
    conversation_id BIGINT REFERENCES conversations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payment_requests (
    id BIGSERIAL PRIMARY KEY,
    appointment_id BIGINT NOT NULL REFERENCES appointments(id),
    provider TEXT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    status TEXT NOT NULL,
    payment_link TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE calendar_sync_events (
    id BIGSERIAL PRIMARY KEY,
    appointment_id BIGINT NOT NULL REFERENCES appointments(id),
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    external_ref TEXT DEFAULT '',
    details TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE appointment_reminders (
    id BIGSERIAL PRIMARY KEY,
    appointment_id BIGINT NOT NULL REFERENCES appointments(id),
    reminder_type TEXT NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id BIGSERIAL PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
