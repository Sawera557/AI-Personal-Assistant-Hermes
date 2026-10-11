-- Hermes — reference / seed data (0002_seed)
-- Idempotent: ON CONFLICT DO NOTHING so re-running is safe. Seeds only stable
-- reference rows (roles, the 8 businesses, the app catalog) — never user data.

-- ---- roles ----------------------------------------------------------------
INSERT INTO roles (name, description) VALUES
    ('owner',     'Full access across all businesses, including approvals.'),
    ('admin',     'Manage one or more businesses; cannot change global settings.'),
    ('read-only', 'View data only; cannot trigger writes or approvals.')
ON CONFLICT (name) DO NOTHING;

-- ---- entities (the 8 businesses) ------------------------------------------
-- region pinned to us-east-1 per the current decision; country tags Kenya
-- businesses so a future data-residency move is a data change, not a schema one.
INSERT INTO entities (slug, name, business_type, country, region) VALUES
    ('gak_properties',  'GAK Properties',  'real_estate',  'US', 'us-east-1'),
    ('gif_properties',  'GIF Properties',  'real_estate',  'US', 'us-east-1'),
    ('comfort_services','Comfort Services', 'services',      'US', 'us-east-1'),
    ('afro_farm',       'Afro Farm',        'agriculture',  'KE', 'us-east-1'),
    ('gak_commodities', 'GAK Commodities',  'commodities',  'KE', 'us-east-1'),
    ('nour_hammam',     'Nour Hammam',      'services',      'US', 'us-east-1'),
    ('personal',        'Personal',         'personal',      'US', 'us-east-1'),
    ('kenya_ventures',  'Kenya Ventures',   'holding',       'KE', 'us-east-1')
ON CONFLICT (slug) DO NOTHING;

-- ---- connected_apps (the app catalog) -------------------------------------
-- Slugs match HERMES_APPS so the agent's tool layer and the DB agree.
INSERT INTO connected_apps (slug, name, category, provider) VALUES
    ('quickbooks',      'QuickBooks',       'accounting',    'Intuit'),
    ('pdf-co',          'PDF.co',           'documents',     'PDF.co'),
    ('google-contacts', 'Google Contacts',  'contacts',      'Google'),
    ('fathom',          'Fathom',           'meetings',      'Fathom'),
    ('google-meet',     'Google Meet',      'meetings',      'Google'),
    ('google-forms',    'Google Forms',     'forms',         'Google'),
    ('google-sheets',   'Google Sheets',    'spreadsheets',  'Google'),
    ('google-docs',     'Google Docs',      'documents',     'Google'),
    ('google-drive',    'Google Drive',     'storage',       'Google'),
    ('google-calendar', 'Google Calendar',  'calendar',      'Google'),
    ('google-mail',     'Gmail',            'email',         'Google'),
    ('notion',          'Notion',           'productivity',  'Notion')
ON CONFLICT (slug) DO NOTHING;
